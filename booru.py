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
    r"slime|tentacle|strap-on|strapon|pegging|harness|sexy_no_jutsu|genderswap|crossdress|otoko_no_ko|trapb|"
    r"insect|worm|parasite|egg|ovipos|birth|hyper|gigantic|huge_|inflat|bulge_(?!press)|stomach_bulge|x-ray|cross-section|"
    r"wedgie|smell|sniff|foot|feet|toe|armpit|hair_?job|tail_?job|onahole|fleshlight|rimming|anilingus|prostate"
)
# 성인 풀에서 제외 (페티시 분류 통째 + 폭력 계열은 /폭력 전용)
FETISH_TOPICS = {"성인용 → 페티시", "성인용 → 생물"}
FETISH_ALLOW_RE = re.compile(r"^(bound|bdsm|bondage|restrained|cuffs|leash|gag|gagged|bound_wrists|shibari|bound_arms|ball_gag|"
                             r"bound_legs|chain_leash|bound_ankles|chained|cloth_gag|breast_bondage|tape_gag|bit_gag|ribbon_bondage|"
                             r"crotch_rope|frogtie|suspension|rope|blindfold|collar|handcuffs|arms_behind_back|tied_up|bound_together|"
                             r"shibari_over_clothes|naked_ribbon|bondage_outfit)$")
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
            if topic in FETISH_TOPICS and not FETISH_ALLOW_RE.search(tag):
                self.fetish_count += 1
                continue
            lvl = 2 if topic in FETISH_TOPICS else TOPIC_LEVEL.get(topic)
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


# 성별 조합: HL(남탑·여바텀) / BL(남·남) / GL(여·여)
MALE_RE = re.compile(r"penis|testicle|erection|foreskin|flaccid|half-erect|\bbulge|precum|glans|balls|scrotum|"
                     r"\bcum\b|cum_|_cum|facial|bukkake|ejaculat|creampie|fellatio|irrumatio|deepthroat|handjob|paizuri|"
                     r"\bsex\b|^sex_|_sex$|vaginal|^anal$|anal_sex|penetrat|insertion|missionary|doggystyle|mating_press|"
                     r"prone_bone|cowgirl|amazon|piledriver|suspended_congress|spitroast|full_nelson|wheelbarrow|lotus_position|"
                     r"standing_sex|impregnat|condom|blowjob|oral$|licking_penis|male_")
FEMALE_RE = re.compile(r"pussy|vulva|labia|clitor|cameltoe|areola|breast|cleavage|nipple_slip|vaginal|female_|lactat|milk|nursing|"
                       r"breastfeed|paizuri|cervi|uterus|womb|cunnilingus|tribadism|scissor|bra\b|bikini|lingerie|babydoll|pasties|"
                       r"maebari|panties|panty|thong|g-string|c-string|garter|bodystocking|dress|skirt|leotard|camisole|slingshot|"
                       r"bridal|cupless|crotchless|stockings|thighhigh|pantyhose|heels|maid|nurse|frill|lace|buruma|girl|squirt|"
                       r"pussy_juice|cum_in_pussy|creampie|sideboob|underboob|virgin_killer|naked_apron|sitting_on_face")
FEM_TOP_RE = re.compile(r"cowgirl|girl_on_top|amazon|femdom|assertive_female|upright_straddle|sitting_on_(face|person|lap)|"
                        r"straddling|riding|reverse_suspended|face_sitting|thigh_straddling|dominatrix|lap")
PAIR = {
    "HL": {"common_ban": FEM_TOP_RE, "top_ban": FEMALE_RE, "bot_ban": re.compile(r"^(penis|testicles|erection|foreskin|flaccid|half-erect|bulge|erection_under_clothes|precum|male_pubic_hair)$"),
           "top_k": "남", "bot_k": "여", "count": "2people, couple, 1boy, 1girl"},
    "BL": {"common_ban": FEMALE_RE, "top_ban": FEMALE_RE, "bot_ban": FEMALE_RE,
           "top_k": "남", "bot_k": "남", "count": "2people, couple, 2boys"},
    "GL": {"common_ban": MALE_RE, "top_ban": MALE_RE, "bot_ban": MALE_RE,
           "top_k": "여", "bot_k": "여", "count": "2people, couple, 2girls"},
}
SOLO_BAN = {"HL": MALE_RE, "GL": MALE_RE, "BL": FEMALE_RE}


def _posture_group(tag: str):
    for i, g in enumerate(POSTURE_GROUPS):
        if g.search(tag):
            return i
    return None


class Scene:
    """앵커 행위 + 연관 태그로 서로 맞물리는 장면 구성."""

    def __init__(self, B: "Booru", level: int, pair: str = "HL"):
        self.B, self.level = B, level
        self.pair = pair if pair in PAIR else "HL"
        self.role = "common"  # common | top | bot | solo
        self.used: set[str] = set()
        self.posture = None
        self.dress = None  # 'nude' | 'clothed'

    def role_ban(self, tag: str) -> bool:
        P = PAIR[self.pair]
        if self.level < 2:
            return bool(SOLO_BAN[self.pair].search(tag))
        if P["common_ban"].search(tag) and self.role != "top" and self.pair == "HL":
            return True
        if self.role == "common" and P["common_ban"].search(tag):
            return True
        if self.role == "top" and (P["top_ban"].search(tag) or (self.pair == "HL" and FEM_TOP_RE.search(tag))):
            return True
        if self.role == "bot" and P["bot_ban"].search(tag):
            return True
        return False

    def ok(self, tag: str, top: bool = False) -> bool:
        if tag in self.used or META_RE.search(tag) or BAN_RE.search(tag) or NONCON_RE.search(tag) or self.role_ban(tag):
            return False
        it = self.B.by_tag.get(tag)
        if it is None:
            return False
        if (it["topic"] in FETISH_TOPICS and not FETISH_ALLOW_RE.search(tag)) or it["topic"] == VIOLENCE_TOPIC:
            return False
        if it["topic"] == "성인용 → 신체" and tag not in BODY_ALLOW:
            return False
        if top and self.pair != "GL" and not self.B.top_ok(tag):
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
        if tag in self.used or self.role_ban(tag):
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


def roll_scene(B: "Booru", level: int, ext: dict | None = None, pair: str = "HL") -> dict:
    """{'solo':[...]} 또는 {'common':[...], 'top':[...], 'bot':[...]} (items).
    ext = {face, face_top, face_bot, pose2, place, extra}: 봇 내장 (tag,kor) 목록."""
    ext = ext or {}
    sc = Scene(B, level, pair)
    E = lambda key, k: sc.from_ext(ext.get(key, []), k)
    if level < 2:
        sc.role = "solo"
        anchor = sc.from_pool(T_EXPO if level == 0 else T_POSE | T_EXPO, 1)
        a = anchor[0]["n"] if anchor else None
        wear = (sc.from_related(a, CLOTHED_RE, 1) if a else []) or sc.from_pool(T_WEAR | T_EXPO, 1)
        pose = (sc.from_related(a, POSE_RE, 1) if a else []) or sc.from_pool(T_POSE, 1)
        mood = sc.from_pool(T_MOOD, 1) if level == 1 else []
        body = sc.from_pool({"성인용 → 신체"}, 1) if level == 1 else []
        return {"solo": anchor + wear + body + pose + mood + E("face", 1) + E("place", 1) + E("extra", 1)}
    hard = level >= 3
    heavy = re.compile(r"double|triple|multiple|fisting|prolapse|insertion|gaping")
    sc.role = "common"
    acts = sc.from_pool(T_ACT, 1, pattern=None if hard else re.compile(r"^(?!.*(double|triple|multiple|fisting|prolapse|insertion)).*$"))
    if not acts:
        return {"common": [], "top": [], "bot": []}
    a = acts[0]["n"]
    pose2 = E("pose2", 1)
    pose = sc.from_related(a, POSE_RE, 1) or sc.from_pool(T_POSE, 1)
    more = [r["n"] for r in B.related(a, 40) if B.by_tag.get(r["n"], {}).get("topic") in T_ACT and (hard or not heavy.search(r["n"]))]
    acts += sc._take(more, 2 if hard else 1, False)
    fluid = sc.from_related(a, FLUID_RE, 1) or sc.from_pool(T_FLUID | T_TOY, 1)
    if hard:
        fluid += sc.from_pool(T_TOY, 1)
    common = pose2 + pose + acts + fluid + E("place", 1) + E("extra", 1)
    sc.role = "top"
    top = sc.from_related(a, TOP_RE, 2 if hard else 1, top=True)
    top += sc.from_pool(T_ACT | T_POSE, max(0, (2 if hard else 1) - len(top)), top=True, pattern=TOP_RE)
    top_wear = sc.from_related(a, CLOTHED_RE, 1, top=True) or sc.from_pool(T_EXPO | T_WEAR, 1, top=True)
    top_all = top_wear + top + E("face_top", 1)
    sc.role = "bot"
    bot = sc.from_related(a, BOT_RE, 2 if hard else 1)
    bot += sc.from_pool(T_POSE | T_MOOD | T_EXPO, max(0, (2 if hard else 1) - len(bot)), pattern=BOT_RE)
    bot_wear = sc.from_related(a, CLOTHED_RE, 1) or sc.from_pool(T_EXPO | T_WEAR, 1)
    mood = sc._take([r["n"] for r in B.related(a, 60) if B.by_tag.get(r["n"], {}).get("topic") in T_MOOD], 1, False)
    bot_all = bot_wear + bot + mood + E("face_bot", 2 if hard else 1)
    return {"common": common, "top": top_all, "bot": bot_all}


# ── NAI V5 정렬 순서 ──
_CAT = [
    (0, re.compile(r"^nsfw$")),
    (1, re.compile(r"^(1person|2people|couple|\dgirls?|\dboys?)$")),
    (2, re.compile(r"^(pov|from_above|from_below|from_behind|from_side|close-up|dutch_angle|upper_body|face_focus|foreshortening|cowboy_shot|full_body|cropped)$")),
    (4, re.compile(r"ahegao|torogao|blush|tears|crying|open_mouth|tongue|drool|breathing|trembling|biting_lip|smile|grin|smug|face|expression|eyes|pupils|mouth|lips|embarrassed|pleading|scared|surprised|nervous|serious|moan|orgasm|fucked_silly|rolling_eyes|licking_lips")),
    (5, re.compile(r"^looking_|eye_contact|gaze")),
    (7, re.compile(r"vibrator|dildo|sex_toy|condom|lube|handcuff|anal_beads|whip|toy|bead|gag$|leash|collar|rope|chain|blindfold|cuffs")),
    (8, re.compile(r"\bcum\b|cum_|_cum|juice|saliva|sweat|steam|heart|speech_bubble|motion_lines|sparkle|drip|overflow|squirt|milk|lactat|stain|puddle|trail|wet")),
    (9, re.compile(r"^(bed|bathroom|shower|onsen|beach|locker_room|office|car_interior|pool|hotel_room|kitchen|couch|window|rooftop|tent|balcony|changing_room|elevator|bathtub|sauna|mirror|stairs|classroom|outdoors|indoors|night|rain)$")),
    (11, re.compile(r"lighting|backlighting|candle|moonlight|neon|sunlight|spotlight|glow")),
    (13, re.compile(r"magazine_cover|photoshoot|holographic|silk_sheets|rose_petals|pixel|sketch|monochrome")),
]
_CAT_TOPIC = {"성인용 → 노출": 6, "성인용 → 복장 및 악세서리": 6, "복장 및 악세서리 → 상태": 6, "성인용 → 신체": 6,
              "성인용 → 자세": 3, "포즈 → 어필 자세": 3, "성인용 → 행위": 3, "성인용 → 액체": 8, "성인용 → 성인용품": 7,
              "성인용 → 기타": 7, "성인용 → 상태 및 분위기 및 감정": 10, "성인용 → 페티시": 7}


def cat_of(it: dict) -> int:
    t = it["n"]
    for c, rx in _CAT:
        if rx.search(t):
            return c
    c = _CAT_TOPIC.get(it.get("topic", ""))
    if c is not None:
        return c
    if POSE_RE.search(t) or TOP_RE.search(t) or BOT_RE.search(t):
        return 3
    if CLOTHED_RE.search(t) or NUDE_RE.search(t):
        return 6
    return 10


def order(items: list[dict]) -> list[dict]:
    return sorted(items, key=cat_of)  # stable
