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
    r"cuntboy|newhalf|futa|yaoi|yuri|pointless_condom|price_list|paizuri_day|okamoto|condom_box|"
    r"lactat|breastfeed|nursing|milk|slime|tentacle|bondage|bdsm|restrain|shibari|\brope|gag\b|gagged|leash|collar|cuff|"
    r"insect|worm|parasite|egg|ovipos|birth|hyper|gigantic|huge_|inflat|bulge_(?!press)|stomach_bulge|x-ray|cross-section|"
    r"wedgie|smell|sniff|foot|feet|toe|armpit|hair_?job|tail_?job|onahole|fleshlight|strap-on|pegging|rimming|anilingus|prostate"
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
FEM_BODY_RE = re.compile(r"pussy|vulva|labia|clitor|cameltoe|areola|breast|cleavage|nipple_slip|vaginal|female_|lactat|milk|paizuri|cervi|uterus|womb|"
                         r"spreading_own|presenting|_peek$|_slip$|self_|own_|_aside$|_lift$|_pull$|panty|butt_crack|anus_|assisted")

# 신체 분류는 기본 부위만 (색·크기·피어싱·변형 등 페티시성 전부 제외)
BODY_ALLOW = {"nipples", "penis", "pussy", "pubic_hair", "anus", "erection", "testicles", "bulge", "clitoris",
              "erection_under_clothes", "flaccid", "half-erect", "foreskin", "perineum"}
# 그림 내용이 아닌 메타·검열·인원 태그 (연관 태그에서 걸러냄)
META_RE = re.compile(r"censor|uncensored|^hetero$|^\d(girl|boy)s?$|multiple_|solo_focus|^pov|faceless|comic|monochrome|greyscale|"
                     r"threesome|gangbang|group_sex|orgy|foursome|fivesome|spitroast|surrounded|incest|bisexual|femdom|"
                     r"^breasts$|large_breasts|huge_breasts|small_breasts|navel|^ass$|^tongue$|motion_lines|^blush$|^sweat$|^saliva$|"
                     r"bed_sheet|pillow$|^lying$|^sex$|^oral$|^licking$|^vaginal$|^anal$|^penis$|^pussy$|^nipples$|^testicles$|^anus$|"
                     r"^nude$|^completely_nude$|^clothed_sex$|deep_skin|teamwork|cooperative|^hug$|^couple$|^kiss$")

# 주제 → 수위 매핑 (mild=0 hot=1 fire=2 hell=3)
TOPIC_LEVEL = {
    "성인용 → 노출": 0,
    "성인용 → 복장 및 악세서리": 0,
    "성인용 → 자세": 1,
    "성인용 → 상태 및 분위기 및 감정": 1,
    "성인용 → 신체": 1,  # BODY_ALLOW만
    "성인용 → 행위": 2,
    "성인용 → 액체": 2,
    "성인용 → 성인용품": 2,
    "성인용 → 기타": 2,
    "복장 및 악세서리 → 상태": 0,
    "포즈 → 어필 자세": 0,
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
            item = {"n": tag, "k": k[i] or nai(tag), "d": (desc[i] or "")[:140], "topic": topic}
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
            if topic == "성인용 → 신체" and tag not in BODY_ALLOW:
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


def nai(tag: str) -> str:
    """단보루 태그 → NAI 문법: 언더바 제거, _(sex) 같은 꼬리 제거."""
    t = re.sub(r"_\([^)]*\)$", "", tag)
    return t.replace("_", " ")


def fmt(items: list[dict]) -> tuple[str, str]:
    """(한글 ' · ' 연결, NAI 태그 ', ' 연결)"""
    return " · ".join(x["k"] for x in items), ", ".join(nai(x["n"]) for x in items)


# ── 역할 판정 (연관 태그 → 탑/바텀/구도/행위/액체) ──
POSE_RE = re.compile(r"on_back|on_stomach|on_side|all_fours|bent_over|kneeling|squatting|sitting|standing|straddl|spread_legs|"
                     r"m_legs|legs_up|leg_lift|leg_lock|prone|arched|folded|top-down|upright|girl_on_top|boy_on_top|"
                     r"_position$|missionary|doggystyle|mating_press|suspended|piledriver|amazon|full_nelson|lotus|wheelbarrow|"
                     r"from_behind|from_above|from_below|from_side|between_|face_down|head_between|sitting_on")
TOP_RE = re.compile(r"grabbing_another|grabbing_from|hand_on_another|hands_on_another|holding_another|holding_leg|torso_grab|arm_held|"
                    r"pinning|pinned|looming|thrusting|guided|guiding|licking_|sucking|biting|pulling_another|hair_pull|"
                    r"hand_under|caress|spanking|fingering|nipple_tweak|groping|breast_grab|ass_grab|head_grab|chin_grab|"
                    r"wrist_grab|arm_grab|leg_grab|thigh_grab|hand_on_head|hands_on_head|hand_in_another|dominat|mounting|humping|"
                    r"cum_on|facial|bukkake|ejaculat|pull_out|holding_own_penis|hands_on_hips|hips_grab")
BOT_RE = re.compile(r"ahegao|torogao|fucked_silly|rolling_eyes|tears|crying|trembling|pleading|orgasm|female_orgasm|afterglow|"
                    r"pillow_grab|sheet_grab|head_on_pillow|grabbing_own|spreading_own|presenting|arched_back|legs_up|spread_legs|"
                    r"m_legs|on_back|all_fours|bent_over|clothing_aside|panties_aside|_aside$|_lift$|_pull$|_slip$|breasts_out|"
                    r"bouncing|open_mouth|tongue_out|heavy_breathing|drooling|clenched|biting_lip|half-closed|covering_face|"
                    r"embarrassed|nervous|scared|surprised|wavy_mouth|heart-shaped|leg_lock|arms_up|arms_behind|restrained|bound|"
                    r"wet_|stained|cum_in|cumdrip|overflow|pussy_juice|throat|gaping|imminent")
FLUID_RE = re.compile(r"cum|juice|saliva|sweat|wet|drool|lactat|milk|squirt|ejaculat|overflow|stain|puddle|trail")
POSTURE_GROUPS = [
    re.compile(r"standing|upright|suspended|wall"),
    re.compile(r"on_back|lying|missionary|mating_press|legs_up|m_legs|folded|piledriver|amazon"),
    re.compile(r"all_fours|bent_over|doggystyle|from_behind|prone|top-down|face_down"),
    re.compile(r"sitting|straddl|cowgirl|girl_on_top|boy_on_top|lotus|lap"),
]
NUDE_RE = re.compile(r"^nude$|completely_nude|^bottomless$|^topless$|naked_")
CLOTHED_RE = re.compile(r"clothed|_aside|_lift$|_pull$|_slip$|_peek$|through_clothes|under_clothes|over_clothes|panties|bra|bikini|"
                        r"lingerie|garter|stocking|thighhigh|pantyhose|sweater|shirt|skirt|dress|uniform|apron|swimsuit|leotard")


def _posture_group(tag: str):
    for i, g in enumerate(POSTURE_GROUPS):
        if g.search(tag):
            return i
    return None


class Scene:
    """앵커 행위 + 연관 태그로 서로 맞물리는 장면 구성."""

    def __init__(self, B: "Booru", level: int):
        self.B, self.level = B, level
        self.used: set[str] = set()
        self.posture = None
        self.dress = None  # 'nude' | 'clothed'

    def ok(self, tag: str, top: bool = False) -> bool:
        if tag in self.used or META_RE.search(tag) or BAN_RE.search(tag) or NONCON_RE.search(tag):
            return False
        it = self.B.by_tag.get(tag)
        if it is None:
            return False
        if it["topic"] in FETISH_TOPICS or it["topic"] == VIOLENCE_TOPIC:
            return False
        if it["topic"] == "성인용 → 신체" and tag not in BODY_ALLOW:
            return False
        if top and not self.B.top_ok(tag):
            return False
        if it.get("level", 0) > self.level:
            return False
        g = _posture_group(tag)
        if g is not None and self.posture is not None and g != self.posture:
            return False
        if self.dress == "nude" and CLOTHED_RE.search(tag) and not NUDE_RE.search(tag):
            return False
        if self.dress == "clothed" and NUDE_RE.search(tag):
            return False
        return True

    def add(self, tag: str):
        self.used.add(tag)
        g = _posture_group(tag)
        if g is not None and self.posture is None:
            self.posture = g
        if self.dress is None:
            if NUDE_RE.search(tag):
                self.dress = "nude"
            elif CLOTHED_RE.search(tag):
                self.dress = "clothed"
        return self.B.by_tag[tag]

    def ext_ok(self, tag: str) -> bool:
        if tag in self.used:
            return False
        g = _posture_group(tag)
        if g is not None and self.posture is not None and g != self.posture:
            return False
        if self.dress == "nude" and CLOTHED_RE.search(tag) and not NUDE_RE.search(tag):
            return False
        if self.dress == "clothed" and NUDE_RE.search(tag):
            return False
        it = self.B.by_tag.get(tag)
        if it and it.get("level", 0) > self.level:
            return False
        if it is None and self.level < 2 and re.search(r"lube|vibrator|dildo|condom|handcuff|sex_toy|whip", tag):
            return False
        return True

    def from_ext(self, pairs: list, k: int) -> list[dict]:
        """봇 내장 (tag, kor) 목록에서 장면과 안 부딪히는 것 k개."""
        cand = list(pairs)
        random.shuffle(cand)
        out = []
        for t, kk in cand:
            if len(out) >= k:
                break
            if not self.ext_ok(t):
                continue
            self.used.add(t)
            g = _posture_group(t)
            if g is not None and self.posture is None:
                self.posture = g
            if self.dress is None:
                if NUDE_RE.search(t):
                    self.dress = "nude"
                elif CLOTHED_RE.search(t):
                    self.dress = "clothed"
            out.append({"n": t, "k": kk, "d": "", "topic": ""})
        return out

    def _take(self, cand: list[str], k: int, top: bool) -> list[dict]:
        random.shuffle(cand)
        out = []
        for t in cand:
            if len(out) >= k:
                break
            if self.ok(t, top):
                out.append(self.add(t))
        return out

    def from_related(self, anchor: str, pattern, k: int, top: bool = False, limit: int = 80) -> list[dict]:
        cand = [r["n"] for r in self.B.related(anchor, limit, adult_only=True) if pattern.search(r["n"])]
        return self._take(cand, k, top)

    def from_pool(self, topics: set[str], k: int, top: bool = False, pattern=None) -> list[dict]:
        cand = [it["n"] for l in range(self.level + 1) for it in self.B.pool[l]
                if it["topic"] in topics and (pattern is None or pattern.search(it["n"]))]
        return self._take(cand, k, top)


T_EXPO = {"성인용 → 노출", "복장 및 악세서리 → 상태"}
T_WEAR = {"성인용 → 복장 및 악세서리"}
T_POSE = {"성인용 → 자세", "포즈 → 어필 자세"}
T_MOOD = {"성인용 → 상태 및 분위기 및 감정"}
T_ACT = {"성인용 → 행위"}
T_FLUID = {"성인용 → 액체"}
T_TOY = {"성인용 → 성인용품", "성인용 → 기타"}


def roll_scene(B: "Booru", level: int, ext: dict | None = None) -> list[tuple[str, list[dict]]]:
    """[(구역 라벨, items)] — level 0/1은 1인, 2/3은 커플(구도·행위·탑·바텀).
    ext = {face, face_top, face_bot, pose2, place, extra}: 봇 내장 (tag,kor) 목록 (장면 검사 통과시켜 사용)."""
    ext = ext or {}
    sc = Scene(B, level)
    E = lambda key, k: sc.from_ext(ext.get(key, []), k)
    if level < 2:
        anchor = sc.from_pool(T_EXPO if level == 0 else T_POSE | T_EXPO, 1)
        a = anchor[0]["n"] if anchor else None
        wear = (sc.from_related(a, CLOTHED_RE, 1) if a else []) or sc.from_pool(T_WEAR | T_EXPO, 1)
        pose = sc.from_related(a, POSE_RE, 1) if a else []
        pose = pose or sc.from_pool(T_POSE, 1)
        mood = sc.from_pool(T_MOOD, 1) if level == 1 else []
        body = sc.from_pool({"성인용 → 신체"}, 1) if level == 1 else []
        face = E("face", 1)
        return [("👤 인물", anchor + wear + body), ("🎬 구도", pose), ("💭 상태", mood + face), ("✨ 연출", E("place", 1) + E("extra", 1))]
    # ── 커플 ──
    hard = level >= 3
    acts = sc.from_pool(T_ACT, 1, pattern=None if hard else re.compile(r"^(?!.*(double|triple|multiple|fisting|prolapse|insertion|large_insertion)).*$"))
    if not acts:
        return [("🔥 행위", [])]
    a = acts[0]["n"]
    # 구도: 앵커의 연관 자세 1 (없으면 풀)
    pose = sc.from_related(a, POSE_RE, 1) or sc.from_pool(T_POSE, 1)
    # 행위 보강: 연관 행위 1 (지옥맛 2)
    acts += sc.from_related(a, re.compile(r".*"), 2 if hard else 1, limit=40) and [] or []
    heavy = re.compile(r"double|triple|multiple|fisting|prolapse|insertion|gaping")
    more = [r["n"] for r in B.related(a, 40) if B.by_tag.get(r["n"], {}).get("topic") in T_ACT and (hard or not heavy.search(r["n"]))]
    acts += sc._take(more, 2 if hard else 1, False)
    # 액체·토이: 연관 우선
    fluid = sc.from_related(a, FLUID_RE, 1) or sc.from_pool(T_FLUID | T_TOY, 1)
    if hard:
        fluid += sc.from_pool(T_TOY, 1)
    # 탑: 연관 중 능동 태그 + 노출 1 (여성형 제외)
    top = sc.from_related(a, TOP_RE, 2 if hard else 1, top=True)
    top += sc.from_pool(T_ACT | T_POSE, max(0, (2 if hard else 1) - len(top)), top=True, pattern=TOP_RE)
    top += sc.from_related(a, re.compile(r"^(?!.*(nude)).*"), 0)  # no-op placeholder
    top_wear = sc.from_related(a, CLOTHED_RE, 1, top=True) or sc.from_pool(T_EXPO | T_WEAR, 1, top=True)
    # 바텀: 연관 중 수동·반응 태그 + 노출 1
    bot = sc.from_related(a, BOT_RE, 2 if hard else 1)
    bot += sc.from_pool(T_POSE | T_MOOD | T_EXPO, max(0, (2 if hard else 1) - len(bot)), pattern=BOT_RE)
    bot_wear = sc.from_related(a, CLOTHED_RE, 1) or sc.from_pool(T_EXPO | T_WEAR, 1)
    mood = sc.from_related(a, re.compile(r".*"), 0)  # placeholder
    mood = [sc.add(t) for t in [r["n"] for r in B.related(a, 60) if B.by_tag.get(r["n"], {}).get("topic") in T_MOOD and sc.ok(r["n"])][:1]]
    pose2 = E("pose2", 1)
    face_t = E("face_top", 1)
    face_b = E("face_bot", 2 if hard else 1)
    return [("🎬 구도", pose2 + pose), ("🔥 행위", acts + fluid), ("🔝 탑", top_wear + top + face_t),
            ("🔻 바텀", bot_wear + bot + mood + face_b), ("✨ 연출", E("place", 1) + E("extra", 1))]
