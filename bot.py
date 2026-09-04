"""
Saver — 디스코드 채널 이미지 일괄 백업 봇
/save 로 "마지막 저장 지점 이후" 이미지를 ZIP으로 묶어 R2 링크로 전달.
커서는 채널 × 호출자 기준. 채널 주인 매핑(/owner)으로 인자 없이 자기 방 자동 지정.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp
import boto3
import discord
from botocore.config import Config as BotoConfig
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# ───────────────────────── 설정 ─────────────────────────
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ.get("GUILD_ID", "0") or 0)
def _account_id(raw: str) -> str:
    """계정 ID만, 또는 S3 API 주소 전체가 들어와도 32자리 ID만 추출."""
    m = re.search(r"([0-9a-f]{32})", raw)
    if not m:
        raise SystemExit(f"R2_ACCOUNT_ID 값이 이상합니다: {raw!r} — 32자리 16진수 계정 ID여야 합니다")
    return m.group(1)


R2_ACCOUNT_ID = _account_id(os.environ["R2_ACCOUNT_ID"])
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
LINK_TTL_HOURS = int(os.environ.get("LINK_TTL_HOURS", "72"))
MAX_ZIP_MB = int(os.environ.get("MAX_ZIP_MB", "1500"))
INCLUDE_VIDEO = os.environ.get("INCLUDE_VIDEO", "0") == "1"
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "saver.sqlite3"
DOWNLOAD_CONCURRENCY = 6

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("saver")

# ───────────────────────── DB ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS cursors(
        channel_id INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        last_message_id INTEGER NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(channel_id, user_id)
    );
    CREATE TABLE IF NOT EXISTS owners(
        channel_id INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL
    );
    CREATE TABLE IF NOT EXISTS uploads(
        key        TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        size       INTEGER NOT NULL,
        expires_at REAL NOT NULL
    );
    """
)
db.commit()


def cursor_get(channel_id: int, user_id: int) -> Optional[int]:
    row = db.execute(
        "SELECT last_message_id FROM cursors WHERE channel_id=? AND user_id=?", (channel_id, user_id)
    ).fetchone()
    return row[0] if row else None


def cursor_set(channel_id: int, user_id: int, message_id: int) -> None:
    db.execute(
        "INSERT INTO cursors(channel_id,user_id,last_message_id,updated_at) VALUES(?,?,?,?) "
        "ON CONFLICT(channel_id,user_id) DO UPDATE SET last_message_id=excluded.last_message_id, updated_at=excluded.updated_at",
        (channel_id, user_id, message_id, dt.datetime.utcnow().isoformat()),
    )
    db.commit()


def cursor_del(channel_id: int, user_id: int) -> int:
    n = db.execute("DELETE FROM cursors WHERE channel_id=? AND user_id=?", (channel_id, user_id)).rowcount
    db.commit()
    return n


def owner_get(user_id: int) -> Optional[int]:
    row = db.execute("SELECT channel_id FROM owners WHERE user_id=?", (user_id,)).fetchone()
    return row[0] if row else None


def owner_set(channel_id: int, user_id: int) -> None:
    db.execute(
        "INSERT INTO owners(channel_id,user_id) VALUES(?,?) ON CONFLICT(channel_id) DO UPDATE SET user_id=excluded.user_id",
        (channel_id, user_id),
    )
    db.commit()


# ───────────────────────── R2 ─────────────────────────
s3 = boto3.client(
    "s3",
    endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=R2_ACCESS_KEY,
    aws_secret_access_key=R2_SECRET_KEY,
    region_name="auto",
    config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 4}),
)


def r2_upload(path: Path, key: str) -> str:
    s3.upload_file(str(path), R2_BUCKET, key, ExtraArgs={"ContentType": "application/zip"})
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key, "ResponseContentDisposition": f'attachment; filename="{Path(key).name}"'},
        ExpiresIn=LINK_TTL_HOURS * 3600,
    )


def r2_delete(key: str) -> None:
    try:
        s3.delete_object(Bucket=R2_BUCKET, Key=key)
    except Exception as e:  # noqa: BLE001
        log.warning("R2 삭제 실패 %s: %s", key, e)


# ───────────────────────── 유틸 ─────────────────────────
def safe_name(s: str, limit: int = 40) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .")
    return (s or "_")[:limit]


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def is_wanted(filename: str, content_type: Optional[str]) -> bool:
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXT or (content_type or "").startswith("image/"):
        return True
    if INCLUDE_VIDEO and (ext in VIDEO_EXT or (content_type or "").startswith("video/")):
        return True
    return False


@dataclass
class Item:
    url: str
    filename: str
    author: str
    created: dt.datetime
    message_id: int
    size: int = 0


@dataclass
class Job:
    user: discord.abc.User
    channel: discord.TextChannel
    after_id: Optional[int]
    mine: bool
    by_author: bool
    label: str
    items: list[Item] = field(default_factory=list)


# ───────────────────────── 봇 ─────────────────────────
intents = discord.Intents.default()
intents.message_content = True


class Saver(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.job_lock = asyncio.Lock()
        self.waiting = 0
        self.http_session: Optional[aiohttp.ClientSession] = None

    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300))
        if GUILD_ID:
            g = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
            log.info("슬래시 명령 길드 동기화 완료 (%s)", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("슬래시 명령 전역 동기화 완료 (반영까지 최대 1시간)")
        self.loop.create_task(self.cleanup_loop())

    async def close(self):
        if self.http_session:
            await self.http_session.close()
        await super().close()

    async def cleanup_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                now = time.time()
                rows = db.execute("SELECT key FROM uploads WHERE expires_at < ?", (now,)).fetchall()
                for (key,) in rows:
                    await asyncio.to_thread(r2_delete, key)
                    db.execute("DELETE FROM uploads WHERE key=?", (key,))
                if rows:
                    db.commit()
                    log.info("만료 ZIP %d개 정리", len(rows))
            except Exception:  # noqa: BLE001
                log.exception("정리 루프 오류")
            await asyncio.sleep(3600)


client = Saver()


# ───────────────────────── 수집 로직 ─────────────────────────
async def collect(job: Job, progress) -> Optional[int]:
    """채널 히스토리 훑어 items 채움. 마지막으로 본 메시지 ID 반환."""
    last_id = None
    scanned = 0
    after = discord.Object(id=job.after_id) if job.after_id else None
    async for msg in job.channel.history(limit=None, after=after, oldest_first=True):
        last_id = msg.id
        scanned += 1
        if job.mine and msg.author.id != job.user.id:
            continue
        author = safe_name(msg.author.display_name)
        for a in msg.attachments:
            if is_wanted(a.filename, a.content_type):
                job.items.append(Item(a.url, a.filename, author, msg.created_at, msg.id, a.size))
        for e in msg.embeds:
            for part in (e.image, e.thumbnail):
                url = getattr(part, "url", None)
                if url and re.search(r"(cdn|media)\.discordapp\.(com|net)/", url):
                    name = Path(url.split("?", 1)[0]).name or "embed.png"
                    if is_wanted(name, None):
                        job.items.append(Item(url, name, author, msg.created_at, msg.id))
        if scanned % 200 == 0:
            await progress(f"메시지 {scanned}개 확인 중… 이미지 {len(job.items)}장 발견")
    await progress(f"메시지 {scanned}개 확인 완료 → 이미지 {len(job.items)}장")
    return last_id


async def download_all(job: Job, workdir: Path, progress) -> tuple[int, int, int]:
    """items를 workdir에 저장. (성공, 중복스킵, 실패) 반환."""
    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    seen_hash: set[str] = set()
    ok = dup = fail = 0
    done = 0
    total = len(job.items)
    last_tick = 0.0

    async def one(it: Item):
        nonlocal ok, dup, fail, done, last_tick
        async with sem:
            data = None
            for attempt in range(3):
                try:
                    async with client.http_session.get(it.url) as r:
                        if r.status == 200:
                            data = await r.read()
                            break
                        if r.status in (403, 404):
                            break
                except Exception:  # noqa: BLE001
                    await asyncio.sleep(1 + attempt)
            done += 1
            if data is None:
                fail += 1
            else:
                h = hashlib.sha256(data).hexdigest()
                if h in seen_hash:
                    dup += 1
                else:
                    seen_hash.add(h)
                    stamp = it.created.astimezone(dt.timezone(dt.timedelta(hours=9))).strftime("%Y%m%d_%H%M")
                    base = safe_name(Path(it.filename).stem, 60)
                    ext = Path(it.filename).suffix.lower() or ".png"
                    name = f"{stamp}_{it.author}_{it.message_id}_{base}{ext}"
                    folder = workdir / it.author if job.by_author else workdir
                    folder.mkdir(parents=True, exist_ok=True)
                    (folder / name).write_bytes(data)
                    it.size = len(data)
                    ok += 1
            now = time.time()
            if now - last_tick > 2 or done == total:
                last_tick = now
                await progress(f"다운로드 {done}/{total}")

    await asyncio.gather(*(one(it) for it in job.items))
    return ok, dup, fail


def make_zips(workdir: Path, outdir: Path, stem: str) -> list[Path]:
    """workdir → ZIP(들). MAX_ZIP_MB 넘으면 분할."""
    limit = MAX_ZIP_MB * 1024 * 1024
    files = sorted(p for p in workdir.rglob("*") if p.is_file())
    zips: list[Path] = []
    part = 1
    cur: Optional[zipfile.ZipFile] = None
    cur_size = 0

    def open_new():
        nonlocal cur, cur_size, part
        p = outdir / (f"{stem}.zip" if part == 1 else f"{stem}_part{part}.zip")
        cur = zipfile.ZipFile(p, "w", zipfile.ZIP_STORED)
        zips.append(p)
        cur_size = 0

    open_new()
    for f in files:
        sz = f.stat().st_size
        if cur_size and cur_size + sz > limit:
            cur.close()
            part += 1
            open_new()
        cur.write(f, f.relative_to(workdir).as_posix())
        cur_size += sz
    cur.close()
    if len(zips) > 1:  # 첫 파일도 part1 이름으로 통일
        first = zips[0]
        renamed = outdir / f"{stem}_part1.zip"
        first.rename(renamed)
        zips[0] = renamed
    return zips


async def run_job(inter: discord.Interaction, job: Job):
    msg = await inter.followup.send("⏳ 준비 중…", ephemeral=True, wait=True)
    state = {"text": ""}

    async def progress(text: str):
        if text == state["text"]:
            return
        state["text"] = text
        try:
            await msg.edit(content=f"⏳ **#{job.channel.name}** · {text}")
        except discord.HTTPException:
            pass

    if client.job_lock.locked():
        client.waiting += 1
        await progress(f"대기열 {client.waiting}번째 — 앞 작업 끝나면 시작")
    async with client.job_lock:
        client.waiting = max(0, client.waiting - 1)
        tmp = Path(tempfile.mkdtemp(prefix="saver_"))
        try:
            await progress("메시지 확인 중…")
            last_id = await collect(job, progress)
            if not job.items:
                if last_id:
                    cursor_set(job.channel.id, job.user.id, last_id)
                await progress("새 이미지가 없습니다. 커서를 최신으로 맞췄어요.")
                return

            work = tmp / "files"
            work.mkdir()
            ok, dup, fail = await download_all(job, work, progress)
            if ok == 0:
                await progress(f"다운로드 실패 (실패 {fail}, 중복 {dup}). 커서는 유지합니다.")
                return

            await progress("ZIP 압축 중…")
            stamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y%m%d_%H%M%S")
            stem = f"{safe_name(job.channel.name)}_{stamp}"
            zips = await asyncio.to_thread(make_zips, work, tmp, stem)

            links = []
            total_bytes = 0
            for i, z in enumerate(zips, 1):
                await progress(f"업로드 중… ({i}/{len(zips)})")
                key = f"{job.channel.guild.id}/{job.channel.id}/{job.user.id}/{z.name}"
                url = await asyncio.to_thread(r2_upload, z, key)
                sz = z.stat().st_size
                total_bytes += sz
                db.execute(
                    "INSERT OR REPLACE INTO uploads(key,user_id,channel_id,size,expires_at) VALUES(?,?,?,?,?)",
                    (key, job.user.id, job.channel.id, sz, time.time() + LINK_TTL_HOURS * 3600),
                )
                links.append((z.name, url, sz))
            db.commit()

            cursor_set(job.channel.id, job.user.id, last_id)

            first = min(it.created for it in job.items)
            last = max(it.created for it in job.items)
            kst = dt.timezone(dt.timedelta(hours=9))
            span = f"{first.astimezone(kst):%Y-%m-%d} ~ {last.astimezone(kst):%Y-%m-%d}"
            expire = (dt.datetime.now(kst) + dt.timedelta(hours=LINK_TTL_HOURS)).strftime("%m/%d %H:%M")

            emb = discord.Embed(
                title=f"✅ #{job.channel.name} 저장 완료",
                description=f"**{ok}장** · {human(total_bytes)} · {job.label}\n기간 {span}",
                colour=0x8AA0D8,
            )
            for name, url, sz in links:
                emb.add_field(name=name, value=f"[다운로드]({url}) · {human(sz)}", inline=False)
            foot = f"링크 만료 {expire} (KST)"
            if dup or fail:
                foot += f" · 중복 {dup} · 실패 {fail}"
            emb.set_footer(text=foot)
            await msg.edit(content=None, embed=emb)
        except discord.Forbidden:
            await progress("❌ 이 채널을 읽을 권한이 없습니다. 봇 역할에 '채널 보기'와 '메시지 기록 보기'가 필요합니다.")
        except Exception as e:  # noqa: BLE001
            log.exception("작업 실패")
            await progress(f"❌ 오류: {type(e).__name__}: {e}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────── 명령 ─────────────────────────
def resolve_channel(inter: discord.Interaction, channel: Optional[discord.TextChannel]) -> discord.TextChannel:
    if channel:
        return channel
    own = owner_get(inter.user.id)
    if own:
        ch = inter.guild.get_channel(own)
        if isinstance(ch, discord.TextChannel):
            return ch
    return inter.channel  # type: ignore[return-value]


def parse_date(s: str) -> Optional[dt.datetime]:
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone(dt.timedelta(hours=9)))
        except ValueError:
            continue
    return None


@client.tree.command(name="save", description="마지막 저장 지점 이후의 이미지를 ZIP으로 묶어 링크로 드립니다")
@app_commands.describe(
    channel="대상 채널 (비우면 내 방 → 없으면 현재 채널)",
    from_date="이 날짜부터 (YYYY-MM-DD). 커서 무시",
    all="채널 전체 처음부터 (커서 무시)",
    mine="내가 올린 것만",
    by_author="작성자별 폴더로 나누기",
)
@app_commands.rename(from_date="from")
async def save_cmd(
    inter: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    from_date: Optional[str] = None,
    all: bool = False,
    mine: bool = False,
    by_author: bool = False,
):
    await inter.response.defer(ephemeral=True, thinking=True)
    ch = resolve_channel(inter, channel)
    if all:
        after_id, label = None, "전체"
    elif from_date:
        d = parse_date(from_date)
        if not d:
            await inter.followup.send("날짜 형식은 YYYY-MM-DD 입니다.", ephemeral=True)
            return
        after_id, label = discord.utils.time_snowflake(d), f"{d:%Y-%m-%d}부터"
    else:
        after_id = cursor_get(ch.id, inter.user.id)
        label = "이어서" if after_id else "처음부터"
    if mine:
        label += " · 내 것만"
    job = Job(inter.user, ch, after_id, mine, by_author, label)
    await run_job(inter, job)


@client.tree.command(name="save-status", description="이 채널에서 내 마지막 저장 지점을 확인합니다")
@app_commands.describe(channel="대상 채널 (비우면 내 방 → 현재 채널)")
async def status_cmd(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    ch = resolve_channel(inter, channel)
    cur = cursor_get(ch.id, inter.user.id)
    if not cur:
        await inter.response.send_message(f"#{ch.name}: 아직 저장한 적 없음 → 다음 /save 는 처음부터", ephemeral=True)
        return
    t = discord.utils.snowflake_time(cur).astimezone(dt.timezone(dt.timedelta(hours=9)))
    await inter.response.send_message(f"#{ch.name}: 마지막 저장 지점 **{t:%Y-%m-%d %H:%M}** (KST) 이후부터 이어서 받습니다", ephemeral=True)


@client.tree.command(name="save-reset", description="저장 지점(커서)을 초기화합니다. 다음 /save 는 처음부터")
@app_commands.describe(channel="대상 채널", user="다른 멤버 커서 초기화 (관리자만)")
async def reset_cmd(inter: discord.Interaction, channel: Optional[discord.TextChannel] = None, user: Optional[discord.Member] = None):
    ch = resolve_channel(inter, channel)
    target = inter.user
    if user and user.id != inter.user.id:
        if not inter.user.guild_permissions.administrator:
            await inter.response.send_message("다른 멤버의 커서는 관리자만 초기화할 수 있어요.", ephemeral=True)
            return
        target = user
    n = cursor_del(ch.id, target.id)
    who = "내" if target.id == inter.user.id else f"{target.display_name} 님"
    await inter.response.send_message(
        f"#{ch.name}: {who} 커서 {'초기화 완료' if n else '기록 없음'}", ephemeral=True
    )


owner_group = app_commands.Group(name="owner", description="채널 주인 매핑 (관리자)", default_permissions=discord.Permissions(administrator=True))


@owner_group.command(name="set", description="채널 주인을 지정합니다. 그 멤버가 /save 를 인자 없이 치면 자기 방이 자동 선택됩니다")
async def owner_set_cmd(inter: discord.Interaction, channel: discord.TextChannel, user: discord.Member):
    owner_set(channel.id, user.id)
    await inter.response.send_message(f"#{channel.name} 주인 = {user.display_name}", ephemeral=True)


@owner_group.command(name="list", description="채널 주인 목록")
async def owner_list_cmd(inter: discord.Interaction):
    rows = db.execute("SELECT channel_id,user_id FROM owners").fetchall()
    if not rows:
        await inter.response.send_message("등록된 주인 없음", ephemeral=True)
        return
    lines = []
    for cid, uid in rows:
        ch = inter.guild.get_channel(cid)
        m = inter.guild.get_member(uid)
        lines.append(f"#{ch.name if ch else cid} → {m.display_name if m else uid}")
    await inter.response.send_message("\n".join(lines), ephemeral=True)


@owner_group.command(name="remove", description="채널 주인 매핑 해제")
async def owner_remove_cmd(inter: discord.Interaction, channel: discord.TextChannel):
    n = db.execute("DELETE FROM owners WHERE channel_id=?", (channel.id,)).rowcount
    db.commit()
    await inter.response.send_message(f"#{channel.name}: {'해제 완료' if n else '기록 없음'}", ephemeral=True)


client.tree.add_command(owner_group)


@client.tree.command(name="save-all", description="현재 카테고리 안 모든 채널을 각각 ZIP으로 저장 (관리자)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(all="커서 무시하고 전체")
async def save_all_cmd(inter: discord.Interaction, all: bool = False):
    await inter.response.defer(ephemeral=True, thinking=True)
    cat = getattr(inter.channel, "category", None)
    chans = [c for c in (cat.text_channels if cat else inter.guild.text_channels)
             if c.permissions_for(inter.guild.me).read_message_history]
    await inter.followup.send(f"{len(chans)}개 채널 순차 처리 시작", ephemeral=True)
    for ch in chans:
        after_id = None if all else cursor_get(ch.id, inter.user.id)
        job = Job(inter.user, ch, after_id, False, True, "전체" if all else ("이어서" if after_id else "처음부터"))
        await run_job(inter, job)


@client.tree.error
async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("명령 오류", exc_info=error)
    text = f"❌ {type(error).__name__}: {error}"
    if inter.response.is_done():
        await inter.followup.send(text, ephemeral=True)
    else:
        await inter.response.send_message(text, ephemeral=True)


@client.event
async def on_ready():
    log.info("로그인: %s (%s) · 서버 %d개", client.user, client.user.id, len(client.guilds))


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
