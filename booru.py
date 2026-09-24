"""
booru-v5 태그 데이터 로더 — /야차 · /폭력 풀.
https://booru-v5.github.io/data/tags.json.gz (단보루 태그 45k, 한글 번역·설명·주제 분류·성인 목록)
https://booru-v5.github.io/data/related.json.gz (연관 태그)
24시간 캐시. 실패 시 None → bot.py가 내장 풀로 폴백.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import random
import re
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("saver.booru")

BASE = "https://booru-v5.github.io/data/"
CACHE_TTL = 24 * 3600

# ── 어디서도 금지 (미성년·수간·고어·배설·약물) ──
BAN_RE = re.compile(
    r"loli|shota|child|toddler|baby|infant|kindergarten|elementary|\bcub\b|age_difference|"
    r"bestiality|zoophilia|pokephilia|feral|knotting|sex_with_insects|animal_penis|teratophilia|pawpad|breeding_mount|furry|anthro|"
    r"guro|corpse|death|dead|decapitat|severed|dismember|intestine|organ|entrail|brain|amput|bisect|"
    r"cannibal|torture|suicide|self-harm|wrist_cut|impal|crucifix|execution|hanged|murder|headshot|"
    r"crush|drown|strangl|asphyx|glasgow|exposed_muscle|flesh\b|"
    r"scat|feces|poop|diaper|vomit|peeing|pee\b|urin|piss|"
    r"drugged|aphrodisiac|drunk|"
    r"necro|vore|inflation|giving_birth|pregnan|egg_laying|egg_implant|umbilical|birth|"
    r"cuntboy|newhalf|futa|yaoi|yuri|pointless_condom|price_list|paizuri_day|okamoto|condom_box"
)
# 성인 풀에서 제외 (페티시 분류 통째 + 폭력 계열은 /폭력 전용)
FETISH_TOPICS = {"성인용 → 페티시", "성인용 → 생물"}
VIOLENCE_TOPIC = "성인용 → 폭력성"
# 비동의·강압 계열 → /폭력 전용
NONCON_RE = re.compile(
    r"rape|forced|molest|chikan|harass|stalk|abuse|ryona|beaten|spank|humiliat|slap|"
    r"imminent_gangbang|public_use|cumdump|emotionless|netorare|ntr|cheating|prostitut|soapland|"
    r"tucked_money|stealth|sleep_|unwanted|defloration|mind_control|hypnosis|drugged|blackmail"
)
# 여성형 의상 (탑에게 금지)
FEM_WEAR_RE = re.compile(
    r"bra\b|bikini|lingerie|babydoll|pasties|maebari|panties|thong|g-string|c-string|garter|"
    r"bodystocking|dress|skirt|leotard|camisole|apron|slingshot|bridal|cupless|crotchless|"
    r"stockings|thighhigh|pantyhose|heels|nipple_ring|maid|nurse|ribbon|pearl|lace|frill|"
    r"breast|cleavage|pussy|vulva|clitor|cameltoe|areola|buruma|bikini_top|breasts_out|one_breast"
)
# 여성 신체 전제 태그 (탑에게 금지)
FEM_BODY_RE = re.compile(r"pussy|vulva|labia|clitor|cameltoe|areola|breast|cleavage|nipple_slip|vaginal|female_|lactat|milk|paizuri|cervi|uterus|womb")

# 주제 → 수위 매핑 (mild=0 hot=1 fire=2 hell=3)
TOPIC_LEVEL = {
    "성인용 → 노출": 0,
    "성인용 → 복장 및 악세서리": 0,
    "성인용 → 자세": 1,
    "성인용 → 상태 및 분위기 및 감정": 1,
    "성인용 → 신체": 1,
    "성인용 → 행위": 2,
    "성인용 → 액체": 2,
    "성인용 → 성인용품": 2,
    "성인용 → 기타": 2,
    "복장 및 악세서리 → 상태": 0,
    "포즈 → 어필 자세": 0,
    "인물 → 신체 상태 및 변형": 1,
}
# 행위 중 순한 것들(키스·포옹·애무)은 매운맛으로
SOFT_ACT_RE = re.compile(r"^(kiss|french_kiss|hug|groping|nipple_tweak|nipple_stimulation|breast_sucking|licking|foreplay|"
                         r"caress|imminent_kiss|implied_sex|after_sex|afterglow|thigh_sex|grinding|humping|dry_humping|frottage|"
                         r"licking_nipple|licking_breast|nipple_rub|breast_smother|face_between_breasts|head_between_breasts)")


class Booru:
    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ok = False
        self.loaded_at = 0.0
        self.tags: list[dict] = []       # 전체 성인 태그 항목 {n,k,d,topic,level,related:[…]}
        self.by_tag: dict[str, dict] = {}
        self.pool: dict[str, list[dict]] = {}   # level별
        self.violence: list[dict] = []
        self.noncon: list[dict] = []
        self.fetish_count = 0
        self.rel_t: list[str] = []
        self.rel_m: list[int] = []
        self.rel_r: dict = {}
        self.rel_idx: dict[str, int] = {}

    # ── 다운로드/캐시 ──
    def _get(self, name: str) -> dict | None:
        p = self.dir / name
        if not p.exists() or time.time() - p.stat().st_mtime > CACHE_TTL:
            try:
                req = urllib.request.Request(BASE + name, headers={"User-Agent": "saver-bot/1.0"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    p.write_bytes(r.read())
                log.info("booru %s 갱신", name)
            except Exception as e:  # noqa: BLE001
                log.warning("booru %s 다운로드 실패: %s", name, e)
                if not p.exists():
                    return None
        try:
            return json.load(gzip.open(p, "rt", encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("booru %s 파싱 실패: %s", name, e)
            return None

    def load(self) -> bool:
        d = self._get("tags.json.gz")
        if not d:
            return False
        n, k, t, ad, topics, desc = d["n"], d["k"], d["t"], d["ad"], d["topics"], d["d"]
        r = self._get("related.json.gz")
        if r:
            self.rel_t, self.rel_m, self.rel_r = r["t"], r["m"], r["r"]
            self.rel_idx = {tag: i for i, tag in enumerate(self.rel_t)}
        self.tags, self.by_tag, self.violence, self.noncon = [], {}, [], []
        self.pool = {0: [], 1: [], 2: [], 3: []}
        self.fetish_count = 0
        for i in ad:
            tag = n[i]
            topic = topics[t[i]] if 0 <= t[i] < len(topics) else ""
            item = {"n": tag, "k": k[i] or tag, "d": (desc[i] or "")[:140], "topic": topic}
            if BAN_RE.search(tag):
                continue
            if topic == VIOLENCE_TOPIC:
                self.violence.append(item)
                continue
            if NONCON_RE.search(tag):
                self.noncon.append(item)
                continue
            if topic in FETISH_TOPICS:
                self.fetish_count += 1
                continue
            lvl = TOPIC_LEVEL.get(topic)
            if lvl is None:
                continue
            if topic == "성인용 → 행위" and SOFT_ACT_RE.search(tag):
                lvl = 1
            item["level"] = lvl
            self.tags.append(item)
            self.by_tag[tag] = item
            self.pool[lvl].append(item)
        self.ok = len(self.tags) > 200
        self.loaded_at = time.time()
        log.info("booru 성인 %d (순%d 매%d 불%d) · 폭력 %d · 비동의 %d · 페티시 제외 %d",
                 len(self.tags), len(self.pool[0]), len(self.pool[1]), len(self.pool[2]), len(self.violence), len(self.noncon), self.fetish_count)
        return self.ok

    def maybe_refresh(self):
        if time.time() - self.loaded_at > CACHE_TTL:
            try:
                self.load()
            except Exception:  # noqa: BLE001
                log.exception("booru 재로드 실패")

    # ── 조회 ──
    def top_ok(self, tag: str) -> bool:
        return not (FEM_WEAR_RE.search(tag) or FEM_BODY_RE.search(tag))

    def related(self, tag: str, limit: int = 12, adult_only: bool = True) -> list[dict]:
        """연관 태그(성인 풀 안에서만, 금지 제외). related.json 삼중항 (원본idx, 연관idx?, 점수) 중 첫 값이 태그 인덱스."""
        i = self.rel_idx.get(tag)
        if i is None:
            return []
        key = str(self.rel_m[i]) if i < len(self.rel_m) else None
        v = self.rel_r.get(key) if key else None
        if not v:
            return []
        out = []
        for j in range(0, len(v), 3):
            a = v[j]
            if a >= len(self.rel_t):
                continue
            rt = self.rel_t[a]
            if rt == tag:
                continue
            it = self.by_tag.get(rt)
            if it is None:
                if adult_only:
                    continue
                if BAN_RE.search(rt) or NONCON_RE.search(rt):
                    continue
                it = {"n": rt, "k": rt, "d": "", "topic": "", "level": -1}
            out.append(it)
            if len(out) >= limit:
                break
        return out

    def pick(self, level: int, k: int, exclude: set[str] | None = None, top: bool = False, topic_in: set[str] | None = None) -> list[dict]:
        """level 이하 풀에서 k개. top=True면 여성형 의상·신체 제외."""
        cand = [it for l in range(level + 1) for it in self.pool[l]]
        if topic_in:
            cand = [it for it in cand if it["topic"] in topic_in]
        if top:
            cand = [it for it in cand if self.top_ok(it["n"])]
        if exclude:
            cand = [it for it in cand if it["n"] not in exclude]
        if not cand:
            return []
        return random.sample(cand, min(k, len(cand)))


def fmt(items: list[dict]) -> tuple[str, str]:
    """(한글 ' + ' 연결, 태그 ', ' 연결)"""
    return " + ".join(x["k"] for x in items), ", ".join(x["n"] for x in items)
