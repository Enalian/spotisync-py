import re
from typing import Any, List, Set, Tuple

from src.core.models import TrackMeta

STOP_WORDS: frozenset[str] = frozenset(
    {
        "remix",
        "cover",
        "live",
        "nightcore",
        "slowed",
        "reverb",
        "sped up",
        "speed up",
        "instrumental",
        "karaoke",
        "tiktok",
        "edit",
        "snippet",
        "teaser",
        "bass boosted",
        "remake",
        "tribute",
        "8d",
        "mashup",
        "acoustic",
    }
)

VERSION_EQUIVALENCE_GROUPS = (
    ("remix", ("remix", "rmx", "flip", "bootleg", "vip", "mix")),
    ("slowed", ("slowed", "slow", "super slowed", "ultra slowed")),
    ("sped up", ("sped up", "speed up", "nightcore")),
    ("instrumental", ("instrumental", "караоке", "минус")),
    ("extended", ("extended", "club mix")),
    ("japanese", ("japanese", "jp ver", "japanese ver")),
    ("russian", ("russian", "rus ver", "на русском", "russian ver")),
    ("acoustic", ("acoustic", "unplugged", "акустика")),
)

CYR_TO_LAT_TABLE = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "yo",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


def normalize_tokens(s: str) -> List[str]:
    cleaned = re.sub(r"[^\w\s]", " ", s.lower())
    return [w for w in cleaned.split() if len(w) > 1]


def compact_alnum(s: str) -> str:
    return re.sub(r"[^\w]", "", s.lower())


def has_cjk_chars(s: str) -> bool:
    return bool(
        re.search(
            r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\uac00-\ud7af]",
            s,
        )
    )


def transliterate_cyr_to_lat(text: str) -> str:
    return text.lower().translate(CYR_TO_LAT_TABLE)


def contains_word_token(text: str, phrase: str) -> bool:
    p_clean = phrase.strip().lower()
    if not p_clean:
        return False
    pattern = (
        r"(?<![a-zA-Z0-9а-яА-ЯёЁ])" + re.escape(p_clean) + r"(?![a-zA-Z0-9а-яА-ЯёЁ])"
    )
    return bool(re.search(pattern, text.lower()))


def extract_expected_remixer_tokens(title: str) -> List[str]:
    matches = re.findall(
        r"[\(\[]([^\)\]]*?(?:remix|mix|vip|flip|bootleg|vision)[^\)\]]*?)[\)\]]",
        title,
        flags=re.I,
    )
    ignore = {
        "remix",
        "mix",
        "vip",
        "official",
        "audio",
        "video",
        "extended",
        "radio",
        "edit",
        "feat",
        "ft",
        "version",
    }
    return [
        w
        for m in matches
        for w in normalize_tokens(m)
        if w not in ignore and len(w) >= 3
    ]


def check_version_compatibility(
    cand_title: str, uploader: str, description: str, meta: TrackMeta
) -> Tuple[bool, str]:
    title_lower = cand_title.lower()
    orig_title_lower = meta.title.lower()
    orig_combined = f"{orig_title_lower} {meta.album.lower()}"

    for word in STOP_WORDS:
        if contains_word_token(title_lower, word) and not contains_word_token(
            orig_combined, word
        ):
            allowed_by_group = any(
                word in gw
                and any(contains_word_token(orig_combined, gw) for gw in group_words)
                for _, group_words in VERSION_EQUIVALENCE_GROUPS
            )
            if not allowed_by_group:
                return False, f"стоп-слово '{word}' запрещено (нет в оригинале)"

    cand_combined = f"{title_lower} {uploader.lower()} {description.lower()[:350]}"
    for group_label, group_words in VERSION_EQUIVALENCE_GROUPS:
        if any(contains_word_token(orig_title_lower, gw) for gw in group_words):
            if not any(contains_word_token(cand_combined, gw) for gw in group_words):
                return False, f"пропущен обязательный маркер версии '{group_label}'"

    for r_tok in extract_expected_remixer_tokens(meta.title):
        if r_tok not in cand_combined and r_tok not in compact_alnum(cand_combined):
            return False, f"не найден автор ремикса '{r_tok}'"

    return True, "ok"


def is_trusted_artist_or_remixer_channel(uploader: str, meta: TrackMeta) -> bool:
    up_clean = (
        uploader.lower()
        .replace(" official", "")
        .replace(" music", "")
        .replace("youtube channel", "")
        .strip()
    )
    if not up_clean:
        return False
    all_names = [
        a.lower().strip() for a in meta.artists_all if a.strip()
    ] + extract_expected_remixer_tokens(meta.title)

    # Простая фонетика для проверки доверенного канала
    for name in all_names:
        if len(name) >= 2 and (
            name in up_clean or compact_alnum(name) in compact_alnum(up_clean)
        ):
            return True
    return False


def has_content_id_music_match(entry: dict, meta: TrackMeta) -> bool:
    yt_track, yt_artist = (
        (entry.get("track") or "").strip(),
        (entry.get("artist") or "").strip(),
    )
    if not yt_track or not yt_artist:
        return False

    artist_match = any(
        a.lower() in yt_artist.lower() for a in meta.artists_all if a.strip()
    )
    if not artist_match:
        return False

    base_sp, yt_tr_comp = compact_alnum(meta.base_title), compact_alnum(yt_track)
    if base_sp and yt_tr_comp and (base_sp in yt_tr_comp or yt_tr_comp in base_sp):
        return True
    alb_comp = compact_alnum(
        re.sub(r"\s*-\s*(?:ep|single).*$", "", meta.album, flags=re.I)
    )
    return bool(
        alb_comp
        and len(alb_comp) >= 4
        and (alb_comp in yt_tr_comp or yt_tr_comp in alb_comp)
    )


def score_candidate(
    entry: dict, meta: TrackMeta, source_type: str
) -> Tuple[float, str]:
    if not entry:
        return -1.0, "пустой ответ"
    if entry.get("drm"):
        return -1.0, "защищен DRM"

    cand_title = entry.get("title", "Unknown")
    entry_album = (entry.get("album") or "").lower()
    uploader = entry.get("uploader") or entry.get("channel") or "Unknown"
    description = (entry.get("description") or "").lower()
    duration = int(entry.get("duration") or 0)
    dur_diff = abs(duration - meta.duration_sec)
    title_lower = cand_title.lower()

    ver_ok, ver_reason = check_version_compatibility(
        cand_title, uploader, description, meta
    )
    if not ver_ok:
        return -1.0, ver_reason

    if 0 < duration < 30 and meta.duration_sec > 30:
        return -1.0, f"слишком мало (тизер/шортс)"
    if meta.duration_sec > 0 and dur_diff > 8:
        return -1.0, f"разница длительности {dur_diff}сек > 8сек"

    score = 50.0
    reasons = ["base:50"]

    dur_penalty = dur_diff * 2.5 if meta.duration_sec > 0 else 0.0
    if dur_penalty > 0:
        score -= dur_penalty
        reasons.append(f"dur_diff:-{int(dur_penalty)}")
    else:
        score += 15.0
        reasons.append("dur:exact(+15)")

    if (
        "provided to youtube by" in description
        or "auto-generated by youtube" in description
    ):
        score += 60.0
        reasons.append("studio_master:+60")
    if has_content_id_music_match(entry, meta):
        score += 55.0
        reasons.append("content_id_card:+55")
    if uploader.lower().endswith("- topic"):
        score += 45.0
        reasons.append("youtube_topic:+45")
    elif is_trusted_artist_or_remixer_channel(uploader, meta):
        score += 70.0
        reasons.append("artist_ch:+70")

    if meta.release_year != "N/A" and (
        meta.release_year in description or meta.release_year in title_lower
    ):
        score += 15.0
        reasons.append(f"year_match:+15")

    return score, ", ".join(reasons)
