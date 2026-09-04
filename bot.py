"""
Saver — 디스코드 채널 이미지 일괄 백업 봇 (츤데레 에디션)
/저장 으로 "마지막 저장 지점 이후" 이미지를 ZIP으로 묶어 R2 링크로 전달.
커서는 채널 × 호출자 기준. 채널 주인 매핑(/주인)으로 인자 없이 자기 방 자동 지정.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import os
import random
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
def _account_id(raw: str) -> str:
    """계정 ID만, 또는 S3 API 주소 전체가 들어와도 32자리 ID만 추출."""
    m = re.search(r"([0-9a-f]{32})", raw)
    if not m:
        raise SystemExit(f"R2_ACCOUNT_ID 값이 이상합니다: {raw!r} — 32자리 16진수 계정 ID여야 합니다")
    return m.group(1)


TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ.get("GUILD_ID", "0") or 0)
R2_ACCOUNT_ID = _account_id(os.environ["R2_ACCOUNT_ID"])
R2_ACCESS_KEY = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_BUCKET = os.environ["R2_BUCKET"]
LINK_TTL_HOURS = int(os.environ.get("LINK_TTL_HOURS", "1"))
MAX_ZIP_MB = int(os.environ.get("MAX_ZIP_MB", "1500"))
INCLUDE_VIDEO = os.environ.get("INCLUDE_VIDEO", "0") == "1"
DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "saver.sqlite3"
DOWNLOAD_CONCURRENCY = 6
KST = dt.timezone(dt.timedelta(hours=9))

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".avif", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("saver")


# ───────────────────────── 대사 ─────────────────────────
KAOMOJI = [
    "(๑•̀ㅂ•́)و✧", "(｀・ω・´)", "(￣^￣)ゞ", "ヽ(`Д´)ﾉ", "(╬ Ò﹏Ó)", "(¬_¬)", "( •̀ ω •́ )✧",
    "٩(◕‿◕｡)۶", "(☞ﾟヮﾟ)☞", "ヾ(≧▽≦*)o", "(ノ°益°)ノ", "(ง'̀-'́)ง", "(๑˃̵ᴗ˂̵)", "(´｡• ᵕ •｡`)",
    "┐(´д`)┌", "(｡•̀ᴗ-)✧", "ヽ(°〇°)ﾉ", "(¬‿¬)", "(≧◡≦)", "(⌐■_■)", "(*≧ω≦)", "( ｡•̀_•́｡)",
    "(・`ω´・)", "( ˘︹˘ )", "(ﾉ◕ヮ◕)ﾉ*:･ﾟ✧", "(๑•̀ㅁ•́๑)✧", "(ᗒᗣᗕ)՞", "ヾ(๑╹◡╹)ﾉ", "(｡•̀ᴗ-)", "(ᵔᴥᵔ)",
]

LINES = {
    "start": [
        "어이어이! 지금 저장 중이야~! 기다려봐!",
        "이몸이 직접 나섰다! 잠자코 기다려!",
        "귀찮게 하긴… 알았어, 저장해 준다고!",
        "흥, 이 정도쯤이야! 잠깐 기다려!",
        "저장 시작! 딴짓하지 말고 기다려!",
        "또 너냐! …알았어, 지금 긁어온다!",
    ],
    "done": [
        "어이! 이녀석아! 저장 다 됐어!",
        "흥, 이 정도야 이몸에겐 식은 죽 먹기지!",
        "다 됐어! 고마워하라고!",
        "끝! 이몸의 실력을 똑똑히 봤겠지!",
        "자, 받아! 잃어버리면 안 알려준다!",
        "저장 완료! 칭찬은 안 받아도 돼… 딱히!",
    ],
    "expire": [
        "{h}시간 뒤에 링크 만료야 코롸ㅡ!",
        "{h}시간 지나면 링크 사라진다! 빨리 받아!",
        "링크는 {h}시간짜리야! 늦으면 몰라!",
    ],
    "cursor": [
        "어이! 마지막 저장시점은 {d}이야! 알겠냐! 이몸을 귀찮게 하다니~!",
        "마지막으로 저장한 건 {d}! 거기서부터 이어간다! 기억 좀 해!",
        "{d}까지 저장했었잖아! 그 뒤부터야! 알겠냐!",
    ],
    "nocursor": [
        "저장한 적이 없잖아! 처음부터 싹 다 긁어주지!",
        "기록이 없네… 이번엔 처음부터야! 감사해라!",
    ],
    "empty": [
        "새 이미지가 없잖아! 이몸을 헛걸음시키다니~!",
        "받을 게 하나도 없어! 뭘 기대한 거야!",
        "텅 비었어! 다음엔 뭐라도 올리고 불러!",
    ],
    "queue": [
        "앞에 순서가 있어! 줄 서! {n}번째야!",
        "이몸은 하나뿐이라고! {n}번째로 기다려!",
    ],
    "error": [
        "으악! 뭔가 잘못됐어! 이몸 탓 아니야!",
        "에러다! …딱히 당황한 건 아니야!",
    ],
    "mark": [
        "여기까지는 이미 받은 거지? 알았어, 지금부터 새 것만 챙긴다!",
        "저장 지점을 지금으로 맞췄어! 옛날 건 이몸 몰라!",
    ],
    "reset": [
        "커서 지웠어! 다음엔 처음부터 다시야!",
        "초기화 완료! 기억 싹 지웠다!",
    ],
    "links": [
        "아직 안 죽은 링크 다시 준다! 이번엔 꼭 받아!",
        "흥, 또 잃어버린 거냐! 자, 받아!",
    ],
    "nolinks": [
        "살아있는 링크가 없어! 다시 /저장 해!",
    ],
}


def say(kind: str, **kw) -> str:
    return f"{random.choice(LINES[kind]).format(**kw)} {random.choice(KAOMOJI)}"


def kao() -> str:
    return random.choice(KAOMOJI)


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


def cursor_date(message_id: int) -> str:
    return discord.utils.snowflake_time(message_id).astimezone(KST).strftime("%Y년 %m월 %d일 %H:%M")


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


def r2_presign(key: str, seconds: int) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": key, "ResponseContentDisposition": f'attachment; filename="{Path(key).name}"'},
        ExpiresIn=max(60, seconds),
    )


def r2_upload(path: Path, key: str, callback=None) -> str:
    s3.upload_file(str(path), R2_BUCKET, key, ExtraArgs={"ContentType": "application/zip"}, Callback=callback)
    return r2_presign(key, LINK_TTL_HOURS * 3600)


async def upload_with_progress(z: Path, key: str, progress, label: str) -> str:
    """스레드에서 업로드하며 2초마다 진행률 갱신."""
    total = z.stat().st_size
    sent = {"n": 0}

    def cb(nbytes: int):
        sent["n"] += nbytes

    task = asyncio.create_task(asyncio.to_thread(r2_upload, z, key, cb))
    while not task.done():
        pct = sent["n"] * 100 // total if total else 100
        await progress(f"업로드 중… {label} {pct}% ({human(sent['n'])}/{human(total)})")
        await asyncio.sleep(2)
    return await task


def r2_delete(key: str) -> None:
    try:
        s3.delete_object(Bucket=R2_BUCKET, Key=key)
    except Exception as e:  # noqa: BLE001
        log.warning("R2 삭제 실패 %s: %s", key, e)


# ───────────────────────── 유틸 ─────────────────────────
def safe_name(s: str, limit: int = 40) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s).strip(" .")
    return (s or "_")[:limit]


def human(n: float) -> str:
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
    uid: str
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
            await asyncio.sleep(600)


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
                job.items.append(Item(a.url, a.filename, author, msg.created_at, msg.id, str(a.id), a.size))
        for ei, e in enumerate(msg.embeds):
            for part in (e.image, e.thumbnail):
                url = getattr(part, "url", None)
                if url and re.search(r"(cdn|media)\.discordapp\.(com|net)/", url):
                    name = Path(url.split("?", 1)[0]).name or "embed.png"
                    if is_wanted(name, None):
                        job.items.append(Item(url, name, author, msg.created_at, msg.id, f"{msg.id}e{ei}"))
        if scanned % 200 == 0:
            await progress(f"메시지 {scanned}개 뒤지는 중… 이미지 {len(job.items)}장 찾았어!")
    seen_url: set[str] = set()
    uniq = []
    for it in job.items:
        u = it.url.split("?", 1)[0]
        if u not in seen_url:
            seen_url.add(u)
            uniq.append(it)
    job.items = uniq
    await progress(f"메시지 {scanned}개 다 봤어! 이미지 {len(job.items)}장!")
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
                    stamp = it.created.astimezone(KST).strftime("%Y%m%d_%H%M")
                    base = safe_name(Path(it.filename).stem, 60)
                    ext = Path(it.filename).suffix.lower() or ".png"
                    name = f"{stamp}_{it.author}_{it.uid}_{base}{ext}"
                    folder = workdir / it.author if job.by_author else workdir
                    folder.mkdir(parents=True, exist_ok=True)
                    dest = folder / name
                    n = 1
                    while dest.exists():
                        n += 1
                        dest = folder / f"{stamp}_{it.author}_{it.uid}_{base}_{n}{ext}"
                    dest.write_bytes(data)
                    it.size = len(data)
                    ok += 1
            now = time.time()
            if now - last_tick > 2 or done == total:
                last_tick = now
                await progress(f"다운로드 {done}/{total}… 재촉하지 마!")

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
    if len(zips) > 1:
        first = zips[0]
        renamed = outdir / f"{stem}_part1.zip"
        first.rename(renamed)
        zips[0] = renamed
    return zips


def result_embed(job: Job, ok: int, total_bytes: int, links: list, dup: int, fail: int) -> discord.Embed:
    first = min(it.created for it in job.items).astimezone(KST)
    last = max(it.created for it in job.items).astimezone(KST)
    expire = (dt.datetime.now(KST) + dt.timedelta(hours=LINK_TTL_HOURS)).strftime("%m/%d %H:%M")
    emb = discord.Embed(
        title=f"📦 #{job.channel.name} 저장 완료 {kao()}",
        description=f"**{ok}장** · {human(total_bytes)} · {job.label}\n{first:%Y-%m-%d} ~ {last:%Y-%m-%d}",
        colour=0xF6A6C1,
    )
    for name, url, sz in links:
        emb.add_field(name=name, value=f"[⬇ 다운로드]({url}) · {human(sz)}", inline=False)
    foot = f"링크 만료 {expire} (KST)"
    if dup or fail:
        foot += f" · 중복 {dup} · 실패 {fail}"
    emb.set_footer(text=foot)
    return emb


async def run_job(inter: discord.Interaction, job: Job, announce: Optional[str] = None):
    """공개 채팅으로 진행 상황 갱신하며 작업 실행."""
    head = f"{inter.user.mention} {announce}\n" if announce else f"{inter.user.mention} "
    msg = await inter.followup.send(head + say("start"), wait=True)
    state = {"text": ""}

    async def progress(text: str):
        if text == state["text"]:
            return
        state["text"] = text
        try:
            await msg.edit(content=f"{head}⏳ **#{job.channel.name}** · {text} {kao()}")
        except discord.HTTPException:
            pass

    if client.job_lock.locked():
        client.waiting += 1
        await progress(say("queue", n=client.waiting))
    async with client.job_lock:
        client.waiting = max(0, client.waiting - 1)
        tmp = Path(tempfile.mkdtemp(prefix="saver_"))
        try:
            await progress("메시지 뒤지는 중…")
            last_id = await collect(job, progress)
            if not job.items:
                if last_id:
                    cursor_set(job.channel.id, job.user.id, last_id)
                await msg.edit(content=f"{head}{say('empty')}")
                return

            work = tmp / "files"
            work.mkdir()
            ok, dup, fail = await download_all(job, work, progress)
            ok = sum(1 for f in work.rglob("*") if f.is_file())
            if ok == 0:
                await msg.edit(content=f"{head}{say('error')} (실패 {fail}, 중복 {dup}) 커서는 그대로 뒀어.")
                return

            await progress("ZIP으로 묶는 중…")
            stamp = dt.datetime.now(KST).strftime("%Y%m%d_%H%M%S")
            stem = f"{safe_name(job.channel.name)}_{stamp}"
            zips = await asyncio.to_thread(make_zips, work, tmp, stem)

            links = []
            total_bytes = 0
            for i, z in enumerate(zips, 1):
                key = f"{job.channel.guild.id}/{job.channel.id}/{job.user.id}/{z.name}"
                url = await upload_with_progress(z, key, progress, f"({i}/{len(zips)})")
                sz = z.stat().st_size
                total_bytes += sz
                db.execute(
                    "INSERT OR REPLACE INTO uploads(key,user_id,channel_id,size,expires_at) VALUES(?,?,?,?,?)",
                    (key, job.user.id, job.channel.id, sz, time.time() + LINK_TTL_HOURS * 3600),
                )
                links.append((z.name, url, sz))
            db.commit()
            cursor_set(job.channel.id, job.user.id, last_id)

            done_text = f"{inter.user.mention} {say('done')}\n{say('expire', h=LINK_TTL_HOURS)}"
            await msg.edit(content=done_text, embed=result_embed(job, ok, total_bytes, links, dup, fail))
        except discord.Forbidden:
            await msg.edit(content=f"{head}이 채널은 이몸이 못 봐! 권한 좀 줘! {kao()}")
        except Exception as e:  # noqa: BLE001
            log.exception("작업 실패")
            await msg.edit(content=f"{head}{say('error')}\n`{type(e).__name__}: {e}`")
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
            return dt.datetime.strptime(s, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def cursor_line(ch: discord.TextChannel, user_id: int) -> tuple[Optional[int], str]:
    cur = cursor_get(ch.id, user_id)
    if cur:
        return cur, say("cursor", d=cursor_date(cur))
    return None, say("nocursor")


@client.tree.command(name="저장", description="마지막 저장 지점 이후 이미지를 ZIP으로 묶어 링크로 줍니다")
@app_commands.describe(
    채널="대상 채널 (비우면 내 방 → 없으면 현재 채널)",
    부터="이 날짜부터 (예: 2026-08-01). 저장 지점 무시",
    전체="채널 전체 처음부터 (저장 지점 무시)",
    내것만="내가 올린 것만",
    작성자별="작성자별 폴더로 나누기",
)
async def save_cmd(
    inter: discord.Interaction,
    채널: Optional[discord.TextChannel] = None,
    부터: Optional[str] = None,
    전체: bool = False,
    내것만: bool = False,
    작성자별: bool = False,
):
    await inter.response.defer(thinking=True)
    ch = resolve_channel(inter, 채널)
    announce = None
    if 전체:
        after_id, label = None, "전체"
    elif 부터:
        d = parse_date(부터)
        if not d:
            await inter.followup.send(f"{inter.user.mention} 날짜는 2026-08-01 처럼 써! {kao()}")
            return
        after_id, label = discord.utils.time_snowflake(d), f"{d:%Y-%m-%d}부터"
    else:
        after_id, announce = cursor_line(ch, inter.user.id)
        label = "이어서" if after_id else "처음부터"
    if 내것만:
        label += " · 내 것만"
    await run_job(inter, Job(inter.user, ch, after_id, 내것만, 작성자별, label), announce)


@client.tree.command(name="이어저장", description="마지막 저장 지점부터 이어서 저장합니다")
@app_commands.describe(채널="대상 채널 (비우면 내 방 → 현재 채널)", 작성자별="작성자별 폴더로 나누기")
async def continue_cmd(inter: discord.Interaction, 채널: Optional[discord.TextChannel] = None, 작성자별: bool = False):
    await inter.response.defer(thinking=True)
    ch = resolve_channel(inter, 채널)
    after_id, announce = cursor_line(ch, inter.user.id)
    await run_job(inter, Job(inter.user, ch, after_id, False, 작성자별, "이어서" if after_id else "처음부터"), announce)


@client.tree.command(name="저장시점", description="이 채널에서 내 마지막 저장 지점을 알려줍니다")
@app_commands.describe(채널="대상 채널 (비우면 내 방 → 현재 채널)")
async def status_cmd(inter: discord.Interaction, 채널: Optional[discord.TextChannel] = None):
    ch = resolve_channel(inter, 채널)
    _, line = cursor_line(ch, inter.user.id)
    await inter.response.send_message(f"{inter.user.mention} **#{ch.name}** — {line}")


@client.tree.command(name="저장초기화", description="저장 지점을 지웁니다. 다음 /저장 은 처음부터")
@app_commands.describe(채널="대상 채널", 멤버="다른 멤버 것 초기화 (관리자만)")
async def reset_cmd(inter: discord.Interaction, 채널: Optional[discord.TextChannel] = None, 멤버: Optional[discord.Member] = None):
    ch = resolve_channel(inter, 채널)
    target = inter.user
    if 멤버 and 멤버.id != inter.user.id:
        if not inter.user.guild_permissions.administrator:
            await inter.response.send_message(f"남의 기록은 관리자만 지울 수 있어! {kao()}", ephemeral=True)
            return
        target = 멤버
    n = cursor_del(ch.id, target.id)
    who = "" if target.id == inter.user.id else f"{target.display_name} 님 "
    await inter.response.send_message(
        f"{inter.user.mention} **#{ch.name}** {who}{say('reset') if n else '기록이 원래 없었어! ' + kao()}"
    )


HELP_INTRO = [
    "뭐야, 사용법도 몰라? …할 수 없지, 이몸이 딱 한 번만 알려준다! 잘 들어!",
    "또 물어보네… 알았어, 알았다고! 이번엔 제대로 외워!",
    "흥, 설명서까지 이몸이 읽어줘야 해? …자, 잘 봐!",
]

HELP_FIELDS = [
    ("📦 /저장",
     "네 방에 올라온 이미지, **마지막으로 저장한 데 이후부터** 싹 긁어서 ZIP 링크로 준다!\n"
     "다른 방도 올린 사람이 누구든 그 방 거면 다 챙겨. 자기 방에서 그냥 치면 돼!\n"
     "옵션 붙이고 싶으면 → `채널:#방이름` 다른 방 / `부터:2026-08-01` 그 날짜부터 / "
     "`전체:True` 처음부터 전부 / `내것만:True` 네가 올린 것만 / `작성자별:True` 올린 사람별 폴더"),
    ("⏩ /이어저장",
     "/저장이랑 똑같은데, **마지막 저장시점이 언제였는지** 먼저 말해주고 이어간다. 기억 안 나는 녀석용!"),
    ("📍 /저장시점",
     "저장은 안 하고 **마지막 저장시점만** 알려줘. 확인만 하고 싶을 때!"),
    ("🚩 /여기까지",
     "다운로드 없이 **저장시점만 지금으로** 맞춘다. 이미 딴 데서 받아둔 거라 건너뛰고 싶을 때 써!"),
    ("🔗 /링크",
     "링크 놓쳤어? **아직 안 죽은 링크** 다시 준다. 근데 만료 지나면 이몸도 몰라!"),
    ("🔄 /저장초기화",
     "저장시점 싹 지운다. 다음 /저장은 **처음부터** 다시야!"),
    ("⏰ 링크 만료",
     "링크는 **{h}시간**짜리! 그 안에 안 받으면 사라진다! 코롸ㅡ!"),
    ("👑 관리자 전용",
     "`/주인 지정 채널 멤버` 방 주인 등록 → 그 사람은 어디서 /저장 쳐도 자기 방이 저장돼\n"
     "`/주인 목록` `/주인 해제` / `/전체저장` 카테고리 안 방 전부 한 번에"),
]


@client.tree.command(name="설명", description="세이버 사용법을 알려줍니다")
async def help_cmd(inter: discord.Interaction):
    emb = discord.Embed(
        title=f"세이버 사용법 {kao()}",
        description=random.choice(HELP_INTRO),
        colour=0xF6A6C1,
    )
    for name, value in HELP_FIELDS:
        emb.add_field(name=name, value=value.format(h=LINK_TTL_HOURS), inline=False)
    emb.set_footer(text="파일명은 날짜_시간_올린사람_ID_원본명 순서야! 잘 정리해 줬으니 고마워하라고!")
    await inter.response.send_message(f"{inter.user.mention} {kao()}", embed=emb)


@client.tree.command(name="여기까지", description="다운로드 없이 저장 지점만 지금(채널 최신 메시지)으로 맞춥니다")
@app_commands.describe(채널="대상 채널 (비우면 내 방 → 현재 채널)")
async def mark_cmd(inter: discord.Interaction, 채널: Optional[discord.TextChannel] = None):
    await inter.response.defer(thinking=True)
    ch = resolve_channel(inter, 채널)
    last = ch.last_message_id
    if not last:
        async for m in ch.history(limit=1):
            last = m.id
    if not last:
        await inter.followup.send(f"{inter.user.mention} 채널이 텅 비었는데? {kao()}")
        return
    cursor_set(ch.id, inter.user.id, last)
    await inter.followup.send(f"{inter.user.mention} **#{ch.name}** {say('mark')} ({cursor_date(last)})")


@client.tree.command(name="링크", description="아직 만료 안 된 다운로드 링크를 다시 받습니다")
@app_commands.describe(채널="대상 채널 (비우면 내 방 → 현재 채널)")
async def links_cmd(inter: discord.Interaction, 채널: Optional[discord.TextChannel] = None):
    await inter.response.defer(thinking=True)
    ch = resolve_channel(inter, 채널)
    now = time.time()
    rows = db.execute(
        "SELECT key,size,expires_at FROM uploads WHERE user_id=? AND channel_id=? AND expires_at>? ORDER BY key",
        (inter.user.id, ch.id, now),
    ).fetchall()
    if not rows:
        await inter.followup.send(f"{inter.user.mention} {say('nolinks')}")
        return
    emb = discord.Embed(title=f"📦 #{ch.name} 링크 재발급 {kao()}", colour=0xF6A6C1)
    for key, size, exp in rows:
        url = await asyncio.to_thread(r2_presign, key, int(exp - now))
        left = max(1, int((exp - now) / 60))
        emb.add_field(name=Path(key).name, value=f"[⬇ 다운로드]({url}) · {human(size)} · {left}분 남음", inline=False)
    await inter.followup.send(f"{inter.user.mention} {say('links')}", embed=emb)


owner_group = app_commands.Group(name="주인", description="채널 주인 매핑 (관리자)", default_permissions=discord.Permissions(administrator=True))


@owner_group.command(name="지정", description="채널 주인을 지정합니다. 그 멤버는 /저장 만 쳐도 자기 방이 선택됩니다")
@app_commands.describe(채널="채널", 멤버="주인")
async def owner_set_cmd(inter: discord.Interaction, 채널: discord.TextChannel, 멤버: discord.Member):
    owner_set(채널.id, 멤버.id)
    await inter.response.send_message(f"**#{채널.name}** 주인 = {멤버.display_name}! 기억해 뒀어! {kao()}", ephemeral=True)


@owner_group.command(name="목록", description="채널 주인 목록")
async def owner_list_cmd(inter: discord.Interaction):
    rows = db.execute("SELECT channel_id,user_id FROM owners").fetchall()
    if not rows:
        await inter.response.send_message(f"등록된 주인 없어! {kao()}", ephemeral=True)
        return
    lines = []
    for cid, uid in rows:
        ch = inter.guild.get_channel(cid)
        m = inter.guild.get_member(uid)
        lines.append(f"#{ch.name if ch else cid} → {m.display_name if m else uid}")
    await inter.response.send_message("\n".join(lines), ephemeral=True)


@owner_group.command(name="해제", description="채널 주인 매핑 해제")
@app_commands.describe(채널="채널")
async def owner_remove_cmd(inter: discord.Interaction, 채널: discord.TextChannel):
    n = db.execute("DELETE FROM owners WHERE channel_id=?", (채널.id,)).rowcount
    db.commit()
    await inter.response.send_message(f"**#{채널.name}** {'주인 해제했어!' if n else '주인 없었는데?'} {kao()}", ephemeral=True)


client.tree.add_command(owner_group)


@client.tree.command(name="전체저장", description="현재 카테고리 안 모든 채널을 각각 ZIP으로 저장 (관리자)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(전체="저장 지점 무시하고 처음부터")
async def save_all_cmd(inter: discord.Interaction, 전체: bool = False):
    await inter.response.defer(thinking=True)
    cat = getattr(inter.channel, "category", None)
    chans = [c for c in (cat.text_channels if cat else inter.guild.text_channels)
             if c.permissions_for(inter.guild.me).read_message_history]
    await inter.followup.send(f"{inter.user.mention} 채널 {len(chans)}개 전부?! …알았어, 차례로 간다! {kao()}")
    for ch in chans:
        after_id = None if 전체 else cursor_get(ch.id, inter.user.id)
        job = Job(inter.user, ch, after_id, False, True, "전체" if 전체 else ("이어서" if after_id else "처음부터"))
        await run_job(inter, job)


@client.tree.error
async def on_app_error(inter: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("명령 오류", exc_info=error)
    text = f"{say('error')}\n`{type(error).__name__}: {error}`"
    if inter.response.is_done():
        await inter.followup.send(text)
    else:
        await inter.response.send_message(text)


@client.event
async def on_ready():
    log.info("로그인: %s (%s) · 서버 %d개", client.user, client.user.id, len(client.guilds))


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
