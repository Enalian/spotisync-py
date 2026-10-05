from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import contextvars
from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
from typing import Any, Self

from dotenv import load_dotenv
import httpx
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TSRC, TXXX
from mutagen.mp3 import MP3
import yt_dlp

load_dotenv()

BUILD_VERSION = (
    "v7.3.18-MODERN (Python 3.12+ | Metadata Enrichment Quarantine failed_metadata.json | "
    "Customizable Smart TIME_FORMAT | AzuraCast Polling & Auto-Delete Ignored)"
)

TRUE_VALUES = frozenset({"true", "1", "yes", "on"})
TIME_FORMAT: str = os.environ.get("TIME_FORMAT", "%hч %mмин %sсек") or "%hч %mмин %sсек"


def parse_env_bool(key: str, default: str = "false") -> bool:
    return os.environ.get(key, default).strip().lower() in TRUE_VALUES


def parse_str_bool(val: str) -> bool:
    return val.strip().lower() in TRUE_VALUES


def yn(val: Any) -> str:
    return "Да" if bool(val) else "Нет"


def format_duration(seconds: float | int) -> str:
    total_sec = math.ceil(max(0.0, float(seconds)))
    fmt = TIME_FORMAT or "%hч %mмин %sсек"
    tokens = list(re.finditer(r"%([hms])([^%]*)", fmt))
    if not tokens:
        return f"{total_sec}сек"

    has_h = any(m.group(1) == "h" for m in tokens)
    has_m = any(m.group(1) == "m" for m in tokens)

    if has_h:
        hours, rem = divmod(total_sec, 3600)
        mins, secs = divmod(rem, 60) if has_m else (0, rem)
    elif has_m:
        hours = 0
        mins, secs = divmod(total_sec, 60)
    else:
        hours = mins = 0
        secs = total_sec

    values = {"h": hours, "m": mins, "s": secs}
    parts: list[str] = []
    for m in tokens:
        unit = m.group(1)
        suffix = m.group(2).rstrip()
        val = values[unit]
        if val > 0 or (unit == "s" and total_sec == 0 and not parts):
            parts.append(f"{val}{suffix}")

    if not parts:
        last_m = tokens[-1]
        parts.append(f"0{last_m.group(2).rstrip()}")

    return " ".join(parts)


TRACE_LEVEL = 5
SUCCESS_LEVEL = 25
logging.addLevelName(TRACE_LEVEL, "TRACE")
logging.addLevelName(SUCCESS_LEVEL, "SUCCESS")

current_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("current_ctx", default="MAIN")

LOG_LEVEL_STR = os.environ.get("LOG_LEVEL", "INFO").upper().strip()
LOG_COLORS = parse_env_bool("LOG_COLORS", "true")
LOG_FILE = os.environ.get("LOG_FILE", "/app/data/spotisync.log").strip()

LOG_SHOW_PARSED_TRACKS = parse_env_bool("LOG_SHOW_PARSED_TRACKS", "false")
LOG_SHOW_SCORING = parse_env_bool("LOG_SHOW_SCORING", "true")
LOG_SHOW_SKIPPED = parse_env_bool("LOG_SHOW_SKIPPED", "false")
LOG_SHOW_QUARANTINE = parse_env_bool("LOG_SHOW_QUARANTINE", "true")
LOG_DOWNLOAD_PROGRESS = parse_env_bool("LOG_DOWNLOAD_PROGRESS", "true")

LEVEL_MAP: dict[str, int] = {
    "TRACE": TRACE_LEVEL,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "SUCCESS": SUCCESS_LEVEL,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class ContextColorFormatter(logging.Formatter):
    COLORS: dict[int, str] = {
        TRACE_LEVEL: "\033[90m",
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[37m",
        SUCCESS_LEVEL: "\033[1;32m",
        logging.WARNING: "\033[1;33m",
        logging.ERROR: "\033[1;31m",
    }
    RESET = "\033[0m"
    CTX_COLOR = "\033[35m"

    def __init__(self, use_colors: bool = True) -> None:
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")
        self.use_colors = use_colors

    def format(self, record: logging.LogRecord) -> str:
        ctx = current_ctx.get()
        asctime = self.formatTime(record, self.datefmt)
        levelname = f"{record.levelname:<7}"
        raw_msg = record.getMessage()
        if self.use_colors:
            color = self.COLORS.get(record.levelno, "")
            return (
                f"\033[90m{asctime}{self.RESET} | {color}{levelname}{self.RESET} | "
                f"{self.CTX_COLOR}[{ctx}]{self.RESET} {color}{raw_msg}{self.RESET}"
            )
        return f"{asctime} | {levelname} | [{ctx}] {ANSI_ESCAPE_RE.sub('', raw_msg)}"


class CustomLogger(logging.Logger):
    def trace(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(TRACE_LEVEL):
            self._log(TRACE_LEVEL, msg, args, **kwargs)

    def success(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(SUCCESS_LEVEL):
            self._log(SUCCESS_LEVEL, msg, args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger: CustomLogger = logging.getLogger("SpotiSync")
logger.setLevel(TRACE_LEVEL)
logger.propagate = False

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(LEVEL_MAP.get(LOG_LEVEL_STR, logging.INFO))
console_handler.setFormatter(ContextColorFormatter(use_colors=LOG_COLORS))
logger.addHandler(console_handler)

_file_handler: RotatingFileHandler | None = None
_active_log_file_path: str = ""


def reconfigure_logger_handlers() -> None:
    global _file_handler, _active_log_file_path
    target_level = LEVEL_MAP.get(LOG_LEVEL_STR.upper().strip(), logging.INFO)
    console_handler.setLevel(target_level)
    console_handler.setFormatter(ContextColorFormatter(use_colors=LOG_COLORS))

    if LOG_FILE != _active_log_file_path:
        if _file_handler is not None:
            logger.removeHandler(_file_handler)
            try:
                _file_handler.close()
            except OSError:
                pass
            _file_handler = None
        _active_log_file_path = LOG_FILE
        if LOG_FILE:
            try:
                log_path = Path(LOG_FILE)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                _file_handler = RotatingFileHandler(
                    log_path, maxBytes=15 * 1024 * 1024, backupCount=5, encoding="utf-8"
                )
                _file_handler.setFormatter(ContextColorFormatter(use_colors=False))
                logger.addHandler(_file_handler)
            except OSError as e:
                print(f"Не удалось инициализировать файл логов {LOG_FILE}: {e}")

    if _file_handler is not None:
        _file_handler.setLevel(target_level)


shutdown_event = threading.Event()
shutdown_signal_name: str = ""
_active_watchdogs_lock = threading.Lock()
_active_watchdogs: set[AdaptiveWorkerWatchdog] = set()


def is_shutting_down() -> bool:
    return shutdown_event.is_set()


def trigger_graceful_shutdown(sig_name: str) -> None:
    global shutdown_signal_name
    if shutdown_event.is_set():
        return
    shutdown_signal_name = sig_name
    shutdown_event.set()
    token_ctx = current_ctx.set("DOCKER-STOP")
    try:
        logger.warning(
            f"Получен сигнал остановки контейнера [{sig_name}]! "
            f"Прерываем активные загрузки без записи в карантин и очищаем временные файлы..."
        )
    finally:
        current_ctx.reset(token_ctx)

    with _active_watchdogs_lock:
        for wd in list(_active_watchdogs):
            wd.cancel(f"Остановка контейнера ({sig_name})")


soundcloud_consecutive_403 = 0
SOUNDCLOUD_MAX_CONSECUTIVE_403 = 5
soundcloud_lock = threading.Lock()


def record_soundcloud_403() -> None:
    global soundcloud_consecutive_403
    with soundcloud_lock:
        soundcloud_consecutive_403 += 1
        if soundcloud_consecutive_403 == SOUNDCLOUD_MAX_CONSECUTIVE_403:
            logger.warning(
                f"[SOUNDCLOUD] Получено {SOUNDCLOUD_MAX_CONSECUTIVE_403} ошибок 403 Forbidden подряд. "
                f"IP вашего VPN заблокирован в SoundCloud — отключаем SoundCloud до конца цикла!"
            )


def reset_soundcloud_403() -> None:
    global soundcloud_consecutive_403
    with soundcloud_lock:
        if 0 < soundcloud_consecutive_403 < SOUNDCLOUD_MAX_CONSECUTIVE_403:
            soundcloud_consecutive_403 = 0


def is_soundcloud_blocked() -> bool:
    with soundcloud_lock:
        return soundcloud_consecutive_403 >= SOUNDCLOUD_MAX_CONSECUTIVE_403


class YtdlpLoggerAdapter:
    def __init__(self) -> None:
        self.last_error_msg: str = ""
        self.last_warning_msg: str = ""

    def debug(self, msg: str) -> None:
        if not is_shutting_down():
            logger.trace(msg if msg.startswith("[debug] ") else f"[yt-dlp] {msg}")

    def info(self, msg: str) -> None:
        if not is_shutting_down():
            logger.trace(f"[yt-dlp] {msg}")

    def warning(self, msg: str) -> None:
        if is_shutting_down():
            return
        self.last_warning_msg = msg
        if "DRM protected" in msg or "Requested format is not available" in msg:
            logger.debug(f"[yt-dlp skip] {msg}")
        else:
            logger.debug(f"[yt-dlp warn] {msg}")

    def error(self, msg: str) -> None:
        if is_shutting_down():
            return
        self.last_error_msg = msg
        if "DRM protected" in msg or "Requested format is not available" in msg:
            logger.debug(f"[yt-dlp format-skip] {msg}")
        elif "Sign in to confirm your age" in msg:
            logger.debug(f"[yt-dlp age-gate] {msg}")
        elif "[soundcloud]" in msg and "HTTP Error 403" in msg:
            record_soundcloud_403()
            logger.debug(f"[yt-dlp sc-403] {msg}")
        else:
            logger.warning(f"[yt-dlp err] {msg}")


PLAYLIST_URL: str = os.environ.get("PLAYLIST_URL", "").strip()
SPOTIFY_MARKET: str = os.environ.get("SPOTIFY_MARKET", "US").strip() or "US"

FILTER_UNAVAILABLE_SPOTIFY: bool = parse_env_bool("FILTER_UNAVAILABLE_SPOTIFY", "true")
IGNORE_CACHED_URLS: bool = parse_env_bool("IGNORE_CACHED_URLS", "true")
TRACK_ALLOW_EXTERNAL: bool = parse_env_bool("TRACK_ALLOW_EXTERNAL", "false")

SYNC_INTERVAL_MINUTES: float = float(os.environ.get("SYNC_INTERVAL_MINUTES", "30"))
OUTPUT_DIR: Path = Path(os.environ.get("OUTPUT_DIR", "/music"))

CACHE_FILENAME: str = os.environ.get("CACHE_FILENAME", ".spotisync.json").strip() or ".spotisync.json"
CACHE_FILE_PATH: Path = OUTPUT_DIR / CACHE_FILENAME

CUSTOM_TRACKS_FILENAME: str = os.environ.get("CUSTOM_TRACKS_FILENAME", ".spotitracks.json").strip() or ".spotitracks.json"
CUSTOM_TRACKS_PATH: Path = OUTPUT_DIR / CUSTOM_TRACKS_FILENAME

STAGING_DIR: Path = Path(os.environ.get("STAGING_DIR", "/tmp/spotisync_staging"))
SYNC_DELETE_REMOVED: bool = parse_env_bool("SYNC_DELETE_REMOVED", "false")
SYNC_DELETE_IGNORED: bool = (
    parse_env_bool("SYNC_DELETE_IGNORED", "true" if SYNC_DELETE_REMOVED else "false")
    if "SYNC_DELETE_IGNORED" in os.environ
    else parse_env_bool("DELETE_IGNORED_TRACKS", "true" if SYNC_DELETE_REMOVED else "false")
)
PUID: int = int(os.environ.get("PUID", "1000"))
PGID: int = int(os.environ.get("PGID", "1000"))

CONCURRENT_DOWNLOADS: int = max(1, int(os.environ.get("CONCURRENT_DOWNLOADS", "5")))
YTDLP_RETRIES: int = max(1, int(os.environ.get("YTDLP_RETRIES", "3")))
YTDLP_SOCKET_TIMEOUT_SEC: int = max(5, int(os.environ.get("YTDLP_SOCKET_TIMEOUT_SEC", "30")))

WORKER_BASE_TIMEOUT_SEC: int = max(30, int(os.environ.get("WORKER_TIMEOUT_SEC", "360")))
WORKER_SEARCH_TIMEOUT_SEC: float = max(15.0, float(os.environ.get("WORKER_SEARCH_STEP_TIMEOUT_SEC", "180")))
WORKER_STALL_TIMEOUT_SEC: int = max(15, int(os.environ.get("WORKER_STALL_TIMEOUT_SEC", "120")))
WORKER_STAGE_MAX_TIMEOUT_SEC: int = max(60, int(os.environ.get("WORKER_MAX_HARD_TIMEOUT_SEC", "1500")))
MIN_ACCEPTABLE_SPEED_KBPS: float = max(1.0, float(os.environ.get("MIN_ACCEPTABLE_SPEED_KBPS", "10")))
MIN_ACCEPTABLE_SPEED_BPS: float = MIN_ACCEPTABLE_SPEED_KBPS * 1024.0

FAIL_TTL_HOURS: float = max(0.0, float(os.environ.get("FAIL_TTL_HOURS", "72")))
FAILED_CACHE_FILE: Path = Path(os.environ.get("FAILED_CACHE_FILE", "/app/data/failed_tracks.json"))
META_FAIL_TTL_HOURS: float = max(0.0, float(os.environ.get("META_FAIL_TTL_HOURS", "24")))
FAILED_META_CACHE_FILE: Path = Path(os.environ.get("FAILED_META_CACHE_FILE", "/app/data/failed_metadata.json"))
YT_COOKIE_FILE: Path = Path(os.environ.get("YT_COOKIE_FILE", "/app/data/cookies.txt"))
POT_PROVIDER_URL: str = os.environ.get("POT_PROVIDER_URL", "").strip()

AUDIO_NORMALIZE: bool = parse_env_bool("AUDIO_NORMALIZE", "true")
AUDIO_TRIM_SILENCE: bool = parse_env_bool("AUDIO_TRIM_SILENCE", "true")
ENABLE_FALLBACK_SEARCH: bool = parse_env_bool("ENABLE_FALLBACK_SEARCH", "true")
DIRECT_URL_FALLBACK: bool = parse_env_bool("DIRECT_URL_FALLBACK", "true")

AZURACAST_URL: str = os.environ.get("AZURACAST_URL", "").strip().rstrip("/")
AZURACAST_API_KEY: str = os.environ.get("AZURACAST_API_KEY", "").strip()
AZURACAST_STATION_ID: str = os.environ.get("AZURACAST_STATION_ID", "").strip()
AZURACAST_PLAYLIST_ID: str = os.environ.get("AZURACAST_PLAYLIST_ID", "").strip()
AZURACAST_PLAYLIST_NAME: str = os.environ.get("AZURACAST_PLAYLIST_NAME", "").strip()
AZURACAST_MEDIA_SUBDIR: str = os.environ.get("AZURACAST_MEDIA_SUBDIR", "").strip().strip("/")

DEFAULT_PLAYLIST_COVER_URL: str | None = None

_cover_cache_lock = threading.Lock()
_cover_bytes_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
COVER_CACHE_MAX_ITEMS = 32


def fetch_cover_cached(url: str) -> tuple[bytes, str] | None:
    if is_shutting_down():
        return None
    with _cover_cache_lock:
        if url in _cover_bytes_cache:
            _cover_bytes_cache.move_to_end(url)
            return _cover_bytes_cache[url]

    for attempt in range(2):
        if is_shutting_down():
            return None
        try:
            r_cov = httpx.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15.0, follow_redirects=True)
            if r_cov.status_code == 200 and r_cov.content:
                mime = r_cov.headers.get("Content-Type", "image/jpeg")
                res = (r_cov.content, mime)
                with _cover_cache_lock:
                    _cover_bytes_cache[url] = res
                    if len(_cover_bytes_cache) > COVER_CACHE_MAX_ITEMS:
                        _cover_bytes_cache.popitem(last=False)
                return res
        except Exception as e:
            if attempt == 1:
                logger.debug(f"Не удалось загрузить обложку {url}: {e}")
            else:
                time.sleep(0.5)
    return None


def refresh_derived_config() -> None:
    global CACHE_FILE_PATH, CUSTOM_TRACKS_PATH, MIN_ACCEPTABLE_SPEED_BPS
    CACHE_FILE_PATH = OUTPUT_DIR / CACHE_FILENAME
    CUSTOM_TRACKS_PATH = OUTPUT_DIR / CUSTOM_TRACKS_FILENAME
    MIN_ACCEPTABLE_SPEED_BPS = MIN_ACCEPTABLE_SPEED_KBPS * 1024.0
    reconfigure_logger_handlers()


def is_azuracast_configured() -> bool:
    if not (AZURACAST_URL and AZURACAST_API_KEY and AZURACAST_STATION_ID):
        return False
    if not (AZURACAST_PLAYLIST_ID or AZURACAST_PLAYLIST_NAME):
        return False
    if not AZURACAST_URL.startswith(("http://", "https://")):
        return False
    placeholders = ("none", "null", "false", "нет", "выкл", "example", "your_", "ваш_")
    for val in (AZURACAST_URL, AZURACAST_API_KEY, AZURACAST_STATION_ID):
        if any(p in val.lower() for p in placeholders):
            return False
    if not (AZURACAST_API_KEY.isascii() and AZURACAST_URL.isascii() and AZURACAST_STATION_ID.isascii()):
        return False
    return " " not in AZURACAST_API_KEY and len(AZURACAST_API_KEY) >= 8


STOP_WORDS: frozenset[str] = frozenset({
    "remix", "cover", "live", "nightcore", "slowed", "reverb", "sped up", "speed up",
    "instrumental", "karaoke", "tiktok", "edit", "snippet", "teaser", "bass boosted",
    "remake", "tribute", "8d", "mashup", "acoustic",
})

VERSION_EQUIVALENCE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("remix", ("remix", "rmx", "flip", "bootleg", "vip", "mix")),
    ("slowed", ("slowed", "slow", "super slowed", "ultra slowed")),
    ("sped up", ("sped up", "speed up", "nightcore")),
    ("instrumental", ("instrumental", "караоке", "минус")),
    ("extended", ("extended", "club mix")),
    ("japanese", ("japanese", "jp ver", "japanese ver")),
    ("russian", ("russian", "rus ver", "на русском", "russian ver")),
    ("acoustic", ("acoustic", "unplugged", "акустика")),
)

BRACKET_KEEP_KEYWORDS: tuple[str, ...] = (
    "remix", "rmx", "slow", "sped", "speed", "nightcore", "instrumental",
    "extended", "vip", "mix", "edit", "version", "ver", "japanese", "russian",
    "acoustic", "live", "cover", "ost", "soundtrack", "theme", "vision", "flip",
)

YT_CLIENT_PROFILES: tuple[dict[str, Any], ...] = (
    {"clients": ["ios", "mweb"], "use_cookies": False},
    {"clients": ["tv_embedded", "mweb"], "use_cookies": True},
    {"clients": ["web_creator", "android_vr", "tv_embedded"], "use_cookies": True},
)

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

CYR_TO_LAT_TABLE = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})

file_move_lock = threading.Lock()
cache_lock = threading.Lock()
quarantine_lock = threading.Lock()
meta_quarantine_lock = threading.Lock()


def transliterate_cyr_to_lat(text: str) -> str:
    return text.lower().translate(CYR_TO_LAT_TABLE)


def phonetic_skeleton(text: str) -> str:
    s = transliterate_cyr_to_lat(text.lower())
    s = re.sub(r"[^\w]", "", s)
    for src, dst in (
        ("shch", "sh"),
        ("kh", "h"),
        ("ph", "f"),
        ("th", "t"),
        ("ck", "k"),
        ("qu", "kw"),
        ("ts", "c"),
        ("tz", "c"),
        ("ea", "e"),
        ("ee", "i"),
        ("oo", "u"),
        ("ou", "u"),
        ("iy", "i"),
        ("yi", "i"),
        ("yy", "i"),
        ("yo", "o"),
        ("yu", "u"),
        ("ya", "a"),
        ("w", "v"),
        ("x", "ks"),
    ):
        s = s.replace(src, dst)
    s = s.replace("c", "k")
    return re.sub(r"(.)\1+", r"\1", s)


def consonant_skeleton(text: str) -> str:
    skel = phonetic_skeleton(text)
    return re.sub(r"[aeiouy]", "", skel)


def titles_phonetically_match(t1: str, t2: str) -> bool:
    p1, p2 = phonetic_skeleton(t1), phonetic_skeleton(t2)
    if not p1 or not p2:
        return False
    if p1 == p2:
        return True
    if min(len(p1), len(p2)) >= 5 and (p1 in p2 or p2 in p1):
        return True
    c1, c2 = consonant_skeleton(t1), consonant_skeleton(t2)
    if min(len(c1), len(c2)) >= 4 and c1 == c2 and abs(len(p1) - len(p2)) <= 2:
        return True
    if min(len(p1), len(p2)) >= 4 and abs(len(p1) - len(p2)) <= 1:
        diffs = sum(1 for a, b in zip(p1, p2) if a != b) + abs(len(p1) - len(p2))
        if diffs <= 1:
            return True
    return False


def contains_word_token(text: str, phrase: str) -> bool:
    p_clean = phrase.strip().lower()
    if not p_clean:
        return False
    pattern = r"(?<![a-zA-Z0-9а-яА-ЯёЁ])" + re.escape(p_clean) + r"(?![a-zA-Z0-9а-яА-ЯёЁ])"
    return bool(re.search(pattern, text.lower()))


def artists_loosely_match(sp_artist: str, cand_artist: str) -> bool:
    s1, s2 = sp_artist.lower().strip(), cand_artist.lower().strip()
    if not s1 or not s2:
        return False
    if s1 == s2 or contains_word_token(s2, s1) or contains_word_token(s1, s2):
        return True

    a1, a2 = re.sub(r"[^\w]", "", s1), re.sub(r"[^\w]", "", s2)
    if not a1 or not a2:
        return False
    if a1 == a2:
        return True
    if min(len(a1), len(a2)) >= 5 and (a1 in a2 or a2 in a1):
        return True

    t1 = re.sub(r"[^\w]", "", transliterate_cyr_to_lat(s1))
    t2 = re.sub(r"[^\w]", "", transliterate_cyr_to_lat(s2))
    if not t1 or not t2:
        return False
    if t1 == t2 or t1 == a2 or t2 == a1:
        return True
    if titles_phonetically_match(s1, s2):
        return True
    min_t = min(len(t1), len(t2))
    if min_t >= 5 and (t1 in t2 or t2 in t1 or t1 in a2 or t2 in a1):
        return True
    return min_t >= 6 and t1[:5] == t2[:5] and abs(len(t1) - len(t2)) <= 2


class GlobalSpeedTracker:
    def __init__(self) -> None:
        self.samples: deque[float] = deque(maxlen=50)
        self._lock = threading.Lock()

    def record_speed(self, bps: float) -> None:
        if bps > 0:
            with self._lock:
                self.samples.append(bps)

    @property
    def avg_speed_bps(self) -> float:
        with self._lock:
            return (sum(self.samples) / len(self.samples)) if self.samples else 0.0


speed_tracker = GlobalSpeedTracker()


@dataclass(slots=True, eq=False)
class AdaptiveWorkerWatchdog:
    base_timeout: float = float(WORKER_BASE_TIMEOUT_SEC)
    worker_start_time: float = field(init=False)
    stage_start_time: float = field(init=False)
    last_activity: float = field(init=False)
    deadline: float = field(init=False)
    stage: str = "init"
    ema_speed_bps: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    aborted_by_watchdog: bool = False
    hard_cancelled: bool = False
    abort_reason: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        now = time.monotonic()
        self.worker_start_time = now
        self.stage_start_time = now
        self.last_activity = now
        self.deadline = now + self.base_timeout

    def _check_expired_unlocked(self, now: float) -> tuple[bool, str]:
        if is_shutting_down():
            return True, f"Остановка контейнера ({shutdown_signal_name or 'SIGTERM'})"
        if self.hard_cancelled:
            return True, self.abort_reason or "Прервано по сигналу Watchdog"
        stage_elapsed = now - self.stage_start_time
        if stage_elapsed > WORKER_STAGE_MAX_TIMEOUT_SEC:
            return True, f"Превышен лимит этапа '{self.stage}' ({format_duration(WORKER_STAGE_MAX_TIMEOUT_SEC)})"
        if self.stage.startswith("download") and (now - self.last_activity) > WORKER_STALL_TIMEOUT_SEC:
            return True, (
                f"Зависание скачивания на этапе '{self.stage}' ({format_duration(now - self.last_activity)} без данных, "
                f"скорость: {format_speed(self.ema_speed_bps)})"
            )
        if now > self.deadline:
            if self.stage.startswith("search"):
                return True, f"Таймаут поискового запроса '{self.stage}' ({format_duration(stage_elapsed)})"
            return True, f"Таймаут этапа '{self.stage}' ({format_duration(stage_elapsed)}, скорость: {format_speed(self.ema_speed_bps)})"
        return False, ""

    def check_expired(self) -> tuple[bool, str]:
        with self._lock:
            return self._check_expired_unlocked(time.monotonic())

    def cancel(self, reason: str) -> None:
        with self._lock:
            self.aborted_by_watchdog = True
            self.hard_cancelled = True
            self.abort_reason = reason

    def sleep_with_check(self, seconds: float) -> None:
        end_t = time.monotonic() + seconds
        while (now := time.monotonic()) < end_t:
            if is_shutting_down():
                raise TimeoutError(f"Остановка контейнера ({shutdown_signal_name or 'SIGTERM'})")
            with self._lock:
                if self.hard_cancelled:
                    raise TimeoutError(self.abort_reason or "Прервано по сигналу Watchdog")
            time.sleep(min(0.2, max(0.01, end_t - now)))
        with self._lock:
            now = time.monotonic()
            self.last_activity = now
            self.deadline = max(self.deadline, now + 120.0)

    def reset_for_search_stage(self, stage_label: str = "search") -> None:
        if is_shutting_down():
            raise TimeoutError(f"Остановка контейнера ({shutdown_signal_name or 'SIGTERM'})")
        with self._lock:
            if self.hard_cancelled:
                raise TimeoutError(self.abort_reason or "Остановлено адаптивным таймером")
            now = time.monotonic()
            self.aborted_by_watchdog = False
            self.stage = stage_label
            self.stage_start_time = now
            self.last_activity = now
            self.downloaded_bytes = 0
            self.total_bytes = 0
            self.ema_speed_bps = 0.0
            avg_s = speed_tracker.avg_speed_bps
            allowance = max(WORKER_SEARCH_TIMEOUT_SEC, 240.0) if (0 < avg_s < 60 * 1024) else WORKER_SEARCH_TIMEOUT_SEC
            self.deadline = now + allowance

    def reset_for_download_attempt(self, attempt_label: str = "download") -> None:
        if is_shutting_down():
            raise TimeoutError(f"Остановка контейнера ({shutdown_signal_name or 'SIGTERM'})")
        with self._lock:
            if self.hard_cancelled:
                raise TimeoutError(self.abort_reason or "Остановлено адаптивным таймером")
            now = time.monotonic()
            self.aborted_by_watchdog = False
            self.stage = attempt_label
            self.stage_start_time = now
            self.last_activity = now
            self.downloaded_bytes = 0
            self.total_bytes = 0
            self.ema_speed_bps = 0.0
            self.deadline = now + float(WORKER_BASE_TIMEOUT_SEC)

    def update_download(self, downloaded: int, total: int, speed: float | None) -> None:
        if is_shutting_down():
            raise yt_dlp.utils.DownloadError(f"Остановка контейнера ({shutdown_signal_name or 'SIGTERM'})")
        with self._lock:
            now = time.monotonic()
            if self.hard_cancelled:
                raise yt_dlp.utils.DownloadError(self.abort_reason or "Остановлено адаптивным таймером")
            if not self.stage.startswith("download"):
                self.stage = "download"
                self.stage_start_time = now
                self.downloaded_bytes = 0
                self.last_activity = now
            if downloaded > self.downloaded_bytes:
                self.last_activity = now
                self.downloaded_bytes = downloaded
            if total > 0:
                self.total_bytes = total
            if speed and speed > 0:
                self.ema_speed_bps = float(speed) if self.ema_speed_bps <= 0 else (0.3 * float(speed) + 0.7 * self.ema_speed_bps)

            stall_sec = now - self.last_activity
            if stall_sec > WORKER_STALL_TIMEOUT_SEC:
                self.aborted_by_watchdog = True
                self.abort_reason = (
                    f"Поток замер без прогресса на {format_duration(stall_sec)} "
                    f"(скорость: {format_speed(self.ema_speed_bps)})"
                )
                raise yt_dlp.utils.DownloadError(self.abort_reason)

            eff_speed = max(self.ema_speed_bps, MIN_ACCEPTABLE_SPEED_BPS)
            if self.total_bytes > self.downloaded_bytes and eff_speed > 0:
                eta_sec = (self.total_bytes - self.downloaded_bytes) / eff_speed
                self.deadline = now + min(max(eta_sec * 3.0 + 120.0, 150.0), float(WORKER_STAGE_MAX_TIMEOUT_SEC))
            else:
                self.deadline = now + 150.0

    def enter_ffmpeg(self) -> None:
        if is_shutting_down():
            raise yt_dlp.utils.DownloadError(f"Остановка контейнера ({shutdown_signal_name or 'SIGTERM'})")
        with self._lock:
            if self.hard_cancelled:
                raise yt_dlp.utils.DownloadError(self.abort_reason or "Остановлено адаптивным таймером")
            now = time.monotonic()
            self.aborted_by_watchdog = False
            self.stage = "ffmpeg"
            self.stage_start_time = now
            self.last_activity = now
            self.deadline = now + 300.0

    def enter_tagging(self) -> None:
        if is_shutting_down():
            raise TimeoutError(f"Остановка контейнера ({shutdown_signal_name or 'SIGTERM'})")
        with self._lock:
            now = time.monotonic()
            self.aborted_by_watchdog = False
            self.stage = "tagging"
            self.stage_start_time = now
            self.last_activity = now
            self.deadline = now + 120.0


@dataclass(slots=True)
class TrackMeta:
    spotify_id: str
    title: str
    artist: str
    artists_all: list[str]
    album: str
    album_artist: str
    release_date: str
    track_number: str
    disc_number: str
    duration_sec: int
    isrc: str | None
    cover_url: str | None
    direct_url: str | None = None

    @property
    def base_title(self) -> str:
        cleaned = re.sub(r"[\(\[].*?[\)\]]", "", self.title)
        cleaned = re.sub(r"\s+-\s+.*$", "", cleaned)
        return cleaned.strip() or self.title

    @property
    def clean_title(self) -> str:
        def filter_brackets(m: re.Match[str]) -> str:
            content = m.group(0)
            low = content.lower()
            if any(k in low for k in ("feat.", "ft.", "prod.", "produced by", "with ")):
                if not any(kw in low for kw in BRACKET_KEEP_KEYWORDS):
                    return ""
            return content

        cleaned = re.sub(r"[\(\[].*?[\)\]]", filter_brackets, self.title)
        return re.sub(r"\s+", " ", cleaned).strip() or self.title

    @property
    def safe_id(self) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]", "_", self.spotify_id.strip())[:100]

    @property
    def id_filename(self) -> str:
        return f"{self.safe_id}.mp3"

    @property
    def display_name(self) -> str:
        return f"{self.artist} - {self.title}"

    @property
    def formatted_duration(self) -> str:
        if self.duration_sec <= 0:
            return "AUTO"
        return format_duration(self.duration_sec)

    @property
    def release_year(self) -> str:
        return self.release_date[:4] if len(self.release_date) >= 4 else "N/A"

    @property
    def has_full_meta(self) -> bool:
        return bool(self.isrc and self.release_year != "N/A")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Self:
        return cls(
            spotify_id=data["spotify_id"],
            title=data["title"],
            artist=data["artist"],
            artists_all=data.get("artists_all") or [data["artist"]],
            album=data.get("album") or "Single",
            album_artist=data.get("album_artist") or data["artist"],
            release_date=data.get("release_date") or "",
            track_number=str(data.get("track_number") or "1"),
            disc_number=str(data.get("disc_number") or "1"),
            duration_sec=int(data.get("duration_sec") or 0),
            isrc=data.get("isrc"),
            cover_url=data.get("cover_url"),
            direct_url=data.get("direct_url"),
        )


def is_valid_spotify_track(meta: TrackMeta) -> bool:
    if meta.spotify_id.startswith("custom_") or meta.direct_url or not FILTER_UNAVAILABLE_SPOTIFY:
        return True
    if len(meta.spotify_id) != 22:
        return False
    if meta.artist.strip().lower() in ("", "unknown", "неизвестный исполнитель"):
        return False
    if meta.title.strip().lower() in ("", "unknown"):
        return False
    return meta.duration_sec > 0


def parse_jsonc(text: str) -> Any:
    pattern = r'("(?:\\.|[^"\\])*")|/\*.*?\*/|//[^\r\n]*'
    cleaned = re.sub(pattern, lambda m: m.group(1) if m.group(1) else "", text, flags=re.DOTALL)
    cleaned = re.sub(r",\s*([\}\]])", r"\1", cleaned)
    return json.loads(cleaned)


def create_empty_cache() -> dict[str, Any]:
    return {
        "version": 4,
        "playlist_url": PLAYLIST_URL,
        "snapshot_id": "",
        "is_full_playlist": False,
        "raw_playlist_total": 0,
        "filtered_unavailable_count": 0,
        "updated_at": int(time.time()),
        "tracks": {},
    }


def load_folder_cache() -> dict[str, Any]:
    if not CACHE_FILE_PATH.exists():
        return create_empty_cache()
    try:
        raw = parse_jsonc(CACHE_FILE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "tracks" in raw:
            return raw
    except Exception as e:
        logger.warning(f"Файл кэша {CACHE_FILE_PATH.name} поврежден ({e}), создаем новый.")
    return create_empty_cache()


def save_folder_cache_unlocked(cache_data: dict[str, Any]) -> None:
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cache_data["updated_at"] = int(time.time())
        tmp_path = OUTPUT_DIR / f"{CACHE_FILENAME}.tmp"
        tmp_path.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(CACHE_FILE_PATH)
        try:
            os.chown(CACHE_FILE_PATH, PUID, PGID)
        except PermissionError:
            pass
    except OSError as e:
        logger.warning(f"Не удалось записать файл кэша {CACHE_FILE_PATH}: {e}")


def save_folder_cache(cache_data: dict[str, Any]) -> None:
    with cache_lock:
        save_folder_cache_unlocked(cache_data)


def reconcile_cache_with_disk(cache_data: dict[str, Any]) -> tuple[dict[str, Path], int]:
    token_ctx = current_ctx.set("CACHE")
    try:
        tracks_cache: dict[str, Any] = cache_data.setdefault("tracks", {})
        verified_on_disk: dict[str, Path] = {}

        for sp_id, entry in list(tracks_cache.items()):
            meta_dict = entry.get("meta") or {}
            if FILTER_UNAVAILABLE_SPOTIFY and not str(sp_id).startswith("custom_"):
                artist_chk = str(meta_dict.get("artist") or "").strip().lower()
                if artist_chk in ("", "unknown") and not entry.get("downloaded"):
                    del tracks_cache[sp_id]
                    continue

            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", str(sp_id).strip())[:100]
            fname = f"{safe_id}.mp3"
            fpath = OUTPUT_DIR / fname
            entry["filename"] = fname

            meta_dur = int(meta_dict.get("duration_sec") or 0)
            is_valid_size_and_dur = False
            if fpath.exists() and fpath.stat().st_size > 50_000:
                if meta_dur > 35:
                    try:
                        actual_dur = MP3(fpath).info.length
                        if actual_dur >= min(25.0, meta_dur * 0.6):
                            is_valid_size_and_dur = True
                        else:
                            logger.warning(
                                f"[DISK SANITY] Файл {fname} длится всего {format_duration(actual_dur)} "
                                f"(эталон: {format_duration(meta_dur)}). Помечаем для перекачки!"
                            )
                    except Exception:
                        is_valid_size_and_dur = True
                else:
                    is_valid_size_and_dur = True

            if is_valid_size_and_dur:
                entry["downloaded"] = True
                verified_on_disk[sp_id] = fpath
            else:
                entry["downloaded"] = False
                if IGNORE_CACHED_URLS and entry.get("source_type") != "spotitracks":
                    entry["source_url"] = None

        recovered_count = 0
        if OUTPUT_DIR.exists():
            for mp3_file in OUTPUT_DIR.glob("*.mp3"):
                stem = mp3_file.stem
                if stem in verified_on_disk:
                    continue
                if (len(stem) == 22 and stem.isalnum()) or stem.startswith("custom_"):
                    if mp3_file.stat().st_size <= 50_000:
                        continue
                    verified_on_disk[stem] = mp3_file
                    if stem not in tracks_cache:
                        try:
                            easy = EasyID3(mp3_file)
                            title = easy.get("title", [stem])[0]
                            artist = easy.get("artist", ["Unknown"])[0]
                            album = easy.get("album", ["Single"])[0]
                            audio_len = int(MP3(mp3_file).info.length)
                        except Exception:
                            title, artist, album, audio_len = stem, "Unknown", "Single", 0

                        tracks_cache[stem] = {
                            "meta": asdict(
                                TrackMeta(
                                    spotify_id=stem,
                                    title=title,
                                    artist=artist.split(",")[0].strip(),
                                    artists_all=[a.strip() for a in artist.split(",")],
                                    album=album,
                                    album_artist=artist.split(",")[0].strip(),
                                    release_date="",
                                    track_number="1",
                                    disc_number="1",
                                    duration_sec=audio_len,
                                    isrc=None,
                                    cover_url=None,
                                )
                            ),
                            "filename": mp3_file.name,
                            "source_url": None,
                            "source_type": "disk",
                            "score": 100,
                            "downloaded": True,
                            "file_size": mp3_file.stat().st_size,
                            "synced_at": int(time.time()),
                        }
                        recovered_count += 1

        if recovered_count > 0:
            save_folder_cache(cache_data)

        pending_in_cache = sum(1 for e in tracks_cache.values() if not e.get("downloaded"))
        is_full = cache_data.get("is_full_playlist", False)
        logger.info(
            f"Кэш {CACHE_FILE_PATH.name} проверен: в базе: {len(tracks_cache)} (полный: {yn(is_full)}) | "
            f"на диске: {len(verified_on_disk)} | ожидают докачки: {pending_in_cache}"
        )
        return verified_on_disk, pending_in_cache
    finally:
        current_ctx.reset(token_ctx)


def extract_spotify_track_id(raw: str) -> str | None:
    s = raw.strip()
    if not s or "EXAMPLE" in s.upper():
        return None
    if m := re.search(r"(?:track/|spotify:track:)([a-zA-Z0-9]{22})", s):
        return m.group(1)
    if len(s) == 22 and s.isalnum():
        return s
    return None


def parse_raw_ignore_entries(raw_ignore: Any) -> list[tuple[str, str]]:
    parsed: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    if isinstance(raw_ignore, list):
        for item in raw_ignore:
            if isinstance(item, str):
                if (sp_id := extract_spotify_track_id(item)) and sp_id not in seen_ids:
                    seen_ids.add(sp_id)
                    parsed.append((sp_id, ""))
            elif isinstance(item, dict):
                k = str(item.get("id") or item.get("track") or item.get("url") or "")
                reason = str(item.get("reason") or item.get("comment") or "").strip()
                if (sp_id := extract_spotify_track_id(k)) and sp_id not in seen_ids:
                    seen_ids.add(sp_id)
                    parsed.append((sp_id, reason))
    elif isinstance(raw_ignore, dict):
        for k_raw, v_raw in raw_ignore.items():
            if (sp_id := extract_spotify_track_id(str(k_raw))) and sp_id not in seen_ids:
                seen_ids.add(sp_id)
                parsed.append((sp_id, str(v_raw or "").strip()))
    return parsed


def is_track_in_ignore_set(meta: TrackMeta, ignored_ids: set[str]) -> bool:
    return bool(ignored_ids and meta.spotify_id in ignored_ids)


def load_custom_spotitracks() -> tuple[dict[str, dict[str, Any]], list[TrackMeta], set[str], int]:
    token_ctx = current_ctx.set("SPOTITRACKS")
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if not CUSTOM_TRACKS_PATH.exists():
            template = {
                "_comment": "Поддерживается JSONC (комментарии // и /* */). В ignores указываются Spotify ID или ссылки на треки Spotify (приоритет выше overrides).",
                "ignores": [
                    "https://open.spotify.com/track/EXAMPLE_IGNORED_ID"
                ],
                "overrides": {
                    "https://open.spotify.com/track/EXAMPLE_ID": "https://www.youtube.com/watch?v=EXAMPLE",
                    "Artist - Title": "https://soundcloud.com/EXAMPLE",
                },
                "custom_tracks": [
                    {
                        "enabled": False,
                        "id": "custom_example_01",
                        "url": "https://www.youtube.com/watch?v=EXAMPLE",
                        "artist": "Custom Artist",
                        "title": "Custom Track Title",
                        "album": "Radio Exclusive",
                        "year": "2026",
                        "cover_url": "",
                    }
                ],
            }
            try:
                CUSTOM_TRACKS_PATH.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
                os.chown(CUSTOM_TRACKS_PATH, PUID, PGID)
            except OSError:
                pass
            return {}, [], set(), 0

        raw = parse_jsonc(CUSTOM_TRACKS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}, [], set(), 0

        raw_ignore_entries = parse_raw_ignore_entries(raw.get("ignores"))
        ignored_ids: set[str] = {sp_id for sp_id, _ in raw_ignore_entries}

        overrides_map: dict[str, dict[str, Any]] = {}
        unique_overrides = 0
        for k, val in (raw.get("overrides") or {}).items():
            if k.startswith("EXAMPLE_") or "EXAMPLE_ID" in k:
                continue
            key_norm = k.strip()
            url_val = val["url"].strip() if isinstance(val, dict) else str(val).strip()
            if not url_val or "EXAMPLE" in url_val:
                continue

            sp_id_ov = extract_spotify_track_id(key_norm)
            if sp_id_ov and sp_id_ov in ignored_ids:
                logger.info(
                    f"[SPOTITRACKS IGNORES] Трек {sp_id_ov} находится в списке ignores — "
                    f"переопределение ссылки в overrides проигнорировано."
                )
                continue

            entry_obj = {"url": url_val} if isinstance(val, str) else dict(val)
            entry_obj["url"] = url_val
            if sp_id_ov:
                overrides_map[sp_id_ov] = entry_obj
                overrides_map[sp_id_ov.lower()] = entry_obj

            overrides_map[key_norm] = entry_obj
            overrides_map[key_norm.lower()] = entry_obj
            unique_overrides += 1

        custom_tracks: list[TrackMeta] = []
        for idx, item in enumerate(raw.get("custom_tracks") or [], 1):
            if not isinstance(item, dict) or item.get("enabled") is False:
                continue
            url = (item.get("url") or "").strip()
            if not url or "EXAMPLE" in url:
                continue

            url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:10]
            raw_id = (item.get("id") or f"custom_{url_hash}").strip()
            custom_id = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_id)
            if not custom_id.startswith("custom_"):
                custom_id = f"custom_{custom_id}"

            artist = (item.get("artist") or "Custom Artist").strip()
            title = (item.get("title") or f"Custom Track #{idx}").strip()
            album = (item.get("album") or "Custom Radio Tracks").strip()
            year = str(item.get("year") or "").strip()
            cover = (item.get("cover_url") or "").strip() or None
            dur = int(item.get("duration_sec") or 0)

            ct_meta = TrackMeta(
                spotify_id=custom_id,
                title=title,
                artist=artist,
                artists_all=[a.strip() for a in artist.split(",") if a.strip()] or [artist],
                album=album,
                album_artist=artist.split(",")[0].strip(),
                release_date=year,
                track_number=str(idx),
                disc_number="1",
                duration_sec=dur,
                isrc=None,
                cover_url=cover,
                direct_url=url,
            )
            if not is_track_in_ignore_set(ct_meta, ignored_ids):
                custom_tracks.append(ct_meta)

        if raw_ignore_entries or unique_overrides or custom_tracks:
            logger.info(
                f"Загружен {CUSTOM_TRACKS_PATH.name}: в черном списке (ignores): {len(raw_ignore_entries)} | "
                f"переопределений ссылок: {unique_overrides} | кастомных треков: {len(custom_tracks)}"
            )
        return overrides_map, custom_tracks, ignored_ids, len(raw_ignore_entries)
    except Exception as e:
        logger.warning(f"Ошибка чтения {CUSTOM_TRACKS_PATH.name}: {e}")
        return {}, [], set(), 0
    finally:
        current_ctx.reset(token_ctx)


def apply_overrides_to_tracks(
    tracks: list[TrackMeta],
    overrides_map: dict[str, dict[str, Any]],
) -> list[TrackMeta]:
    if not overrides_map:
        return tracks
    applied = 0
    for t in tracks:
        full_name_key = f"{t.artist} - {t.title}".lower()
        ov = (
            overrides_map.get(t.spotify_id)
            or overrides_map.get(t.spotify_id.lower())
            or overrides_map.get(full_name_key)
        )
        if ov and ov.get("url"):
            t.direct_url = ov["url"].strip()
            if ov.get("album"):
                t.album = str(ov["album"]).strip()
            if ov.get("year"):
                t.release_date = str(ov["year"]).strip()
            if ov.get("cover_url"):
                t.cover_url = str(ov["cover_url"]).strip()
            applied += 1
    if applied > 0:
        logger.info(f"[SPOTITRACKS] Применены прямые ссылки из {CUSTOM_TRACKS_PATH.name} для {applied} треков.")
    return tracks


async def remove_cache_and_managed_files_async(wipe_all: bool = False) -> None:
    token_ctx = current_ctx.set("CLEANUP")
    try:
        mode_str = (
            "ПОЛНАЯ ОЧИСТКА ВЫХОДНОЙ ПАПКИ (--remove-cache --all)"
            if wipe_all
            else "ОЧИСТКА КЭША, СКРЫТЫХ ФАЙЛОВ И ТРЕКОВ (--remove-cache)"
        )
        logger.warning(f"=== ЗАПУЩЕНА {mode_str} ===")

        managed_ids: set[str] = set()
        if CACHE_FILE_PATH.exists():
            try:
                data = parse_jsonc(CACHE_FILE_PATH.read_text(encoding="utf-8"))
                managed_ids.update((data.get("tracks") or {}).keys())
            except Exception:
                pass

        preserved_spotitracks = CUSTOM_TRACKS_PATH.name
        mp3_filenames_to_delete: list[str] = []

        if OUTPUT_DIR.exists():
            for item in OUTPUT_DIR.iterdir():
                if item.name == preserved_spotitracks:
                    continue
                if item.is_file() and item.suffix.lower() == ".mp3":
                    stem = item.stem
                    if (
                        wipe_all
                        or stem in managed_ids
                        or (len(stem) == 22 and stem.isalnum())
                        or stem.startswith("custom_")
                    ):
                        mp3_filenames_to_delete.append(item.name)

        if mp3_filenames_to_delete and is_azuracast_configured():
            await unassign_tracks_from_azuracast_playlist(mp3_filenames_to_delete)

        removed_files = 0
        removed_hidden = 0
        mp3_delete_set = set(mp3_filenames_to_delete)

        if OUTPUT_DIR.exists():
            for item in list(OUTPUT_DIR.iterdir()):
                if item.name == preserved_spotitracks:
                    continue
                if wipe_all or item.name.startswith("."):
                    try:
                        if item.is_dir():
                            shutil.rmtree(item, ignore_errors=True)
                        else:
                            item.unlink()
                        if item.name.startswith("."):
                            removed_hidden += 1
                        else:
                            removed_files += 1
                    except OSError as e:
                        logger.error(f"Не удалось удалить {item.name}: {e}")
                    continue
                if item.is_file() and item.name in mp3_delete_set:
                    try:
                        item.unlink()
                        removed_files += 1
                    except OSError as e:
                        logger.error(f"Не удалось удалить {item.name}: {e}")

        FAILED_CACHE_FILE.unlink(missing_ok=True)
        FAILED_META_CACHE_FILE.unlink(missing_ok=True)
        logger.success(
            f"Очистка завершена! Удалено основных файлов/папок: {removed_files} шт. | "
            f"Удалено скрытых файлов кэша: {removed_hidden} шт. (Файл {preserved_spotitracks} сохранен)."
        )
    finally:
        current_ctx.reset(token_ctx)


def format_bytes(size: float) -> str:
    sign = "-" if size < 0 else ""
    size = abs(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0:
            return f"{sign}{size:.2f} {unit}"
        size /= 1024.0
    return f"{sign}{size:.2f} PB"


def format_speed(bps: float | None) -> str:
    if not bps or bps <= 0:
        return "N/A"
    return f"{format_bytes(bps)}/s"


def measure_directory_stats(directory: Path) -> tuple[int, int, int]:
    if not directory.exists():
        return 0, 0, 0
    total_bytes = 0
    mp3_count = 0
    for f in directory.rglob("*"):
        if f.is_file():
            try:
                total_bytes += f.stat().st_size
                if f.suffix.lower() == ".mp3":
                    mp3_count += 1
            except OSError:
                pass
    try:
        free_bytes = shutil.disk_usage(directory).free
    except OSError:
        free_bytes = 0
    return total_bytes, mp3_count, free_bytes


def calculate_smart_quarantine_ttl(reason: str, attempts: int, avg_speed_bps: float) -> float:
    if FAIL_TTL_HOURS <= 0:
        return 0.0
    r_low = reason.lower()
    if any(k in r_low for k in ("таймаут", "timeout", "зависание", "поток замер", "timed out", "connection", "network")):
        base_h = 1.0 if (0 < avg_speed_bps < 150 * 1024) else 2.0
        return min(base_h * max(1, attempts), 12.0)
    if any(k in r_low for k in ("format", "403", "sign in", "bot", "reloaded")):
        return min(6.0 * (1.5 ** max(0, attempts - 1)), FAIL_TTL_HOURS)
    base_not_found = min(24.0, FAIL_TTL_HOURS)
    return min(base_not_found * (2.0 ** max(0, attempts - 1)), max(FAIL_TTL_HOURS, 168.0))


def load_quarantine() -> dict[str, dict[str, Any]]:
    if not FAILED_CACHE_FILE.exists() or FAIL_TTL_HOURS <= 0:
        return {}
    try:
        data = parse_jsonc(FAILED_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        now = time.time()
        return {
            k: v
            for k, v in data.items()
            if isinstance(v, dict)
            and (now - float(v.get("time", now))) < (float(v.get("ttl_hours", FAIL_TTL_HOURS)) * 3600)
        }
    except Exception:
        return {}


def register_quarantine_failure(
    quarantine: dict[str, dict[str, Any]],
    spotify_id: str,
    reason: str,
) -> float:
    if is_shutting_down():
        return 0.0
    with quarantine_lock:
        prev = quarantine.get(spotify_id) or {}
        attempts = int(prev.get("attempts", 0)) + 1
        ttl_hours = calculate_smart_quarantine_ttl(reason, attempts, speed_tracker.avg_speed_bps)
        quarantine[spotify_id] = {
            "time": time.time(),
            "ttl_hours": round(ttl_hours, 2),
            "attempts": attempts,
            "reason": reason,
        }
        save_quarantine_unlocked(quarantine)
        return ttl_hours


def clear_quarantine_entry(quarantine: dict[str, dict[str, Any]] | None, spotify_id: str) -> None:
    if quarantine is None:
        return
    with quarantine_lock:
        if spotify_id in quarantine:
            del quarantine[spotify_id]
            save_quarantine_unlocked(quarantine)


def save_quarantine_unlocked(quarantine: dict[str, dict[str, Any]]) -> None:
    if FAIL_TTL_HOURS <= 0:
        return
    try:
        FAILED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_q = FAILED_CACHE_FILE.with_suffix(".tmp")
        tmp_q.write_text(json.dumps(quarantine, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_q.replace(FAILED_CACHE_FILE)
    except OSError as e:
        logger.warning(f"Не удалось сохранить кэш карантина: {e}")


def load_meta_quarantine() -> dict[str, dict[str, Any]]:
    if not FAILED_META_CACHE_FILE.exists() or META_FAIL_TTL_HOURS <= 0:
        return {}
    try:
        data = parse_jsonc(FAILED_META_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        now = time.time()
        return {
            k: v
            for k, v in data.items()
            if isinstance(v, dict)
            and (now - float(v.get("time", now))) < (float(v.get("ttl_hours", max(24.0, META_FAIL_TTL_HOURS))) * 3600)
        }
    except Exception:
        return {}


def save_meta_quarantine_unlocked(meta_quarantine: dict[str, dict[str, Any]]) -> None:
    if META_FAIL_TTL_HOURS <= 0:
        return
    try:
        FAILED_META_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_q = FAILED_META_CACHE_FILE.with_suffix(".tmp")
        tmp_q.write_text(json.dumps(meta_quarantine, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp_q.replace(FAILED_META_CACHE_FILE)
    except OSError as e:
        logger.warning(f"Не удалось сохранить кэш карантина метаданных: {e}")


def register_meta_quarantine_failure(
    meta_quarantine: dict[str, dict[str, Any]],
    spotify_id: str,
    service_fails_added: int,
    missing_fields: str,
    reason: str,
) -> float:
    if is_shutting_down() or META_FAIL_TTL_HOURS <= 0:
        return 0.0
    with meta_quarantine_lock:
        prev = meta_quarantine.get(spotify_id) or {}
        attempts = int(prev.get("attempts", 0)) + 1
        total_service_fails = int(prev.get("service_fails", 0)) + max(1, service_fails_added)
        base_ttl = max(24.0, META_FAIL_TTL_HOURS)
        ttl_hours = min(base_ttl * (2.0 ** max(0, attempts - 1)), max(base_ttl, 168.0))
        meta_quarantine[spotify_id] = {
            "time": time.time(),
            "ttl_hours": round(ttl_hours, 2),
            "attempts": attempts,
            "service_fails": total_service_fails,
            "missing": missing_fields,
            "reason": reason,
        }
        save_meta_quarantine_unlocked(meta_quarantine)
        return ttl_hours


def clear_meta_quarantine_entry(meta_quarantine: dict[str, dict[str, Any]] | None, spotify_id: str) -> None:
    if meta_quarantine is None:
        return
    with meta_quarantine_lock:
        if spotify_id in meta_quarantine:
            del meta_quarantine[spotify_id]
            save_meta_quarantine_unlocked(meta_quarantine)


def inspect_cookie_file_health(cookie_path: Path) -> tuple[bool, str]:
    if not cookie_path.exists():
        return False, f"Файл {cookie_path} не существует"
    try:
        content = cookie_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return False, f"Ошибка чтения {cookie_path}: {e}"

    auth_cookie_names = {"SAPISID", "__Secure-3PAPISID", "__Secure-1PSID", "__Secure-3PSID", "SID", "LOGIN_INFO"}
    found_auth: set[str] = set()
    expired_auth: set[str] = set()
    now_ts = int(time.time())
    total_cookies = 0

    for line in content.splitlines():
        line_s = line.strip()
        if not line_s or line_s.startswith("#"):
            continue
        parts = line_s.split("\t")
        if len(parts) >= 7:
            total_cookies += 1
            exp_str, name, val = parts[4], parts[5].strip(), parts[6].strip()
            if name in auth_cookie_names and val:
                try:
                    exp_ts = int(exp_str)
                    if 0 < exp_ts < now_ts:
                        expired_auth.add(name)
                    else:
                        found_auth.add(name)
                except ValueError:
                    found_auth.add(name)

    if total_cookies == 0:
        return False, "Файл куки пуст или имеет неверный формат (ожидается Netscape HTTP Cookie File)"
    if not found_auth:
        if expired_auth:
            return False, f"Срок действия авторизационных куки истек ({', '.join(sorted(expired_auth))})"
        return False, (
            f"В файле найдено {total_cookies} куки, но отсутствуют ключи авторизации аккаунта "
            f"(SAPISID / __Secure-3PSID / LOGIN_INFO)."
        )
    return True, f"Найдено {total_cookies} куки (ключи сессии: {', '.join(sorted(found_auth))})"


def convert_raw_cookie_header_to_netscape(raw_input: str) -> str:
    text = raw_input.strip()
    if "# Netscape HTTP Cookie File" in text or "\tTRUE\t/\t" in text or "\tFALSE\t/\t" in text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines[0].startswith("# Netscape HTTP Cookie File"):
            lines.insert(0, "# Netscape HTTP Cookie File")
        return "\n".join(lines) + "\n"

    text_unwrapped = re.sub(r"\\\s*\n\s*", " ", text)
    curl_matches = re.findall(r"(?:-H|--header)\s+\$?['\"]cookie:\s*([^'\"]+)['\"]", text_unwrapped, flags=re.I)
    if not curl_matches:
        curl_matches = re.findall(r"(?:-b|--cookie)\s+\$?['\"]([^'\"]+)['\"]", text_unwrapped, flags=re.I)

    cookie_str = "; ".join(curl_matches) if curl_matches else re.sub(r"^cookie:\s*", "", text_unwrapped, flags=re.I).strip().strip("'\"")
    cookie_dict: dict[str, str] = {}
    for chunk in cookie_str.split(";"):
        chunk = chunk.strip()
        if "=" in chunk and not chunk.startswith("curl "):
            k, v = chunk.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and not any(ch in k for ch in (" ", "\t", "\n", "'", '"')):
                cookie_dict[k] = v

    if not cookie_dict:
        raise ValueError("Не удалось найти пары key=value во введенных данных.")

    auth_keys = [k for k in cookie_dict if "PSID" in k or "SAPISID" in k or k == "LOGIN_INFO"]
    print(f"\n[ПАРСЕР] Распознано куки: {len(cookie_dict)} шт. | Аккаунт: {', '.join(auth_keys) or 'НЕТ'}")

    expiry = int(time.time()) + 365 * 24 * 3600
    netscape_lines = ["# Netscape HTTP Cookie File", "# Generated automatically by SpotiSync --auth", ""]
    for name, val in cookie_dict.items():
        secure = "TRUE" if (name.startswith(("__Secure", "__Host")) or "SAPISID" in name) else "FALSE"
        netscape_lines.append(f".youtube.com\tTRUE\t/\t{secure}\t{expiry}\t{name}\t{val}")
    return "\n".join(netscape_lines) + "\n"


def verify_youtube_cookies(cookie_path: Path) -> bool:
    print("\n[ПРОВЕРКА] Тестируем куки через yt-dlp...")
    ok_health, health_msg = inspect_cookie_file_health(cookie_path)
    if not ok_health:
        print(f"[ОШИБКА КУКИ] {health_msg}")
        return False

    adapter = YtdlpLoggerAdapter()
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": False,
        "cookiefile": str(cookie_path),
        "extract_flat": True,
        "playlistend": 5,
        "socket_timeout": 20,
        "logger": adapter,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/feed/subscriptions", download=False)
            entries = info.get("entries") if isinstance(info, dict) else None
            if entries is not None:
                print(f"[SUCCESS] Куки YouTube полностью рабочие! ({health_msg}). Сохранены в {cookie_path}")
                return True
            reason = adapter.last_error_msg or adapter.last_warning_msg or "YouTube не вернул ленту подписок"
            print(f"[WARN] Авторизация YouTube не подтверждена! Причина: {reason}")
            return False
    except Exception as e:
        print(f"[WARN] Ошибка при проверке сессии YouTube: {e}")
    return False


def run_interactive_auth() -> None:
    print("\n" + "#" * 60)
    print(f" МАСТЕР НАСТРОЙКИ КУКИ YOUTUBE (--auth) | {BUILD_VERSION}")
    print("#" * 60)
    print(f" • Статус YouTube Cookie: {'Да (' + str(YT_COOKIE_FILE) + ')' if YT_COOKIE_FILE.exists() else 'Нет'}")
    print("-" * 60)
    print("  [1] Вставить 'Copy as cURL' или строку 'Cookie:' из браузера (без расширений)")
    print("  [2] Вставить содержимое готового файла cookies.txt (Netscape формат)")
    choice = input("\nВаш выбор [1/2, по умолчанию 1]: ").strip() or "1"

    YT_COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    print("\n--- ИНСТРУКЦИЯ ---")
    if choice == "1":
        print("1. Откройте окно Инкогнито в браузере и войдите на https://www.youtube.com")
        print("2. Нажмите F12 (DevTools) -> вкладка 'Network' -> обновите страницу (F5).")
        print("3. Правой кнопкой на первый запрос 'www.youtube.com' -> Copy -> Copy as cURL (bash).")
        print("4. Сразу закройте окно Инкогнито (чтобы сессия не сбросилась).")
        print("5. Вставьте скопированный текст ниже и нажмите Enter, затем пустую строку или END:\n")
    else:
        print("Вставьте содержимое cookies.txt ниже и введите END на новой строке:\n")

    collected_lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "END" or (choice == "1" and not line.strip() and collected_lines):
            break
        collected_lines.append(line)

    try:
        netscape_content = convert_raw_cookie_header_to_netscape("\n".join(collected_lines))
        YT_COOKIE_FILE.write_text(netscape_content, encoding="utf-8")
        verify_youtube_cookies(YT_COOKIE_FILE)
    except Exception as e:
        print(f"\n[ОШИБКА] Не удалось разобрать введенные данные: {e}")
    print("\n[ГОТОВО] Настройка завершена!\n")


def parse_track_item(item: dict[str, Any]) -> TrackMeta | None:
    track = item.get("track") or item
    if not track or track.get("is_local") or not track.get("id"):
        return None

    artists = [a["name"].strip() for a in track.get("artists", []) if a.get("name") and a["name"].strip()]
    title = (track.get("name") or "").strip()
    dur_ms = int(track.get("duration_ms") or 0)

    if FILTER_UNAVAILABLE_SPOTIFY:
        if track.get("is_playable") is False or not artists or artists[0].lower() == "unknown" or not title or dur_ms <= 0:
            return None

    album_obj = track.get("album") or {}
    album_artists = [a["name"].strip() for a in album_obj.get("artists", []) if a.get("name")]
    images = album_obj.get("images") or []
    track_num = track.get("track_number") or 1
    total_tracks = album_obj.get("total_tracks")

    return TrackMeta(
        spotify_id=track["id"],
        title=title or "Unknown",
        artist=artists[0] if artists else "Unknown",
        artists_all=artists or ["Unknown"],
        album=album_obj.get("name") or "Single",
        album_artist=album_artists[0] if album_artists else (artists[0] if artists else "Unknown"),
        release_date=album_obj.get("release_date") or "",
        track_number=f"{track_num}/{total_tracks}" if total_tracks else str(track_num),
        disc_number=str(track.get("disc_number") or 1),
        duration_sec=dur_ms // 1000,
        isrc=track.get("external_ids", {}).get("isrc"),
        cover_url=images[0]["url"] if images else None,
    )


async def fetch_single_spotify_embed_meta(client: httpx.AsyncClient, sp_id: str) -> TrackMeta | None:
    try:
        r_emb = await client.get(
            f"https://open.spotify.com/embed/track/{sp_id}",
            headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"},
        )
        if r_emb.status_code != 200:
            return None
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', r_emb.text, re.DOTALL)
        if not m:
            return None
        entity = (
            json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
        )
        if not entity:
            return None

        title = (entity.get("title") or entity.get("name") or "").strip()
        artists_list = [
            (a.get("name") or "").strip()
            for a in (entity.get("artists") or [])
            if isinstance(a, dict) and (a.get("name") or "").strip()
        ]
        if not artists_list and entity.get("subtitle"):
            artists_list = [a.strip() for a in str(entity["subtitle"]).replace("\xa0", " ").split(",") if a.strip()]
        if not title or not artists_list:
            return None

        dur_sec = int(entity.get("duration") or 0) // 1000
        rel_iso = str((entity.get("releaseDate") or {}).get("isoString") or "")[:10]
        ext_isrc = (entity.get("externalIds") or {}).get("isrc")
        cover_sources = (entity.get("visualIdentity") or {}).get("image") or entity.get("coverArt", {}).get("sources") or []
        cover_url = cover_sources[0].get("url") if cover_sources and isinstance(cover_sources[0], dict) else None

        return TrackMeta(
            spotify_id=sp_id,
            title=title,
            artist=artists_list[0],
            artists_all=artists_list,
            album=title,
            album_artist=artists_list[0],
            release_date=rel_iso,
            track_number="1",
            disc_number="1",
            duration_sec=dur_sec,
            isrc=str(ext_isrc).strip() if ext_isrc else None,
            cover_url=cover_url,
        )
    except Exception:
        return None


async def fetch_embed_session_and_preview(
    client: httpx.AsyncClient,
    playlist_id: str,
) -> tuple[list[TrackMeta], str | None, str | None, int, int]:
    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    logger.info(f"Получаем гостевую сессию Веб-плеера через: {embed_url}")
    resp = await client.get(embed_url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"})
    resp.raise_for_status()

    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', resp.text, re.DOTALL)
    if not match:
        return [], None, None, 0, 0

    state = json.loads(match.group(1)).get("props", {}).get("pageProps", {}).get("state", {})
    session_obj = state.get("settings", {}).get("session", {})
    embed_access_token: str | None = session_obj.get("accessToken")
    embed_client_id: str | None = session_obj.get("clientId")

    if not embed_access_token and (m_tok := re.search(r'"accessToken"\s*:\s*"([^"]+)"', resp.text)):
        embed_access_token = m_tok.group(1)
    if not embed_client_id and (m_cid := re.search(r'"clientId"\s*:\s*"([a-f0-9]{32})"', resp.text)):
        embed_client_id = m_cid.group(1)

    entity = state.get("data", {}).get("entity", {})
    track_list = entity.get("trackList") or []
    raw_count = len(track_list)
    cover_images = entity.get("coverArt", {}).get("sources") or []
    default_cover = cover_images[0].get("url") if cover_images else None

    tracks: list[TrackMeta] = []
    filtered_out = 0
    for idx, item in enumerate(track_list, 1):
        uri = item.get("uri", "")
        if FILTER_UNAVAILABLE_SPOTIFY and (not uri.startswith("spotify:track:") or item.get("isPlayable") is False):
            filtered_out += 1
            continue
        sp_id = uri.split(":")[-1] if ":" in uri else ""
        if len(sp_id) != 22:
            filtered_out += 1
            continue
        title = (item.get("title") or "").strip()
        subtitle = (item.get("subtitle") or "").replace("\xa0", " ").strip()
        duration_sec = int(item.get("duration") or 0) // 1000
        if FILTER_UNAVAILABLE_SPOTIFY and (not title or not subtitle or subtitle.lower() == "unknown" or duration_sec <= 0):
            filtered_out += 1
            continue
        artists_all = [a.strip() for a in subtitle.split(",") if a.strip()] or ["Unknown"]
        tracks.append(
            TrackMeta(
                spotify_id=sp_id,
                title=title,
                artist=artists_all[0],
                artists_all=artists_all,
                album=entity.get("name") or "Spotify Playlist",
                album_artist=artists_all[0],
                release_date="",
                track_number=str(idx),
                disc_number="1",
                duration_sec=duration_sec,
                isrc=None,
                cover_url=default_cover,
            )
        )
    return tracks, embed_access_token, embed_client_id, raw_count, filtered_out


async def fetch_remote_snapshot_id(client: httpx.AsyncClient, playlist_id: str, web_token: str) -> str:
    try:
        r = await client.get(
            f"https://api.spotify.com/v1/playlists/{playlist_id}",
            headers={"Authorization": f"Bearer {web_token}", "User-Agent": BROWSER_UA},
            params={"fields": "snapshot_id", "market": SPOTIFY_MARKET},
        )
        if r.status_code == 200:
            return str(r.json().get("snapshot_id", ""))
    except Exception:
        pass
    return ""


async def get_spotify_client_token(client: httpx.AsyncClient, web_client_id: str | None) -> str | None:
    cid = web_client_id or "d8a5ed958d274c2e8ee717e6a4b0971d"
    payload = {
        "client_data": {
            "client_version": "1.2.52.442.g01893f92",
            "client_id": cid,
            "js_sdk_data": {
                "device_brand": "Apple",
                "device_model": "unknown",
                "os": "macos",
                "os_version": "10.15.7",
                "device_id": hashlib.md5(cid.encode()).hexdigest(),
                "device_type": "computer",
            },
        }
    }
    try:
        r = await client.post(
            "https://clienttoken.spotify.com/v1/clienttoken",
            json=payload,
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json", "Content-Type": "application/json"},
        )
        if r.status_code == 200 and (tok := r.json().get("granted_token", {}).get("token")):
            logger.info("Получен client-token для GraphQL Pathfinder.")
            return str(tok)
    except Exception as e:
        logger.debug(f"Не удалось получить client-token: {e}")
    return None


async def discover_dynamic_graphql_hashes(client: httpx.AsyncClient, playlist_id: str) -> list[str]:
    discovered: list[str] = []
    try:
        r = await client.get(f"https://open.spotify.com/playlist/{playlist_id}", headers={"User-Agent": BROWSER_UA}, follow_redirects=True)
        if r.status_code != 200:
            return discovered
        js_urls = re.findall(r'src="(https://[^"]+spotifycdn\.com/cdn/build/web-player/[^"]+\.js)"', r.text)
        for js_url in js_urls[:8]:
            try:
                jr = await client.get(js_url, headers={"User-Agent": BROWSER_UA})
                if jr.status_code == 200 and "fetchPlaylist" in jr.text:
                    matches = re.findall(
                        r'"fetchPlaylist"[^}]{0,140}"([a-f0-9]{64})"|"([a-f0-9]{64})"[^}]{0,140}"fetchPlaylist"',
                        jr.text,
                    )
                    for m1, m2 in matches:
                        h = m1 or m2
                        if h and h not in discovered:
                            discovered.append(h)
                            logger.info(f"Найден актуальный sha256Hash в JS-бандле Spotify: {h[:16]}...")
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"Ошибка сканирования JS-бандлов: {e}")
    return discovered


async def fetch_via_pathfinder_graphql(
    client: httpx.AsyncClient,
    playlist_id: str,
    web_token: str,
    web_client_id: str | None = None,
    pt_token: str = "",
) -> tuple[list[TrackMeta] | None, int, int, str | None]:
    logger.info("[СПОСОБ 1 | GRAPHQL] Запрашиваем все страницы плейлиста (500+ треков) через Pathfinder...")
    client_token = await get_spotify_client_token(client, web_client_id)
    headers = {
        "Authorization": f"Bearer {web_token}",
        "User-Agent": BROWSER_UA,
        "App-Platform": "WebPlayer",
        "Spotify-App-Version": "1.2.52.442.g01893f92",
        "Origin": "https://open.spotify.com",
        "Referer": "https://open.spotify.com/",
        "Accept": "application/json",
    }
    if client_token:
        headers["client-token"] = client_token

    gql_url = "https://api-partner.spotify.com/pathfinder/v1/query"
    hashes = [
        "b39f62e9b566aa849b1780927de1450f47e02c54abf1e66e513f96e849591e41",
        "73a3b3470804983e4d55d83cd6cc99715019228fd999d51429cc69473a18789d",
        "cd2275433b29f7316de76e7b5b5e060d77447eb324a62d6202b92629f2c27622",
    ]

    tracks: list[TrackMeta] = []
    filtered_local_count = 0
    offset, limit, total = 0, 100, 1
    dynamic_checked = False
    last_err_text = ""

    while offset < total and not is_shutting_down():
        variables: dict[str, Any] = {
            "uri": f"spotify:playlist:{playlist_id}",
            "offset": offset,
            "limit": limit,
            "enableWatchFeedEntrypoint": False,
        }
        if pt_token:
            variables["permissionToken"] = pt_token

        page_data = None
        for _ in range(2):
            for op_name in ("fetchPlaylist", "fetchPlaylistContents"):
                for h in hashes:
                    payload = {
                        "operationName": op_name,
                        "variables": variables,
                        "extensions": {"persistedQuery": {"version": 1, "sha256Hash": h}},
                    }
                    try:
                        r = await client.post(gql_url, headers=headers, json=payload)
                        if r.status_code == 200:
                            rj = r.json()
                            if "data" in rj and rj["data"].get("playlistV2"):
                                page_data = rj
                                break
                            last_err_text = r.text[:200]
                        else:
                            last_err_text = f"HTTP {r.status_code}: {r.text[:180]}"
                    except Exception as e:
                        last_err_text = str(e)
                if page_data:
                    break
            if page_data or dynamic_checked:
                break
            dynamic_checked = True
            if new_hashes := await discover_dynamic_graphql_hashes(client, playlist_id):
                hashes = new_hashes + hashes
            else:
                break

        if not page_data:
            logger.warning(f"GraphQL Pathfinder не отдал страницу (offset: {offset}). Ответ: {last_err_text}")
            break

        content = page_data.get("data", {}).get("playlistV2", {}).get("content", {})
        total = content.get("totalCount", 0)
        items = content.get("items", [])
        if not items:
            break

        for idx, item in enumerate(items, offset + 1):
            item_v2 = item.get("itemV2", {}).get("data", {})
            if (item_v2.get("__typename") if item_v2 else "") != "Track":
                filtered_local_count += 1
                continue
            if FILTER_UNAVAILABLE_SPOTIFY and item_v2.get("playability", {}).get("playable") is False:
                filtered_local_count += 1
                continue

            uri = item_v2.get("uri", "")
            sp_id = uri.split(":")[-1] if ":" in uri else ""
            if not sp_id or (FILTER_UNAVAILABLE_SPOTIFY and (not uri.startswith("spotify:track:") or len(sp_id) != 22)):
                filtered_local_count += 1
                continue

            title = (item_v2.get("name") or "").strip()
            artists_all = [
                a.get("profile", {}).get("name", "").strip()
                for a in item_v2.get("artists", {}).get("items", [])
                if a.get("profile", {}).get("name", "").strip()
            ]
            dur_ms = int(item_v2.get("trackDuration", {}).get("totalMilliseconds") or 0)
            if FILTER_UNAVAILABLE_SPOTIFY and (not title or not artists_all or artists_all[0].lower() == "unknown" or dur_ms <= 0):
                filtered_local_count += 1
                continue

            album_obj = item_v2.get("albumOfTrack", {})
            cover_sources = album_obj.get("coverArt", {}).get("sources", [])
            date_obj = album_obj.get("date") or {}

            tracks.append(
                TrackMeta(
                    spotify_id=sp_id,
                    title=title,
                    artist=artists_all[0],
                    artists_all=artists_all,
                    album=album_obj.get("name") or "Single",
                    album_artist=artists_all[0],
                    release_date=str(date_obj.get("isoString") or date_obj.get("year") or "")[:10],
                    track_number=str(item_v2.get("trackNumber") or idx),
                    disc_number=str(item_v2.get("discNumber") or 1),
                    duration_sec=dur_ms // 1000,
                    isrc=None,
                    cover_url=cover_sources[0].get("url") if cover_sources else None,
                )
            )

        offset += len(items)
        pct = int((offset / max(total, 1)) * 100)
        logger.info(
            f"[GRAPHQL PROGRESS] Прочитано из плейлиста: {min(offset, total)}/{total} ({pct}%) | "
            f"Доступно: {len(tracks)} | Отфильтровано: {filtered_local_count}"
        )

    return (tracks if tracks else None), total, filtered_local_count, client_token


def score_external_meta_candidate(
    cand_title: str,
    cand_artists: list[str],
    cand_duration_sec: int,
    cand_album: str,
    meta: TrackMeta,
) -> tuple[float, str]:
    if not cand_title.strip() or not cand_artists:
        return -1.0, "пустое название или артист"

    ver_ok, ver_reason = check_version_compatibility(cand_title, " ".join(cand_artists), cand_album, meta)
    if not ver_ok:
        return -1.0, f"несовпадение версии ({ver_reason})"

    cand_base = re.sub(r"[\(\[].*?[\)\]]", "", cand_title)
    cand_base = re.sub(r"\s+-\s+.*$", "", cand_base).strip() or cand_title.strip()

    sp_base_comp = compact_alnum(meta.base_title)
    cand_base_comp = compact_alnum(cand_base)
    sp_translit_comp = compact_alnum(transliterate_cyr_to_lat(meta.base_title))
    cand_translit_comp = compact_alnum(transliterate_cyr_to_lat(cand_base))

    title_exact = (
        (bool(sp_base_comp) and sp_base_comp == cand_base_comp)
        or (bool(sp_translit_comp) and sp_translit_comp == cand_translit_comp)
        or titles_phonetically_match(meta.base_title, cand_base)
        or titles_phonetically_match(meta.clean_title, cand_title)
    )

    dur_diff = abs(cand_duration_sec - meta.duration_sec) if (meta.duration_sec > 0 and cand_duration_sec > 0) else 0
    sp_alb_clean = re.sub(r"\s*-\s*(?:ep|single).*$", "", meta.album, flags=re.I).strip().lower()
    cand_alb_clean = re.sub(r"\s*-\s*(?:ep|single).*$", "", cand_album, flags=re.I).strip().lower()
    album_exact = bool(
        sp_alb_clean
        and cand_alb_clean
        and sp_alb_clean not in ("single", "spotify playlist")
        and (
            sp_alb_clean == cand_alb_clean
            or compact_alnum(sp_alb_clean) == compact_alnum(cand_alb_clean)
            or titles_phonetically_match(sp_alb_clean, cand_alb_clean)
        )
    )

    expected_artists = [a for a in meta.artists_all if a.strip()] + extract_expected_remixer_tokens(meta.title)
    primary_artist_match = any(artists_loosely_match(meta.artist, ca) for ca in cand_artists if ca.strip())
    any_artist_match = primary_artist_match or any(
        artists_loosely_match(sp_a, ca) for sp_a in expected_artists for ca in cand_artists if ca.strip()
    )

    if not any_artist_match:
        cjk_artist_cross = any(has_cjk_chars(meta.artist) != has_cjk_chars(ca) for ca in cand_artists if ca.strip())
        if cjk_artist_cross and title_exact and meta.duration_sec > 0 and cand_duration_sec > 0 and dur_diff <= 4:
            any_artist_match = True
        else:
            return -1.0, f"чужой артист ({', '.join(cand_artists[:2])} != {meta.artist})"

    if not title_exact:
        sp_tokens = normalize_tokens(meta.base_title)
        cand_tokens = set(normalize_tokens(cand_base)) | set(normalize_tokens(cand_title))
        overlap = (sum(1 for w in sp_tokens if w in cand_tokens) / len(sp_tokens)) if sp_tokens else 0.0
        substring_ok = (
            min(len(sp_base_comp), len(cand_base_comp)) >= 4
            and (sp_base_comp in cand_base_comp or cand_base_comp in sp_base_comp)
        ) or (
            min(len(sp_translit_comp), len(cand_translit_comp)) >= 4
            and (sp_translit_comp in cand_translit_comp or cand_translit_comp in sp_translit_comp)
        )
        cjk_title_cross = (
            has_cjk_chars(meta.base_title) != has_cjk_chars(cand_base)
            and primary_artist_match
            and ((meta.duration_sec > 0 and cand_duration_sec > 0 and dur_diff <= 5) or album_exact)
        )
        if overlap < 0.70 and not substring_ok and not cjk_title_cross:
            return -1.0, f"чужое название ('{cand_title}' != '{meta.title}')"

    if primary_artist_match and (title_exact or album_exact):
        max_dur_tol = 9
    elif any_artist_match and title_exact:
        max_dur_tol = 7
    else:
        max_dur_tol = 5

    if meta.duration_sec > 0 and cand_duration_sec > 0 and dur_diff > max_dur_tol:
        return -1.0, (
            f"разница длительности {format_duration(dur_diff)} > {format_duration(max_dur_tol)} "
            f"({format_duration(cand_duration_sec)} vs {format_duration(meta.duration_sec)})"
        )

    score = 100.0 - (dur_diff * 8.0)
    if primary_artist_match:
        score += 25.0
    if title_exact:
        score += 25.0
    if album_exact:
        score += 20.0
    if compact_alnum(cand_title) == compact_alnum(meta.clean_title):
        score += 10.0

    return score, "OK"


async def try_spotify_embed_track(client: httpx.AsyncClient, tr: TrackMeta) -> tuple[bool, str]:
    if tr.release_date and tr.isrc:
        return True, "OK"
    try:
        r_emb = await client.get(f"https://open.spotify.com/embed/track/{tr.spotify_id}", headers={"User-Agent": BROWSER_UA})
        if r_emb.status_code == 200:
            if m := re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', r_emb.text, re.DOTALL):
                entity = (
                    json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("state", {}).get("data", {}).get("entity", {})
                )
                if entity:
                    if (rel_iso := (entity.get("releaseDate") or {}).get("isoString")) and not tr.release_date:
                        tr.release_date = str(rel_iso)[:10]
                    if (ext_isrc := (entity.get("externalIds") or {}).get("isrc")) and not tr.isrc:
                        tr.isrc = str(ext_isrc).strip()
                    if tr.release_year != "N/A":
                        return tr.has_full_meta, f"Embed дал Год: {tr.release_year}"
        return False, f"Embed HTTP {r_emb.status_code}"
    except Exception as e:
        return False, f"Embed err ({type(e).__name__})"


async def enrich_via_apple_itunes(
    ext_client: httpx.AsyncClient,
    apple_sem: asyncio.Semaphore,
    tr: TrackMeta,
) -> tuple[bool, str]:
    async with apple_sem:
        try:
            await asyncio.sleep(1.2)
            q_term = f"{tr.artist} {tr.clean_title}"
            r = await ext_client.get(
                "https://itunes.apple.com/search",
                params={"term": q_term, "entity": "song", "limit": 10, "country": SPOTIFY_MARKET},
            )
            if r.status_code == 400 and SPOTIFY_MARKET.upper() != "US":
                r = await ext_client.get(
                    "https://itunes.apple.com/search",
                    params={"term": q_term, "entity": "song", "limit": 10, "country": "US"},
                )
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"

            results = r.json().get("results") or []
            if not results:
                return False, "0 результатов в iTunes"

            scored_items: list[tuple[float, dict[str, Any]]] = []
            last_reject = "не подошел по длительности/артисту"
            for item in results:
                sc, reason = score_external_meta_candidate(
                    cand_title=str(item.get("trackName") or item.get("trackCensoredName") or "").strip(),
                    cand_artists=[str(item.get("artistName") or "").strip()],
                    cand_duration_sec=int(item.get("trackTimeMillis") or 0) // 1000,
                    cand_album=str(item.get("collectionName") or "").strip(),
                    meta=tr,
                )
                if sc > 0:
                    scored_items.append((sc, item))
                else:
                    last_reject = reason

            if not scored_items:
                return False, last_reject

            scored_items.sort(key=lambda x: x[0], reverse=True)
            best_item = scored_items[0][1]

            if (rel_d := str(best_item.get("releaseDate") or "")[:10]) and not tr.release_date:
                tr.release_date = rel_d
            if (col_name := (best_item.get("collectionName") or "").strip()) and tr.album in ("Single", "Spotify Playlist", ""):
                tr.album = re.sub(r"\s*-\s*Single$", "", col_name, flags=re.I)

            if tr.release_year != "N/A":
                return True, f"получен Год: {tr.release_year}"
            return False, "в карточке iTunes отсутствует дата релиза"
        except Exception as e:
            return False, f"ошибка ({type(e).__name__})"


async def enrich_via_musicbrainz(
    ext_client: httpx.AsyncClient,
    mb_sem: asyncio.Semaphore,
    tr: TrackMeta,
) -> tuple[bool, str]:
    async with mb_sem:
        try:
            await asyncio.sleep(1.1)
            safe_art = re.sub(r'["\\+\-&|!(){}\[\]^~*?:\/]', " ", tr.artist).strip()
            safe_tit = re.sub(r'["\\+\-&|!(){}\[\]^~*?:\/]', " ", tr.base_title).strip()
            headers = {"User-Agent": "SpotiSyncRadio/7.3 ( https://github.com/spotisync )"}

            queries = [f'artist:"{safe_art}" AND recording:"{safe_tit}"']
            if len(safe_tit) >= 2:
                queries.append(f'recording:"{safe_tit}"')

            found_any_rec = False
            last_reject = "не подошел по фильтрам точности"

            for q_idx, lucene_q in enumerate(queries):
                if q_idx > 0:
                    await asyncio.sleep(1.1)
                r = await ext_client.get(
                    "https://musicbrainz.org/ws/2/recording",
                    params={"query": lucene_q, "fmt": "json", "limit": 10},
                    headers=headers,
                )
                if r.status_code != 200:
                    continue

                recordings = r.json().get("recordings") or []
                if not recordings:
                    continue
                found_any_rec = True

                scored_recs: list[tuple[float, dict[str, Any]]] = []
                for rec in recordings:
                    rec_title = str(rec.get("title") or "").strip()
                    if disambig := str(rec.get("disambiguation") or "").strip():
                        rec_title = f"{rec_title} ({disambig})"

                    rec_len_sec = int(rec.get("length") or 0) // 1000
                    has_isrc = bool(rec.get("isrcs"))

                    mb_artists: list[str] = []
                    for ac in rec.get("artist-credit") or []:
                        if isinstance(ac, dict):
                            if a_name := str(ac.get("name") or "").strip():
                                mb_artists.append(a_name)
                            art_obj = ac.get("artist") or {}
                            if isinstance(art_obj, dict):
                                if o_name := str(art_obj.get("name") or "").strip():
                                    mb_artists.append(o_name)
                                if sort_name := str(art_obj.get("sort-name") or "").strip():
                                    mb_artists.append(sort_name)
                                for al in art_obj.get("aliases") or []:
                                    if isinstance(al, dict) and (al_name := str(al.get("name") or "").strip()):
                                        mb_artists.append(al_name)

                    releases = rec.get("releases") or []
                    first_album = str(releases[0].get("title") or "") if releases and isinstance(releases[0], dict) else ""

                    sc, reason = score_external_meta_candidate(
                        cand_title=rec_title,
                        cand_artists=mb_artists,
                        cand_duration_sec=rec_len_sec,
                        cand_album=first_album,
                        meta=tr,
                    )
                    if sc > 0:
                        if rec_len_sec <= 0:
                            if not has_isrc:
                                last_reject = "в записи MusicBrainz нет ни длительности, ни ISRC"
                                continue
                            sc -= 15.0
                        scored_recs.append((sc + (30.0 if has_isrc else 0.0), rec))
                    else:
                        last_reject = reason

                if scored_recs:
                    scored_recs.sort(key=lambda x: x[0], reverse=True)
                    for _, best_rec in scored_recs:
                        if (isrc_list := best_rec.get("isrcs") or []) and not tr.isrc:
                            first_isrc = isrc_list[0]
                            tr.isrc = str(first_isrc.get("id") if isinstance(first_isrc, dict) else first_isrc).strip()
                        if (first_rel := str(best_rec.get("first-release-date") or "")[:10]) and not tr.release_date:
                            tr.release_date = first_rel
                        if tr.isrc:
                            return tr.has_full_meta, "OK (MusicBrainz)"

            if found_any_rec:
                return False, f"запись отклонена или без ISRC ({last_reject})"
            return False, "0 записей в MusicBrainz"
        except Exception as e:
            return False, f"ошибка ({type(e).__name__})"


async def enrich_single_track_via_deezer(
    ext_client: httpx.AsyncClient,
    dz_sem: asyncio.Semaphore,
    tr: TrackMeta,
) -> tuple[bool, str]:
    async def dz_get_json(url: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
        last_err = ""
        for retry in range(4):
            try:
                r = await ext_client.get(url, params=params)
                if r.status_code == 200:
                    rj = r.json()
                    if isinstance(rj, dict) and "error" in rj:
                        err_obj = rj["error"] or {}
                        err_code = err_obj.get("code")
                        if err_code == 4:
                            last_err = "Quota limit (code 4)"
                            await asyncio.sleep(1.2 * (retry + 1))
                            continue
                        return {}, f"Deezer error {err_code}: {err_obj.get('message', 'Unknown')}"
                    return (rj if isinstance(rj, dict) else {}), ""
                if r.status_code == 429:
                    last_err = "HTTP 429"
                    await asyncio.sleep(1.5 * (retry + 1))
                else:
                    last_err = f"HTTP {r.status_code}"
                    break
            except Exception as e:
                last_err = f"ошибка сети ({type(e).__name__})"
                await asyncio.sleep(0.5)
        return {}, last_err or "неизвестный сбой Deezer"

    async with dz_sem:
        await asyncio.sleep(0.12)
        q_strict = f'artist:"{tr.artist}" track:"{tr.clean_title}"'
        rj, err_s = await dz_get_json("https://api.deezer.com/search", params={"q": q_strict, "limit": 10})
        data: list[dict[str, Any]] = list(rj.get("data") or [])

        if not data and tr.clean_title != tr.base_title:
            rj_b, _ = await dz_get_json(
                "https://api.deezer.com/search",
                params={"q": f'artist:"{tr.artist}" track:"{tr.base_title}"', "limit": 10},
            )
            data = list(rj_b.get("data") or [])

        if not data:
            rj2, _ = await dz_get_json(
                "https://api.deezer.com/search", params={"q": f"{tr.artist} {tr.clean_title}", "limit": 10}
            )
            data = list(rj2.get("data") or [])

        if not data and len(tr.base_title) >= 3:
            rj3, err_t = await dz_get_json(
                "https://api.deezer.com/search", params={"q": f'track:"{tr.base_title}"', "limit": 12}
            )
            data = list(rj3.get("data") or [])
            if not data:
                return False, (err_t or err_s or "нет в каталоге Deezer")

        scored_candidates: list[tuple[float, int]] = []
        last_reject_reason = "нет подходящих совпадений"

        for cand in data:
            cand_id = cand.get("id")
            if not cand_id:
                continue
            c_title = str(cand.get("title") or cand.get("title_short") or "").strip()
            c_ver = str(cand.get("title_version") or "").strip()
            if c_ver and c_ver.lower() not in c_title.lower():
                c_title = f"{c_title} {c_ver}".strip()

            c_artist = str((cand.get("artist") or {}).get("name") or "").strip()
            sc, reason = score_external_meta_candidate(
                cand_title=c_title,
                cand_artists=[c_artist] if c_artist else [],
                cand_duration_sec=int(cand.get("duration") or 0),
                cand_album=str((cand.get("album") or {}).get("title") or "").strip(),
                meta=tr,
            )
            if sc > 0:
                scored_candidates.append((sc, int(cand_id)))
            else:
                last_reject_reason = reason

        if not scored_candidates:
            return False, f"отклонено ({last_reject_reason})"

        scored_candidates.sort(key=lambda x: x[0], reverse=True)

        for _, matched_id in scored_candidates[:2]:
            await asyncio.sleep(0.12)
            tj, err_t = await dz_get_json(f"https://api.deezer.com/track/{matched_id}")
            if not tj:
                last_reject_reason = f"ошибка карточки ID: {matched_id} ({err_t})"
                continue

            contributors = [
                str(c.get("name") or "").strip()
                for c in (tj.get("contributors") or [])
                if isinstance(c, dict) and c.get("name")
            ]
            main_art = str((tj.get("artist") or {}).get("name") or "").strip()
            all_dz_artists = list(dict.fromkeys(([main_art] if main_art else []) + contributors))
            full_title = str(tj.get("title") or "").strip()
            full_ver = str(tj.get("title_version") or "").strip()
            if full_ver and full_ver.lower() not in full_title.lower():
                full_title = f"{full_title} {full_ver}".strip()

            sc_full, reason_full = score_external_meta_candidate(
                cand_title=full_title,
                cand_artists=all_dz_artists,
                cand_duration_sec=int(tj.get("duration") or 0),
                cand_album=str((tj.get("album") or {}).get("title") or ""),
                meta=tr,
            )
            if sc_full < 0:
                last_reject_reason = reason_full
                continue

            if not tr.isrc and tj.get("isrc"):
                tr.isrc = str(tj["isrc"]).strip()
            rel_d = tj.get("release_date") or (tj.get("album") or {}).get("release_date")
            if not tr.release_date and rel_d and str(rel_d) != "0000-00-00":
                tr.release_date = str(rel_d).strip()
            if tr.album in ("Single", "Spotify Playlist", "") and (tj.get("album") or {}).get("title"):
                tr.album = str(tj["album"]["title"]).strip()

            if tr.has_full_meta:
                return True, "OK"

            missing_parts = []
            if not tr.isrc:
                missing_parts.append("пустой ISRC")
            if tr.release_year == "N/A":
                missing_parts.append("пустой год")
            last_reject_reason = f"найден ID: {matched_id}, но " + " и ".join(missing_parts)

        return False, last_reject_reason


async def enrich_tracks_metadata(
    client: httpx.AsyncClient,
    tracks: list[TrackMeta],
    web_token: str | None,
    cache_data: dict[str, Any],
    ignore_quarantine: bool = False,
) -> tuple[list[TrackMeta], int]:
    if not tracks:
        return tracks, 0

    cached_tracks = cache_data.get("tracks", {})
    meta_quarantine = load_meta_quarantine()
    enriched_result: list[TrackMeta | None] = []
    to_fetch_indices: list[int] = []
    pre_filtered = 0

    for i, t in enumerate(tracks):
        cached_meta = cached_tracks.get(t.spotify_id, {}).get("meta", {})
        if cached_meta.get("isrc") and cached_meta.get("release_date"):
            restored = TrackMeta.from_dict(cached_meta)
            restored.direct_url = t.direct_url
            if is_valid_spotify_track(restored):
                enriched_result.append(restored)
                clear_meta_quarantine_entry(meta_quarantine, t.spotify_id)
            else:
                enriched_result.append(None)
                pre_filtered += 1
        else:
            if cached_meta.get("release_date") and not t.release_date:
                t.release_date = cached_meta["release_date"]
            if cached_meta.get("isrc") and not t.isrc:
                t.isrc = cached_meta["isrc"]

            if is_valid_spotify_track(t):
                enriched_result.append(t)
                if (not t.isrc or t.release_year == "N/A") and not t.spotify_id.startswith("custom_"):
                    to_fetch_indices.append(i)
            else:
                enriched_result.append(None)
                pre_filtered += 1

    if not to_fetch_indices:
        logger.info("Метаданные всех треков (ISRC, альбомы, года) мгновенно взяты из локального кэша!")
        return [t for t in enriched_result if t is not None and is_valid_spotify_track(t)], pre_filtered

    total_to_enrich = len(to_fetch_indices)
    logger.info(
        f"Запускаем каскад обогащения метаданных (Spotify -> Deezer [Strict Scorer] -> MusicBrainz -> Apple Music) для {total_to_enrich} треков..."
    )

    headers = {"Authorization": f"Bearer {web_token}", "User-Agent": BROWSER_UA} if web_token else {}
    sp_sem = asyncio.Semaphore(6)
    dz_sem = asyncio.Semaphore(5)
    mb_sem = asyncio.Semaphore(1)
    apple_sem = asyncio.Semaphore(2)

    spotify_rest_cooldown_until: float = 0.0
    spotify_rest_long_ban_logged = False
    api_filtered = ok_spotify = ok_deezer = ok_mb_apple = partial_ok = failed_all = skipped_meta_q = processed_count = 0

    ext_limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
    async with httpx.AsyncClient(timeout=15.0, headers={"User-Agent": BROWSER_UA}, limits=ext_limits) as ext_client:

        async def enrich_single_track_chain(idx: int) -> None:
            nonlocal api_filtered, ok_spotify, ok_deezer, ok_mb_apple, partial_ok, failed_all, skipped_meta_q, processed_count
            nonlocal spotify_rest_cooldown_until, spotify_rest_long_ban_logged
            if is_shutting_down():
                return
            try:
                tr = enriched_result[idx]
                if not tr:
                    return

                sp_id = tr.spotify_id
                sp_reason = ""
                dz_reason = mb_reason = apple_reason = "не требовался"

                if web_token:
                    wait_cd = spotify_rest_cooldown_until - time.monotonic()
                    if wait_cd <= 15.0:
                        if wait_cd > 0:
                            await asyncio.sleep(wait_cd)
                        async with sp_sem:
                            wait_in = spotify_rest_cooldown_until - time.monotonic()
                            if wait_in <= 0:
                                try:
                                    await asyncio.sleep(0.08)
                                    r = await client.get(
                                        f"https://api.spotify.com/v1/tracks/{sp_id}",
                                        headers=headers,
                                        params={"market": SPOTIFY_MARKET},
                                    )
                                    if r.status_code == 200:
                                        parsed = parse_track_item(r.json())
                                        if parsed and is_valid_spotify_track(parsed):
                                            parsed.direct_url = tr.direct_url
                                            enriched_result[idx] = parsed
                                            tr = parsed
                                        elif FILTER_UNAVAILABLE_SPOTIFY and not tr.direct_url:
                                            enriched_result[idx] = None
                                            api_filtered += 1
                                            return
                                    elif r.status_code == 429:
                                        ra = float(r.headers.get("Retry-After") or 10.0)
                                        spotify_rest_cooldown_until = max(spotify_rest_cooldown_until, time.monotonic() + ra)
                                        sp_reason = f"REST 429 ({format_duration(ra)})"
                                        if ra > 15.0 and not spotify_rest_long_ban_logged:
                                            spotify_rest_long_ban_logged = True
                                            logger.warning(
                                                f"[SPOTIFY 429 BYPASS] REST /v1/tracks в тайм-ауте на {format_duration(ra)}. "
                                                f"Годы выпуска берем из Spotify Embed, а ISRC — из Deezer (Strict Scorer) и MusicBrainz!"
                                            )
                                    elif r.status_code in (400, 404) and FILTER_UNAVAILABLE_SPOTIFY and not tr.direct_url:
                                        enriched_result[idx] = None
                                        api_filtered += 1
                                        return
                                    else:
                                        sp_reason = f"REST HTTP {r.status_code}"
                                except Exception as e:
                                    sp_reason = f"REST err ({type(e).__name__})"
                            else:
                                sp_reason = f"REST 429 ({format_duration(wait_in)})"
                    else:
                        sp_reason = f"REST 429 ({format_duration(wait_cd)})"

                if tr.has_full_meta:
                    ok_spotify += 1
                    clear_meta_quarantine_entry(meta_quarantine, sp_id)
                    return

                if tr.release_year == "N/A":
                    async with sp_sem:
                        _, emb_msg = await try_spotify_embed_track(client, tr)
                    sp_reason = f"{sp_reason}, {emb_msg}".strip(", ")
                else:
                    sp_reason = f"{sp_reason}, Год: {tr.release_year} (GraphQL)".strip(", ")

                if tr.has_full_meta:
                    ok_spotify += 1
                    clear_meta_quarantine_entry(meta_quarantine, sp_id)
                    return

                if (not ignore_quarantine) and (sp_id in meta_quarantine):
                    skipped_meta_q += 1
                    if tr.isrc or tr.release_year != "N/A":
                        partial_ok += 1
                    else:
                        failed_all += 1
                    if LOG_SHOW_QUARANTINE or logger.isEnabledFor(logging.DEBUG):
                        mq_info = meta_quarantine[sp_id]
                        mq_ttl = float(mq_info.get("ttl_hours", max(24.0, META_FAIL_TTL_HOURS)))
                        mq_left = max(0.0, mq_ttl * 3600 - (time.time() - float(mq_info.get("time", time.time()))))
                        log_mq = logger.info if LOG_SHOW_QUARANTINE else logger.debug
                        log_mq(
                            f"[META-QUARANTINE] Пропуск внешних API ({format_duration(mq_left)} из {format_duration(mq_ttl * 3600)} осталось | "
                            f"фейлов сервисов: {int(mq_info.get('service_fails', 1))}) -> {tr.display_name}"
                        )
                    return

                service_fails = 0
                dz_ok, dz_reason = await enrich_single_track_via_deezer(ext_client, dz_sem, tr)
                if dz_ok and tr.has_full_meta:
                    ok_deezer += 1
                    clear_meta_quarantine_entry(meta_quarantine, sp_id)
                    return
                if not dz_ok:
                    service_fails += 1

                if not tr.isrc:
                    mb_ok, mb_reason = await enrich_via_musicbrainz(ext_client, mb_sem, tr)
                    if mb_ok and tr.has_full_meta:
                        ok_mb_apple += 1
                        clear_meta_quarantine_entry(meta_quarantine, sp_id)
                        return
                    if not tr.isrc:
                        service_fails += 1

                if tr.release_year == "N/A":
                    _, apple_reason = await enrich_via_apple_itunes(ext_client, apple_sem, tr)
                    if tr.has_full_meta:
                        ok_mb_apple += 1
                        clear_meta_quarantine_entry(meta_quarantine, sp_id)
                        return
                    if tr.release_year == "N/A":
                        service_fails += 1

                combined_ext_reason = f"Deezer: {dz_reason} | MusicBrainz: {mb_reason} | Apple: {apple_reason}"
                if tr.isrc or tr.release_year != "N/A":
                    partial_ok += 1
                    missing_what = "ISRC" if not tr.isrc else "год выпуска"
                    q_ttl_h = register_meta_quarantine_failure(
                        meta_quarantine, sp_id, service_fails, missing_what, combined_ext_reason
                    )
                    q_note = f" [в мета-карантин на {format_duration(q_ttl_h * 3600)}]" if q_ttl_h > 0 else ""
                    logger.warning(
                        f"[ENRICH WARN] Не найден {missing_what} для '{tr.display_name}' "
                        f"(ISRC: {tr.isrc or 'N/A'}, Год: {tr.release_year}){q_note} | "
                        f"Spotify: {sp_reason} | Deezer: {dz_reason} | MusicBrainz: {mb_reason}"
                    )
                else:
                    failed_all += 1
                    q_ttl_h = register_meta_quarantine_failure(
                        meta_quarantine, sp_id, service_fails, "ISRC+Год", combined_ext_reason
                    )
                    q_note = f" [в мета-карантин на {format_duration(q_ttl_h * 3600)}]" if q_ttl_h > 0 else ""
                    logger.warning(
                        f"[ENRICH FAIL] Не удалось получить ни ISRC, ни год для '{tr.display_name}'{q_note} | "
                        f"Spotify: {sp_reason} | {combined_ext_reason}"
                    )
            except Exception as e:
                failed_all += 1
                logger.warning(f"[ENRICH EXCEPTION] Сбой обогащения трека #{idx}: {e}")
            finally:
                processed_count += 1
                if (processed_count % 25 == 0 or processed_count == total_to_enrich) and not is_shutting_down():
                    pct = int((processed_count / total_to_enrich) * 100)
                    logger.info(
                        f"[ENRICH PROGRESS] Обработано: {processed_count}/{total_to_enrich} ({pct}%) | "
                        f"Полные (ISRC+Год) — Spotify: {ok_spotify}, Deezer: {ok_deezer}, MB/Apple: {ok_mb_apple} | "
                        f"Только Год: {partial_ok} | Пропуск (мета-карантин): {skipped_meta_q} | Отказов: {failed_all}"
                    )

        await asyncio.gather(*(enrich_single_track_chain(idx) for idx in to_fetch_indices))

    final_list = [t for t in enriched_result if t is not None and is_valid_spotify_track(t)]
    return final_list, (pre_filtered + api_filtered)


async def fetch_spotify_tracks_with_cache(
    playlist_source: str,
    cache_data: dict[str, Any],
    ignored_keys: set[str] | None = None,
    ignore_quarantine: bool = False,
) -> tuple[list[TrackMeta], str, bool, int, int, float, float]:
    token_ctx = current_ctx.set("SPOTIFY")
    try:
        match = re.search(r"playlist/([a-zA-Z0-9]+)", playlist_source)
        playlist_id = match.group(1) if match else playlist_source.strip()
        pt_match = re.search(r"[?&]pt=([a-zA-Z0-9]+)", playlist_source)
        pt_token = pt_match.group(1) if pt_match else ""

        limits = httpx.Limits(max_keepalive_connections=15, max_connections=25)
        async with httpx.AsyncClient(timeout=25.0, limits=limits) as client:
            t_parse_start = time.monotonic()
            embed_tracks, web_token, web_client_id, emb_raw_total, emb_filtered = await fetch_embed_session_and_preview(
                client, playlist_id
            )
            if not web_token and not embed_tracks:
                raise RuntimeError("Не удалось получить сессию с open.spotify.com!")

            remote_snapshot = await fetch_remote_snapshot_id(client, playlist_id, web_token) if web_token else ""
            cached_snapshot = cache_data.get("snapshot_id", "")
            cached_url = cache_data.get("playlist_url", "")
            cached_tracks_dict = cache_data.get("tracks", {})
            is_full_cached = cache_data.get("is_full_playlist", False)

            if (
                remote_snapshot
                and remote_snapshot == cached_snapshot
                and playlist_source == cached_url
                and is_full_cached
                and len(cached_tracks_dict) > 0
            ):
                parse_dt = max(time.monotonic() - t_parse_start, 0.01)
                all_valid_cached = [
                    tr_obj
                    for k, e in cached_tracks_dict.items()
                    if not str(k).startswith("custom_")
                    and is_valid_spotify_track(tr_obj := TrackMeta.from_dict(e["meta"]))
                ]
                tracks_from_cache = [
                    tr_obj
                    for tr_obj in all_valid_cached
                    if not (ignored_keys and is_track_in_ignore_set(tr_obj, ignored_keys))
                ]
                ign_cached_cnt = len(all_valid_cached) - len(tracks_from_cache)
                if ign_cached_cnt > 0:
                    logger.info(f"[SPOTITRACKS IGNORES] Исключено из кэша по списку ignores: {ign_cached_cnt} треков.")

                enrich_dt = 0.0
                enrich_filtered = 0
                incomplete_cnt = sum(
                    1 for t in tracks_from_cache if not t.has_full_meta and not t.spotify_id.startswith("custom_")
                )
                if incomplete_cnt > 0:
                    t_enrich_start = time.monotonic()
                    tracks_from_cache, enrich_filtered = await enrich_tracks_metadata(
                        client, tracks_from_cache, web_token, cache_data, ignore_quarantine=ignore_quarantine
                    )
                    enrich_dt = max(time.monotonic() - t_enrich_start, 0.01)

                raw_tot = int(cache_data.get("raw_playlist_total") or len(all_valid_cached))
                filt_cnt = max(
                    int(cache_data.get("filtered_unavailable_count") or 0) + ign_cached_cnt + enrich_filtered,
                    max(0, raw_tot - len(tracks_from_cache)),
                )
                full_meta_cnt = sum(1 for t in tracks_from_cache if t.has_full_meta)
                logger.info(
                    f"[CACHE HIT] Плейлист не изменился (snapshot_id: {remote_snapshot[:12]}..., "
                    f"проверено за {format_duration(parse_dt)}). "
                    f"Всего в плейлисте: {raw_tot} | Отфильтровано: {filt_cnt} | "
                    f"Финально доступно: {len(tracks_from_cache)} (полные ISRC+Год: {full_meta_cnt}/{len(tracks_from_cache)})"
                )
                return tracks_from_cache, remote_snapshot, True, raw_tot, filt_cnt, parse_dt, enrich_dt

            tracks: list[TrackMeta] = []
            raw_total = total_filtered = 0
            parse_dt = enrich_dt = 0.0

            if web_token:
                gql_tracks, gql_raw_total, gql_filtered, _ = await fetch_via_pathfinder_graphql(
                    client, playlist_id, web_token, web_client_id, pt_token
                )
                if gql_tracks and len(gql_tracks) >= len(embed_tracks):
                    parse_dt = max(time.monotonic() - t_parse_start, 0.01)
                    if ignored_keys:
                        before_ign = len(gql_tracks)
                        gql_tracks = [t for t in gql_tracks if not is_track_in_ignore_set(t, ignored_keys)]
                        ign_cnt = before_ign - len(gql_tracks)
                        if ign_cnt > 0:
                            gql_filtered += ign_cnt
                            logger.info(f"[SPOTITRACKS IGNORES] Пропущено до обогащения по списку ignores: {ign_cnt} треков.")
                    logger.info(
                        f"Плейлист получен со Spotify за {format_duration(parse_dt)} "
                        f"({int(max(1, len(gql_tracks))/parse_dt)} треков/сек): "
                        f"Всего: {gql_raw_total} | Доступно: {len(gql_tracks)} | Отфильтровано на старте: {gql_filtered}"
                    )
                    t_enrich_start = time.monotonic()
                    tracks, enrich_filtered = await enrich_tracks_metadata(
                        client, gql_tracks, web_token, cache_data, ignore_quarantine=ignore_quarantine
                    )
                    enrich_dt = max(time.monotonic() - t_enrich_start, 0.01)
                    raw_total = gql_raw_total or (len(tracks) + gql_filtered + enrich_filtered)
                    total_filtered = gql_filtered + enrich_filtered
                    cache_data["is_full_playlist"] = True

            if not tracks and embed_tracks:
                parse_dt = max(time.monotonic() - t_parse_start, 0.01)
                if ignored_keys:
                    before_ign = len(embed_tracks)
                    embed_tracks = [t for t in embed_tracks if not is_track_in_ignore_set(t, ignored_keys)]
                    emb_filtered += before_ign - len(embed_tracks)
                logger.info(
                    f"[СПОСОБ 2 | EMBED] Список из Embed-виджета получен за "
                    f"{format_duration(parse_dt)} (всего в виджете: {emb_raw_total})..."
                )
                t_enrich_start = time.monotonic()
                tracks, enrich_filtered = await enrich_tracks_metadata(
                    client, embed_tracks, web_token, cache_data, ignore_quarantine=ignore_quarantine
                )
                enrich_dt = max(time.monotonic() - t_enrich_start, 0.01)
                raw_total = emb_raw_total
                total_filtered = emb_filtered + enrich_filtered
                cache_data["is_full_playlist"] = False

            if not tracks:
                raise RuntimeError("Spotify вернул 0 доступных треков! Проверьте открытость плейлиста.")

            cache_data["raw_playlist_total"] = raw_total
            cache_data["filtered_unavailable_count"] = total_filtered

            full_meta_count = sum(1 for t in tracks if t.has_full_meta)
            only_isrc_count = sum(1 for t in tracks if t.isrc and t.release_year == "N/A")
            only_year_count = sum(1 for t in tracks if not t.isrc and t.release_year != "N/A")
            missing_both_count = len(tracks) - full_meta_count - only_isrc_count - only_year_count
            meta_q_count = len(load_meta_quarantine())

            logger.info(
                f"Обогащение метаданных завершено за {format_duration(enrich_dt)} "
                f"({len(tracks)/enrich_dt:.1f} треков/сек): "
                f"Всего доступно: {len(tracks)} | Успешно (ISRC+Год): {full_meta_count} | "
                f"Только ISRC: {only_isrc_count} | Только Год: {only_year_count} | "
                f"Не получено: {missing_both_count} | В мета-карантине: {meta_q_count}"
            )

            if (LOG_SHOW_PARSED_TRACKS or logger.isEnabledFor(logging.DEBUG)) and tracks:
                log_fn = logger.info if LOG_SHOW_PARSED_TRACKS else logger.debug
                width = len(str(len(tracks)))
                for idx, t in enumerate(tracks, 1):
                    log_fn(
                        f"[{idx:0{width}d}/{len(tracks)}] {t.display_name} | Альбом: {t.album} ({t.release_year}) | "
                        f"Трек: #{t.track_number} | {t.formatted_duration} | ISRC: {t.isrc or 'N/A'} | Файл: {t.id_filename}"
                    )

            return tracks, remote_snapshot, False, raw_total, total_filtered, parse_dt, enrich_dt
    finally:
        current_ctx.reset(token_ctx)


def keep_alive_youtube_cookies() -> None:
    if is_shutting_down():
        return
    token_ctx = current_ctx.set("COOKIES")
    try:
        if not YT_COOKIE_FILE.exists():
            logger.warning(
                f"Файл куки {YT_COOKIE_FILE} НЕ НАЙДЕН! "
                f"Скрипт будет работать без авторизации (треки 18+ могут быть недоступны). Запустите --auth для настройки."
            )
            return

        ok_health, health_msg = inspect_cookie_file_health(YT_COOKIE_FILE)
        if not ok_health:
            logger.error(f"Проверка файла куки не пройдена: {health_msg}! Обновите куки через --auth.")
            return

        adapter = YtdlpLoggerAdapter()
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": False,
            "cookiefile": str(YT_COOKIE_FILE),
            "extract_flat": True,
            "playlistend": 5,
            "socket_timeout": 20,
            "force_ipv4": True,
            "logger": adapter,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info("https://www.youtube.com/feed/subscriptions", download=False)

        entries = info.get("entries") if isinstance(info, dict) else None
        webpage_url = (info.get("webpage_url") or "") if isinstance(info, dict) else ""

        if entries is None or "ServiceLogin" in webpage_url or "accounts.google.com" in webpage_url:
            yt_reason = adapter.last_error_msg or adapter.last_warning_msg or "Сессия была сброшена или устарела"
            logger.error(f"НЕ УДАЛОСЬ подтвердить авторизацию YouTube ({YT_COOKIE_FILE})! Причина: {yt_reason}.")
        else:
            logger.success(f"Сессия YouTube успешно проверена и продлена ({health_msg}).")
    except Exception as e:
        logger.error(f"Ошибка при проверке токена авторизации YouTube ({YT_COOKIE_FILE}): {e}")
    finally:
        current_ctx.reset(token_ctx)


def normalize_tokens(s: str) -> list[str]:
    cleaned = re.sub(r"[^\w\s]", " ", s.lower())
    return [w for w in cleaned.split() if len(w) > 1]


def compact_alnum(s: str) -> str:
    return re.sub(r"[^\w]", "", s.lower())


def has_cjk_chars(s: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f\uac00-\ud7af]", s))


def extract_expected_remixer_tokens(title: str) -> list[str]:
    matches = re.findall(r"[\(\[]([^\)\]]*?(?:remix|mix|vip|flip|bootleg|vision)[^\)\]]*?)[\)\]]", title, flags=re.I)
    ignore = {"remix", "mix", "vip", "official", "audio", "video", "extended", "radio", "edit", "feat", "ft", "version"}
    return [w for m in matches for w in normalize_tokens(m) if w not in ignore and len(w) >= 3]


def check_version_compatibility(cand_title: str, uploader: str, description: str, meta: TrackMeta) -> tuple[bool, str]:
    title_lower = cand_title.lower()
    orig_title_lower = meta.title.lower()
    orig_combined = f"{orig_title_lower} {meta.album.lower()}"

    for word in STOP_WORDS:
        if contains_word_token(title_lower, word) and not contains_word_token(orig_combined, word):
            allowed_by_group = any(
                word in group_words and any(contains_word_token(orig_combined, gw) for gw in group_words)
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
    up_clean = uploader.lower().replace(" official", "").replace(" music", "").replace("youtube channel", "").strip()
    if not up_clean:
        return False
    all_names = [a.lower().strip() for a in meta.artists_all if a.strip()] + extract_expected_remixer_tokens(meta.title)
    return any(len(name) >= 2 and artists_loosely_match(name, up_clean) for name in all_names)


def has_content_id_music_match(entry: dict[str, Any], meta: TrackMeta) -> bool:
    yt_track = (entry.get("track") or "").strip()
    yt_artist = (entry.get("artist") or "").strip()
    if not yt_track or not yt_artist:
        return False
    if not any(artists_loosely_match(a, yt_artist) for a in meta.artists_all if a.strip()):
        return False
    base_sp = compact_alnum(meta.base_title)
    yt_tr_comp = compact_alnum(yt_track)
    if base_sp and yt_tr_comp and (base_sp in yt_tr_comp or yt_tr_comp in base_sp):
        return True
    alb_comp = compact_alnum(re.sub(r"\s*-\s*(?:ep|single).*$", "", meta.album, flags=re.I))
    return bool(alb_comp and len(alb_comp) >= 4 and (alb_comp in yt_tr_comp or yt_tr_comp in alb_comp))


def has_title_match(entry: dict[str, Any], meta: TrackMeta) -> bool:
    cand_title = entry.get("title") or ""
    entry_track = entry.get("track") or ""
    uploader = entry.get("uploader") or entry.get("channel") or ""
    description = entry.get("description") or ""

    if has_content_id_music_match(entry, meta):
        return True

    target_base = meta.base_title.lower().strip()
    cand_lower = cand_title.lower()
    track_lower = entry_track.lower()
    if target_base in cand_lower or (track_lower and target_base in track_lower):
        return True

    target_comp = compact_alnum(target_base)
    cand_comp = compact_alnum(cand_lower)
    track_comp = compact_alnum(track_lower)
    if target_comp and len(target_comp) >= 3 and (target_comp in cand_comp or (track_comp and target_comp in track_comp)):
        return True

    sp_album_clean = re.sub(r"\s*-\s*(?:ep|single).*$", "", meta.album, flags=re.I).strip()
    alb_comp = compact_alnum(sp_album_clean)
    if alb_comp and len(alb_comp) >= 4 and sp_album_clean.lower() not in ("single", "spotify playlist"):
        if alb_comp in cand_comp or (track_comp and alb_comp in track_comp):
            return True

    if has_cjk_chars(meta.title) and is_trusted_artist_or_remixer_channel(uploader, meta):
        return True

    first_desc_lines = "\n".join(description.splitlines()[:4]).lower()
    if target_comp and len(target_comp) >= 4 and target_comp in compact_alnum(first_desc_lines):
        return True

    target_words = normalize_tokens(target_base)
    if not target_words:
        return True
    cand_words = set(normalize_tokens(cand_lower)) | set(normalize_tokens(track_lower))
    return (sum(1 for w in target_words if w in cand_words) / len(target_words)) >= 0.70


def is_perfect_match(entry: dict[str, Any], meta: TrackMeta) -> tuple[bool, str]:
    if not entry or entry.get("drm"):
        return False, "пусто или DRM"
    duration = int(entry.get("duration") or 0)
    if duration <= 0:
        return False, "нет длительности"
    dur_diff = abs(duration - meta.duration_sec)
    if meta.duration_sec > 0 and dur_diff > 3:
        return False, f"разница длительности {format_duration(dur_diff)} > 3сек"

    cand_title = entry.get("title") or ""
    cand_title_lower = cand_title.lower()
    entry_album = (entry.get("album") or "").lower()
    entry_year = str(entry.get("release_year") or entry.get("upload_date", "")[:4] or "")
    uploader = entry.get("uploader") or entry.get("channel") or ""
    uploader_lower = uploader.lower()
    description = (entry.get("description") or "").lower()

    ver_ok, ver_reason = check_version_compatibility(cand_title, uploader, description, meta)
    if not ver_ok:
        return False, ver_reason
    if not has_title_match(entry, meta):
        return False, "название не совпадает с искомым треком"

    has_provided = "provided to youtube by" in description or "auto-generated by youtube" in description
    is_topic = uploader_lower.endswith("- topic") or "release - topic" in uploader_lower
    is_artist_ch = is_trusted_artist_or_remixer_channel(uploader, meta)
    has_cid_card = has_content_id_music_match(entry, meta)

    if not (has_provided or is_topic or is_artist_ch or has_cid_card):
        return False, "нет маркеров официального релиза"

    meta_album_lower = meta.album.lower().strip()
    album_matched = meta_album_lower not in ("", "single", "spotify playlist") and (
        meta_album_lower in entry_album or meta_album_lower in description or meta_album_lower in cand_title_lower
    )
    year_matched = meta.release_year != "N/A" and (
        meta.release_year == entry_year or meta.release_year in description or meta.release_year in cand_title_lower
    )

    details = [f"dur_diff: {format_duration(dur_diff)}"]
    if has_provided:
        details.append("ProvidedBy")
    if is_topic:
        details.append("Topic")
    if has_cid_card:
        details.append("ContentID_MusicCard")
    if is_artist_ch:
        details.append("Artist/Remixer_Channel")
    if album_matched:
        details.append(f"Album: '{meta.album}'")
    if year_matched:
        details.append(f"Year: {meta.release_year}")

    return True, ", ".join(details)


def score_candidate(entry: dict[str, Any], meta: TrackMeta, source_type: str) -> tuple[float, str]:
    if not entry:
        return -1.0, "пустой ответ"
    if entry.get("drm"):
        return -1.0, "защищен DRM"

    cand_title = entry.get("title") or "Unknown"
    entry_album = (entry.get("album") or "").lower()
    uploader = entry.get("uploader") or entry.get("channel") or "Unknown"
    description = (entry.get("description") or "").lower()
    duration = int(entry.get("duration") or 0)
    dur_diff = abs(duration - meta.duration_sec)
    title_lower = cand_title.lower()

    ver_ok, ver_reason = check_version_compatibility(cand_title, uploader, description, meta)
    if not ver_ok:
        return -1.0, ver_reason
    if 0 < duration < 30 and meta.duration_sec > 30:
        return -1.0, f"длительность {format_duration(duration)} слишком мала (тизер/шортс)"
    if meta.duration_sec > 0 and dur_diff > 8:
        return -1.0, (
            f"длина {format_duration(duration)} не равна эталону {format_duration(meta.duration_sec)} "
            f"(разница {format_duration(dur_diff)} > 8сек)"
        )
    if not has_title_match(entry, meta):
        return -1.0, f"чужое название трека ('{cand_title}' не совпадает с '{meta.clean_title}')"

    uploader_lower = uploader.lower()
    score = 50.0
    reasons = ["base:50"]

    dur_penalty = dur_diff * 2.5 if meta.duration_sec > 0 else 0.0
    if dur_penalty > 0:
        score -= dur_penalty
        reasons.append(f"dur_diff:{format_duration(dur_diff)}(-{int(dur_penalty)})")
    else:
        score += 15.0
        reasons.append("dur:exact(+15)")

    if "provided to youtube by" in description or "auto-generated by youtube" in description:
        score += 60.0
        reasons.append("studio_master:+60")
    if has_content_id_music_match(entry, meta):
        score += 55.0
        reasons.append("content_id_card:+55")
    if uploader_lower.endswith("- topic"):
        score += 45.0
        reasons.append("youtube_topic:+45")
    elif is_trusted_artist_or_remixer_channel(uploader, meta):
        score += 70.0
        reasons.append("artist_or_remixer_ch:+70")

    if any(
        sym in description
        for sym in ("℗", "©", "phonographic copyright", "sony music", "universal music", "warner music", "первое музыкальное")
    ):
        score += 20.0
        reasons.append("label_copyright:+20")

    meta_album_lower = meta.album.lower().strip()
    if meta_album_lower not in ("", "single", "spotify playlist") and (
        meta_album_lower in entry_album or meta_album_lower in description
    ):
        score += 25.0
        reasons.append("album_match:+25")

    if meta.release_year != "N/A" and (meta.release_year in description or meta.release_year in title_lower):
        score += 15.0
        reasons.append(f"year_match({meta.release_year}):+15")

    if any(k in title_lower for k in ("official audio", "official video", "official music video", "official lyric video")):
        score += 25.0
        reasons.append("official_tag:+25")

    if any(artists_loosely_match(a, title_lower) or a.lower() in description[:250] for a in meta.artists_all):
        score += 15.0
        reasons.append("artist_mentioned:+15")

    if source_type == "soundcloud":
        score += 3.0
        reasons.append("src:sc(+3)")

    return score, ", ".join(reasons)


def build_ydl_opts(attempt_idx: int, isolated_cookie_path: Path | None, for_search: bool = False) -> dict[str, Any]:
    profile_cfg = YT_CLIENT_PROFILES[attempt_idx % len(YT_CLIENT_PROFILES)]
    extractor_args: dict[str, Any] = {
        "youtube": {"player_client": profile_cfg["clients"]},
        "youtubetab": {"skip": ["authcheck"]},
    }
    if POT_PROVIDER_URL:
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [POT_PROVIDER_URL]}

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "force_ipv4": True,
        "socket_timeout": YTDLP_SOCKET_TIMEOUT_SEC,
        "retries": YTDLP_RETRIES,
        "fragment_retries": YTDLP_RETRIES,
        "extractor_retries": YTDLP_RETRIES,
        "file_access_retries": YTDLP_RETRIES,
        "nocheckcertificate": True,
        "extractor_args": extractor_args,
        "logger": YtdlpLoggerAdapter(),
    }
    if for_search:
        opts["ignore_no_formats_error"] = True
        opts["skip_download"] = True
    if profile_cfg.get("use_cookies", True):
        if isolated_cookie_path and isolated_cookie_path.exists():
            opts["cookiefile"] = str(isolated_cookie_path)
        elif YT_COOKIE_FILE.exists():
            opts["cookiefile"] = str(YT_COOKIE_FILE)
    return opts


def build_ffmpeg_postprocessor_args() -> list[str]:
    filters: list[str] = []
    if AUDIO_TRIM_SILENCE:
        filters.append(
            "silenceremove="
            "start_periods=1:start_duration=0.05:start_threshold=-72dB:start_silence=0.25:"
            "stop_periods=1:stop_duration=3.0:stop_threshold=-72dB:stop_silence=0.5"
        )
    if AUDIO_NORMALIZE:
        filters.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    args = ["-ar", "44100", "-ac", "2"]
    if filters:
        args.extend(["-af", ",".join(filters)])
    return args


def make_progress_hooks(
    source_label: str,
    watchdog: AdaptiveWorkerWatchdog | None = None,
) -> tuple[Callable[[dict[str, Any]], None], Callable[[dict[str, Any]], None]]:
    last_milestone = -1
    started_logged = pp_logged = False

    def download_hook(d: dict[str, Any]) -> None:
        nonlocal last_milestone, started_logged
        status = d.get("status")
        if status == "downloading":
            downloaded = int(d.get("downloaded_bytes") or 0)
            total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            speed = d.get("speed")
            eta = d.get("eta")
            if speed and speed > 0:
                speed_tracker.record_speed(float(speed))
            if watchdog:
                watchdog.update_download(downloaded, total, speed)
            if not LOG_DOWNLOAD_PROGRESS or is_shutting_down():
                return
            if not started_logged:
                started_logged = True
                logger.info(f"[{source_label}] Начато скачивание аудио ({format_bytes(total) if total > 0 else 'поток'})...")
            if total > 0:
                pct = int((downloaded / total) * 100)
                milestone = (pct // 25) * 25
                if milestone > last_milestone and milestone in (25, 50, 75):
                    last_milestone = milestone
                    logger.info(
                        f"[{source_label}] Прогресс: {pct:3d}% ({format_bytes(downloaded)} / {format_bytes(total)}) | "
                        f"Скорость: {format_speed(speed)} | ETA: {format_duration(eta) if eta is not None else '?'}"
                    )
        elif status == "finished":
            if watchdog:
                watchdog.enter_ffmpeg()
            if LOG_DOWNLOAD_PROGRESS and not is_shutting_down():
                total_dl = d.get("total_bytes") or d.get("downloaded_bytes") or 0
                elapsed_dl = float(d.get("elapsed") or 0.0)
                logger.info(
                    f"[{source_label}] Прогресс: 100% "
                    f"({format_bytes(total_dl)} скачано за {format_duration(elapsed_dl)})"
                )

    def postprocessor_hook(d: dict[str, Any]) -> None:
        nonlocal pp_logged
        if watchdog:
            watchdog.enter_ffmpeg()
        if LOG_DOWNLOAD_PROGRESS and not is_shutting_down() and d.get("status") == "started" and not pp_logged:
            pp_logged = True
            logger.info(
                f"[FFMPEG] Мастеринг в MP3 320kbps (44.1kHz, обрезка тишины: {yn(AUDIO_TRIM_SILENCE)}, "
                f"нормализация -14 LUFS: {yn(AUDIO_NORMALIZE)})..."
            )

    return download_hook, postprocessor_hook


def clean_staging_artifacts(staging_base: Path) -> None:
    if staging_base.parent.exists():
        for f in staging_base.parent.glob(f"{staging_base.name}*"):
            if f.is_file():
                f.unlink(missing_ok=True)


def canonical_media_url_key(url: str | None) -> str:
    if not url:
        return ""
    u = url.strip()
    if not u:
        return ""
    if yt_m := re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([a-zA-Z0-9_-]{11})", u):
        return f"yt:{yt_m.group(1)}"
    u_clean = re.sub(r"^https?://(www\.|m\.)?", "", u.lower()).split("?")[0].rstrip("/")
    return u_clean


def is_same_media_url(url1: str | None, url2: str | None) -> bool:
    k1, k2 = canonical_media_url_key(url1), canonical_media_url_key(url2)
    return bool(k1 and k2 and k1 == k2)


def skip_if_already_downloaded_from_same_url(
    candidate_url: str,
    existing_mp3_path: Path,
    existing_disk_url: str,
    meta: TrackMeta,
    source_type: str,
    score: float,
    cache_data: dict[str, Any],
    quarantine: dict[str, dict[str, Any]] | None,
) -> bool:
    if not (
        existing_mp3_path.exists()
        and existing_mp3_path.stat().st_size > 50_000
        and is_same_media_url(candidate_url, existing_disk_url)
    ):
        return False

    logger.info(
        f"[SKIP SAME SOURCE] Поиск выбрал тот же источник ({candidate_url}), "
        f"который уже скачан в {existing_mp3_path.name} -> пропускаем повторное скачивание!"
    )
    try:
        tag_mp3_file(existing_mp3_path, meta)
    except Exception as e:
        logger.debug(f"Не удалось обновить теги существующего файла {existing_mp3_path.name}: {e}")

    with cache_lock:
        cache_data["tracks"][meta.spotify_id] = {
            "meta": asdict(meta),
            "filename": existing_mp3_path.name,
            "source_url": existing_disk_url or candidate_url,
            "override_url": meta.direct_url,
            "source_type": source_type,
            "score": int(score),
            "downloaded": True,
            "file_size": existing_mp3_path.stat().st_size,
            "synced_at": int(time.time()),
        }
        save_folder_cache_unlocked(cache_data)

    clear_quarantine_entry(quarantine, meta.spotify_id)
    return True


def download_with_retries(
    url: str,
    staging_base: Path,
    isolated_cookie: Path | None,
    source_type: str = "AUDIO",
    meta_to_enrich: TrackMeta | None = None,
    watchdog: AdaptiveWorkerWatchdog | None = None,
    allow_any_duration: bool = False,
) -> tuple[Path | None, str]:
    ffmpeg_args = build_ffmpeg_postprocessor_args()
    src_tag = source_type.upper()
    has_cookie = bool((isolated_cookie and isolated_cookie.exists()) or YT_COOKIE_FILE.exists())
    last_error = ""

    for attempt in range(YTDLP_RETRIES):
        if watchdog:
            watchdog.reset_for_download_attempt(f"download:{src_tag}:try#{attempt + 1}")
        clean_staging_artifacts(staging_base)

        profile_cfg = YT_CLIENT_PROFILES[attempt % len(YT_CLIENT_PROFILES)]
        use_c = profile_cfg.get("use_cookies", True) and has_cookie
        profile_name = "+".join(profile_cfg["clients"]) + (" [cookies:Да]" if use_c else " [cookies:Нет]")

        logger.info(f"[КАЧАЕМ | {src_tag}] Попытка {attempt + 1}/{YTDLP_RETRIES} (клиент: {profile_name}) -> {url}")

        dl_hook, pp_hook = make_progress_hooks(src_tag, watchdog=watchdog)
        dl_opts = build_ydl_opts(attempt, isolated_cookie, for_search=False)
        dl_opts.update({
            "format": "bestaudio/best",
            "outtmpl": f"{staging_base}.%(ext)s",
            "progress_hooks": [dl_hook],
            "postprocessor_hooks": [pp_hook],
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"}],
            "postprocessor_args": {"extractaudio": ffmpeg_args},
        })

        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                if attempt > 0:
                    ydl.cache.remove()
                info = ydl.extract_info(url, download=True)
                if source_type == "soundcloud":
                    reset_soundcloud_403()
                if meta_to_enrich and info:
                    if meta_to_enrich.title.startswith(("Custom Track #", "External Track")) and info.get("title"):
                        meta_to_enrich.title = info["title"]
                    if meta_to_enrich.artist in ("Custom Artist", "External Artist") and (
                        extracted_artist := (info.get("artist") or info.get("uploader"))
                    ):
                        meta_to_enrich.artist = extracted_artist
                        meta_to_enrich.artists_all = [extracted_artist]
                        meta_to_enrich.album_artist = extracted_artist
                    if not meta_to_enrich.cover_url and info.get("thumbnail"):
                        meta_to_enrich.cover_url = info["thumbnail"]
                    if meta_to_enrich.duration_sec <= 0 and info.get("duration"):
                        meta_to_enrich.duration_sec = int(info["duration"])

            mp3_file = Path(f"{staging_base}.mp3")
            if mp3_file.exists() and mp3_file.stat().st_size > 50_000:
                try:
                    audio_len = MP3(mp3_file).info.length
                    target_min = (
                        15.0
                        if allow_any_duration
                        else (
                            min(25.0, meta_to_enrich.duration_sec * 0.6)
                            if (meta_to_enrich and meta_to_enrich.duration_sec > 35)
                            else 10.0
                        )
                    )
                    if audio_len < target_min:
                        err_msg = (
                            f"Скачанный файл слишком короткий "
                            f"({format_duration(audio_len)} < {format_duration(target_min)}) — тизер/шортс"
                        )
                        logger.warning(f"[{src_tag}] {err_msg}. Отбраковываем.")
                        mp3_file.unlink(missing_ok=True)
                        last_error = err_msg
                        continue
                except Exception:
                    pass
                return mp3_file, ""
        except TimeoutError:
            raise
        except Exception as e:
            if is_shutting_down() or (watchdog and (watchdog.aborted_by_watchdog or watchdog.hard_cancelled)):
                raise TimeoutError((watchdog.abort_reason if watchdog else "") or str(e)) from e
            err_str = str(e)
            last_error = err_str
            if "DRM protected" in err_str:
                logger.info(f"[{src_tag}] Источник {url} защищен DRM, переключаемся на следующий вариант.")
                return None, "Защищен DRM (невозможно скачать)"
            if any(
                unavail in err_str
                for unavail in (
                    "Video unavailable",
                    "This video is not available",
                    "Private video",
                    "Video is no longer available",
                    "has been removed by the uploader",
                )
            ):
                if attempt == 0 and has_cookie and not use_c and YTDLP_RETRIES > 1:
                    logger.info(f"[{src_tag}] Видео недоступно без авторизации, мгновенно пробуем профиль с куками...")
                    continue
                logger.warning(f"[{src_tag}] Видео {url} недоступно для IP сервера (Geo-Block / Удалено) — пропускаем без ожидания.")
                return None, err_str
            if "Sign in to confirm your age" in err_str and not use_c:
                logger.info(f"[{src_tag}] Ролик 18+ (Age-Gate), мгновенно переключаемся на профиль с куками...")
                continue
            if "Requested format is not available" in err_str and attempt + 1 < YTDLP_RETRIES:
                logger.info(f"[{src_tag}] Клиент {profile_name} не отдал аудиопоток, мгновенно пробуем следующий профиль...")
                continue
            wait_sec = float(2**attempt)
            logger.warning(
                f"[RETRY {attempt + 1}/{YTDLP_RETRIES}] Сбой на {url} ({profile_name}): {e}. "
                f"Пауза {format_duration(wait_sec)}..."
            )
            if watchdog:
                watchdog.sleep_with_check(wait_sec)
            else:
                time.sleep(wait_sec)

    return None, last_error or "Превышено количество попыток загрузки (yt-dlp error)"


def finalize_staged_mp3(
    staged_mp3: Path,
    meta: TrackMeta,
    source_url: str,
    source_type: str,
    score: float,
    found_title: str,
    cache_data: dict[str, Any],
    quarantine: dict[str, dict[str, Any]] | None = None,
    watchdog: AdaptiveWorkerWatchdog | None = None,
) -> None:
    if watchdog:
        watchdog.enter_tagging()
    tag_mp3_file(staged_mp3, meta)

    with file_move_lock:
        final_path = OUTPUT_DIR / meta.id_filename
        shutil.move(staged_mp3, final_path)
        try:
            os.chown(final_path, PUID, PGID)
        except PermissionError:
            pass

    file_size = final_path.stat().st_size
    with cache_lock:
        cache_data["tracks"][meta.spotify_id] = {
            "meta": asdict(meta),
            "filename": final_path.name,
            "source_url": source_url,
            "override_url": meta.direct_url,
            "source_type": source_type,
            "score": int(score),
            "downloaded": True,
            "file_size": file_size,
            "synced_at": int(time.time()),
        }
        save_folder_cache_unlocked(cache_data)

    clear_quarantine_entry(quarantine, meta.spotify_id)
    logger.success(
        f"Готово! [{source_type.upper()} | {int(score)} pts | {format_bytes(file_size)}] "
        f"'{meta.display_name}' (Источник: '{found_title}' | {source_url}) -> {final_path.name}"
    )


def process_track_sync(
    meta: TrackMeta,
    worker_label: str,
    cache_data: dict[str, Any],
    watchdog: AdaptiveWorkerWatchdog,
    quarantine: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    if is_shutting_down():
        return False, "Остановка контейнера"

    token_ctx = current_ctx.set(worker_label)
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    temp_work_dir = Path(tempfile.mkdtemp(dir=STAGING_DIR, prefix=f"sp_{meta.safe_id}_"))
    isolated_cookie: Path | None = None

    show_scoring = LOG_SHOW_SCORING or logger.isEnabledFor(logging.DEBUG)
    score_log_fn = logger.info if LOG_SHOW_SCORING else logger.debug

    try:
        if YT_COOKIE_FILE.exists():
            isolated_cookie = temp_work_dir / "thread_cookies.txt"
            shutil.copy2(YT_COOKIE_FILE, isolated_cookie)

        with cache_lock:
            cached_entry = dict(cache_data.get("tracks", {}).get(meta.spotify_id, {}))

        existing_mp3_path = OUTPUT_DIR / meta.id_filename
        existing_disk_url = str(cached_entry.get("source_url") or "").strip()
        existing_disk_src_type = str(cached_entry.get("source_type") or "cached").strip()
        has_valid_existing_mp3 = existing_mp3_path.exists() and existing_mp3_path.stat().st_size > 50_000
        failed_url_keys: set[str] = set()

        logger.info(
            f"▶ Старт обработки: {meta.display_name} -> {meta.id_filename} "
            f"[Альбом: {meta.album} ({meta.release_year}) | Эталон: {meta.formatted_duration} | ISRC: {meta.isrc or 'N/A'}]"
        )

        if meta.direct_url:
            if skip_if_already_downloaded_from_same_url(
                meta.direct_url, existing_mp3_path, existing_disk_url, meta, "spotitracks", 100, cache_data, quarantine
            ):
                return True, ""

            logger.info(f"[DIRECT URL] Задан прямой источник: {meta.direct_url}")
            staged_mp3, err_reason = download_with_retries(
                meta.direct_url, temp_work_dir / "track_audio", isolated_cookie, "spotitracks",
                meta_to_enrich=meta, watchdog=watchdog, allow_any_duration=True,
            )
            if staged_mp3:
                finalize_staged_mp3(staged_mp3, meta, meta.direct_url, "spotitracks", 100, meta.title, cache_data, quarantine, watchdog)
                return True, ""

            if bad_key := canonical_media_url_key(meta.direct_url):
                failed_url_keys.add(bad_key)

            can_fallback_to_search = (
                DIRECT_URL_FALLBACK
                and meta.artist not in ("", "Unknown", "Custom Artist", "External Artist")
                and not meta.title.startswith(("Custom Track #", "External Track"))
            )
            if not can_fallback_to_search:
                return False, f"Ошибка скачивания прямой ссылки: {err_reason or 'Неизвестно'}"

            logger.warning(
                f"[DIRECT URL FALLBACK] Прямая ссылка {meta.direct_url} недоступна ({err_reason or 'сбой'}), "
                f"но DIRECT_URL_FALLBACK=true -> переходим к каскадному поиску (ISRC / Topic / YouTube / SoundCloud)..."
            )

        if (
            existing_disk_url
            and not IGNORE_CACHED_URLS
            and not is_same_media_url(existing_disk_url, meta.direct_url)
            and canonical_media_url_key(existing_disk_url) not in failed_url_keys
        ):
            if has_valid_existing_mp3:
                skip_if_already_downloaded_from_same_url(
                    existing_disk_url, existing_mp3_path, existing_disk_url, meta,
                    existing_disk_src_type, float(cached_entry.get("score", 100)), cache_data, quarantine,
                )
                return True, ""

            logger.info(f"[CACHE URL] Найдена прямая ссылка в кэше ({existing_disk_url}), качаем без поиска...")
            staged_mp3, _ = download_with_retries(
                existing_disk_url, temp_work_dir / "track_audio", isolated_cookie, existing_disk_src_type, meta_to_enrich=meta, watchdog=watchdog
            )
            if staged_mp3:
                finalize_staged_mp3(
                    staged_mp3, meta, existing_disk_url, existing_disk_src_type, cached_entry.get("score", 100),
                    meta.title, cache_data, quarantine, watchdog,
                )
                return True, ""
            if bad_key := canonical_media_url_key(existing_disk_url):
                failed_url_keys.add(bad_key)
            logger.warning("Ссылка из кэша недоступна, переходим к каскадному поиску...")

        if meta.isrc:
            logger.info(f"[УРОВЕНЬ 1 | ISRC] Поиск по студийному коду ISRC: {meta.isrc}")
            for attempt in range(YTDLP_RETRIES):
                watchdog.reset_for_search_stage(f"search:ISRC:try#{attempt + 1}")
                try:
                    s_opts = build_ydl_opts(attempt, isolated_cookie, for_search=True)
                    s_opts.update({"extract_flat": False, "ignoreerrors": True})
                    with yt_dlp.YoutubeDL(s_opts) as ydl:
                        if attempt > 0:
                            ydl.cache.remove()
                        info = ydl.extract_info(f'ytsearch5:"{meta.isrc}"', download=False)
                        entries = [e for e in (info.get("entries", []) if info else []) if e]
                        for entry in entries:
                            w_url = entry.get("webpage_url")
                            if not w_url or canonical_media_url_key(w_url) in failed_url_keys:
                                continue
                            ok_match, match_details = is_perfect_match(entry, meta)
                            if ok_match:
                                logger.info(f"★ Идеальное совпадение по ISRC ({match_details}): '{entry.get('title')}' ({w_url})")
                                if skip_if_already_downloaded_from_same_url(
                                    w_url, existing_mp3_path, existing_disk_url, meta, "ytmusic", 100, cache_data, quarantine
                                ):
                                    return True, ""
                                staged_mp3, _ = download_with_retries(
                                    w_url, temp_work_dir / "track_audio", isolated_cookie, "ytmusic", meta_to_enrich=meta, watchdog=watchdog
                                )
                                if staged_mp3:
                                    finalize_staged_mp3(
                                        staged_mp3, meta, w_url, "ytmusic", 100,
                                        entry.get("title", meta.title), cache_data, quarantine, watchdog,
                                    )
                                    return True, ""
                                if bad_key := canonical_media_url_key(w_url):
                                    failed_url_keys.add(bad_key)
                        if entries:
                            break
                except TimeoutError:
                    raise
                except Exception as e:
                    logger.debug(f"Ошибка ISRC поиска: {e}")

        logger.info(f"[УРОВЕНЬ 2 | TOPIC / YTM] Поиск студийного релиза: {meta.artist} - {meta.clean_title}")
        topic_entries: list[dict[str, Any]] = []
        for attempt in range(YTDLP_RETRIES):
            watchdog.reset_for_search_stage(f"search:TOPIC:try#{attempt + 1}")
            try:
                s_opts = build_ydl_opts(attempt, isolated_cookie, for_search=True)
                s_opts.update({"extract_flat": False, "ignoreerrors": True})
                with yt_dlp.YoutubeDL(s_opts) as ydl:
                    if attempt > 0:
                        ydl.cache.remove()
                    info = ydl.extract_info(f"ytsearch5:{meta.artist} - {meta.clean_title} topic", download=False)
                    topic_entries = [e for e in (info.get("entries", []) if info else []) if e]
                if topic_entries:
                    break
            except TimeoutError:
                raise
            except Exception as e:
                logger.debug(f"Ошибка Topic поиска: {e}")

        for entry in topic_entries:
            w_url = entry.get("webpage_url")
            if not w_url or canonical_media_url_key(w_url) in failed_url_keys:
                continue
            ok_match, match_details = is_perfect_match(entry, meta)
            if ok_match:
                logger.info(
                    f"★ Ранняя остановка! Идеальное совпадение ({match_details}): "
                    f"'{entry.get('title')}' (Канал: {entry.get('uploader') or entry.get('channel')} | {w_url})"
                )
                if skip_if_already_downloaded_from_same_url(
                    w_url, existing_mp3_path, existing_disk_url, meta, "ytmusic", 100, cache_data, quarantine
                ):
                    return True, ""
                staged_mp3, _ = download_with_retries(
                    w_url, temp_work_dir / "track_audio", isolated_cookie, "ytmusic", meta_to_enrich=meta, watchdog=watchdog
                )
                if staged_mp3:
                    finalize_staged_mp3(
                        staged_mp3, meta, w_url, "ytmusic", 100,
                        entry.get("title", meta.title), cache_data, quarantine, watchdog,
                    )
                    return True, ""
                if bad_key := canonical_media_url_key(w_url):
                    failed_url_keys.add(bad_key)

        logger.info("[УРОВЕНЬ 3 | КАСКАД] Идеальных Topic-совпадений не найдено, запускаем балльный каскад (YouTube / SoundCloud)...")
        all_artists_str = ", ".join(meta.artists_all[:2])
        search_waterfall: list[tuple[str, str, str]] = [
            ("youtube", f"ytsearch5:{all_artists_str} - {meta.clean_title} Official Audio", "Поиск Official Audio на YouTube"),
            ("soundcloud", f"scsearch5:{meta.artist} - {meta.clean_title}", "Поиск на SoundCloud"),
            ("youtube", f"ytsearch5:{meta.artist} {meta.clean_title}", "Широкий поиск на YouTube"),
            *(
                [("youtube", f"ytsearch5:{meta.artist} {meta.album}", "Поиск по названию альбома (CJK Fallback)")]
                if has_cjk_chars(meta.title) and meta.album.lower() not in ("single", "spotify playlist")
                else []
            ),
        ]

        total_steps = len(search_waterfall)
        last_download_error = "Не найден ни на одной площадке (YouTube, SoundCloud)"

        for step_idx, (source_type, query, step_desc) in enumerate(search_waterfall, 1):
            watchdog.reset_for_search_stage(f"search:cascade_{step_idx}_{source_type}")
            if source_type == "soundcloud" and is_soundcloud_blocked():
                logger.info(f"[КАСКАД {step_idx}/{total_steps} | SOUNDCLOUD] Пропуск (IP VPN заблокирован 403 в SoundCloud)")
                continue

            logger.info(f"[КАСКАД {step_idx}/{total_steps} | {source_type.upper()}] {step_desc} -> '{query}'")
            entries: list[dict[str, Any]] = []
            for search_attempt in range(YTDLP_RETRIES):
                watchdog.reset_for_search_stage(f"search:cascade_{step_idx}_{source_type}:try#{search_attempt + 1}")
                if source_type == "soundcloud" and is_soundcloud_blocked():
                    break
                try:
                    s_opts = build_ydl_opts(search_attempt, isolated_cookie, for_search=True)
                    s_opts.update({"extract_flat": False, "ignoreerrors": True})
                    with yt_dlp.YoutubeDL(s_opts) as ydl:
                        if search_attempt > 0:
                            ydl.cache.remove()
                        info = ydl.extract_info(query, download=False)
                        entries = [e for e in (info.get("entries", []) if info else []) if e]
                    if entries:
                        if source_type == "soundcloud":
                            reset_soundcloud_403()
                        break
                except TimeoutError:
                    raise
                except Exception as e:
                    logger.debug(f"Ошибка поискового запроса '{query}': {e}")

            if not entries and not (step_idx == 1 and topic_entries):
                continue

            combined_entries = entries + (topic_entries if step_idx == 1 and source_type == "youtube" else [])
            seen_urls: set[str] = set()
            scored: list[tuple[float, str, str, str]] = []

            for entry in combined_entries:
                w_url = entry.get("webpage_url")
                w_key = canonical_media_url_key(w_url)
                if not w_url or not w_key or w_key in seen_urls:
                    continue
                seen_urls.add(w_key)
                if w_key in failed_url_keys:
                    if show_scoring:
                        score_log_fn(f"  ✗ Пропуск: '{entry.get('title') or 'Unknown'}' [{w_url}] -> этот URL уже проверялся и недоступен")
                    continue

                s, reason = score_candidate(entry, meta, source_type)
                c_title = entry.get("title") or "Unknown"
                c_up = entry.get("uploader") or entry.get("channel") or "Unknown"
                c_dur_str = format_duration(int(entry.get("duration") or 0))
                if s > 0:
                    scored.append((s, w_url, c_title, c_up))
                    if show_scoring:
                        score_log_fn(f"  ✓ Кандидат: '{c_title}' [{c_up} | {c_dur_str} | {w_url}] -> {s:.0f} pts ({reason})")
                elif show_scoring:
                    score_log_fn(f"  ✗ Отклонен: '{c_title}' [{c_up} | {c_dur_str}] -> {reason}")

            if not scored:
                continue

            scored.sort(key=lambda x: x[0], reverse=True)
            for cand_rank, (score, candidate_url, c_title, c_up) in enumerate(scored[:2], 1):
                logger.info(f"★ Выбран лучший источник #{cand_rank} [{source_type.upper()} | {int(score)} pts]: '{c_title}' (Канал: {c_up} | {candidate_url})")
                if skip_if_already_downloaded_from_same_url(
                    candidate_url, existing_mp3_path, existing_disk_url, meta, source_type, score, cache_data, quarantine
                ):
                    return True, ""
                staged_mp3, err_reason = download_with_retries(
                    candidate_url, temp_work_dir / "track_audio", isolated_cookie, source_type, meta_to_enrich=meta, watchdog=watchdog
                )
                if staged_mp3:
                    finalize_staged_mp3(staged_mp3, meta, candidate_url, source_type, score, c_title, cache_data, quarantine, watchdog)
                    return True, ""
                if bad_key := canonical_media_url_key(candidate_url):
                    failed_url_keys.add(bad_key)
                last_download_error = f"Ошибка скачивания кандидата ({source_type}): {err_reason or 'Неизвестно'}"

        if not ENABLE_FALLBACK_SEARCH:
            logger.info(
                f"[УРОВЕНЬ 4 | ФОЛЛБЭК ОТКЛЮЧЕН] ENABLE_FALLBACK_SEARCH=false — "
                f"пропускаем нестрогий поиск для '{meta.display_name}'."
            )
            logger.error(f"✖ Не удалось найти/скачать ни на одной площадке: {meta.display_name} ({meta.id_filename})")
            return False, last_download_error

        full_unmodified_query = f"{meta.artist} - {meta.title}"
        logger.warning(
            f"[УРОВЕНЬ 4 | ФОЛЛБЭК ПОСЛЕДНЕГО ШАНСА] Официальных совпадений не найдено! "
            f"Ищем полное название без вырезаний и берем первое видео с YouTube -> '{full_unmodified_query}'"
        )
        fallback_entries: list[dict[str, Any]] = []
        for fb_attempt in range(YTDLP_RETRIES):
            watchdog.reset_for_search_stage(f"search:FALLBACK_FIRST_YT:try#{fb_attempt + 1}")
            try:
                s_opts = build_ydl_opts(fb_attempt, isolated_cookie, for_search=True)
                s_opts.update({"extract_flat": False, "ignoreerrors": True})
                with yt_dlp.YoutubeDL(s_opts) as ydl:
                    if fb_attempt > 0:
                        ydl.cache.remove()
                    info = ydl.extract_info(f"ytsearch3:{full_unmodified_query}", download=False)
                    fallback_entries = [e for e in (info.get("entries", []) if info else []) if e and not e.get("drm")]
                if fallback_entries:
                    break
            except TimeoutError:
                raise
            except Exception as e:
                logger.debug(f"Ошибка фоллбэк-поиска '{full_unmodified_query}': {e}")

        for fb_entry in fallback_entries:
            fb_url = fb_entry.get("webpage_url")
            fb_dur = int(fb_entry.get("duration") or 0)
            fb_key = canonical_media_url_key(fb_url)
            if not fb_url or not fb_key or fb_key in failed_url_keys or (0 < fb_dur < 15) or fb_dur > 1500:
                continue

            fb_title = fb_entry.get("title") or meta.title
            fb_up = fb_entry.get("uploader") or fb_entry.get("channel") or "Unknown"
            logger.warning(
                f"★ [ФОЛЛБЭК YT #1] Берем первое подходящее видео с YouTube: "
                f"'{fb_title}' [Канал: {fb_up} | Длина: {format_duration(fb_dur)} | {fb_url}]"
            )
            if skip_if_already_downloaded_from_same_url(
                fb_url, existing_mp3_path, existing_disk_url, meta, "youtube_fallback", 10, cache_data, quarantine
            ):
                return True, ""
            staged_mp3, err_reason = download_with_retries(
                fb_url, temp_work_dir / "track_audio", isolated_cookie, "youtube_fallback",
                meta_to_enrich=meta, watchdog=watchdog, allow_any_duration=True,
            )
            if staged_mp3:
                finalize_staged_mp3(staged_mp3, meta, fb_url, "youtube_fallback", 10, fb_title, cache_data, quarantine, watchdog)
                return True, ""
            failed_url_keys.add(fb_key)
            last_download_error = f"Ошибка скачивания первого видео фоллбэка: {err_reason or 'Неизвестно'}"

        logger.error(f"✖ Не удалось найти/скачать ни на одной площадке: {meta.display_name} ({meta.id_filename})")
        return False, last_download_error
    except TimeoutError as e:
        if not is_shutting_down():
            logger.warning(f"⏱ Остановка воркера по адаптивному таймеру для {meta.display_name}: {e}")
        return False, str(e)
    except Exception as e:
        if not is_shutting_down():
            logger.exception(f"Критическая ошибка воркера для {meta.display_name}: {e}")
        return False, f"Исключение воркера: {e}"
    finally:
        shutil.rmtree(temp_work_dir, ignore_errors=True)
        current_ctx.reset(token_ctx)


async def resolve_azuracast_station_id(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    raw_station = AZURACAST_STATION_ID.strip()
    try:
        r_st = await client.get(f"{AZURACAST_URL}/api/stations", headers=headers)
        if r_st.status_code == 200 and isinstance(stations := r_st.json(), list):
            if not raw_station and len(stations) == 1:
                st = stations[0]
                return str(st.get("id") or st.get("short_name") or "")
            raw_low = raw_station.lower()
            for st in stations:
                st_id = str(st.get("id") or "")
                st_short = str(st.get("short_name") or "").strip()
                st_name = str(st.get("name") or "").strip()
                if raw_station == st_id or raw_low in (st_short.lower(), st_name.lower()):
                    return st_id or raw_station
    except Exception as e:
        logger.debug(f"Не удалось получить список станций /api/stations: {e}")
    return raw_station


async def check_azuracast_connectivity() -> None:
    token_ctx = current_ctx.set("AZURACAST-DIAG")
    try:
        print("\n" + "=" * 88)
        print(f" ДИАГНОСТИКА ПОДКЛЮЧЕНИЯ К AZURACAST API | {BUILD_VERSION}")
        print("=" * 88)
        print(f" • AZURACAST_URL           : {AZURACAST_URL or 'НЕ ЗАДАН'}")
        print(f" • AZURACAST_STATION_ID    : {AZURACAST_STATION_ID or 'НЕ ЗАДАН (будет показан список всех станций)'}")
        print(f" • AZURACAST_PLAYLIST_ID   : {AZURACAST_PLAYLIST_ID or 'НЕ ЗАДАН'}")
        print(f" • AZURACAST_PLAYLIST_NAME : {AZURACAST_PLAYLIST_NAME or 'НЕ ЗАДАН'}")
        print(f" • API KEY задан           : {yn(bool(AZURACAST_API_KEY))} (длина: {len(AZURACAST_API_KEY)} симв.)")
        print(f" • Статус конфигурации     : {yn(is_azuracast_configured())}")
        print("-" * 88)

        if not AZURACAST_URL:
            print(" [ОШИБКА] Переменная AZURACAST_URL пуста!")
            print("=" * 88 + "\n")
            return

        headers = {"X-API-Key": AZURACAST_API_KEY, "Accept": "application/json", "User-Agent": "SpotiSync/7.3"}
        async with httpx.AsyncClient(timeout=15.0, verify=False, follow_redirects=True) as client:
            t0 = time.monotonic()
            try:
                r_status = await client.get(f"{AZURACAST_URL}/api/status", headers=headers)
                dt = (time.monotonic() - t0) * 1000
                print(f" 1. Пинг {AZURACAST_URL}/api/status -> HTTP {r_status.status_code} ({dt:.0f} мс)")
                if r_status.history:
                    redir_chain = " -> ".join(f"{r.status_code} ({r.headers.get('location')})" for r in r_status.history)
                    print(f"    [РЕДИРЕКТ] Запрос был перенаправлен: {redir_chain} -> {r_status.url}")
                print(f"    Ответ сервера: {r_status.text[:250].strip()}")
            except Exception as e:
                print(f" 1. [СБОЙ СЕТИ] Не удалось достучаться до {AZURACAST_URL}/api/status: {type(e).__name__}: {e}")
                print("    Проверьте: находится ли контейнер в одной Docker-сети с AzuraCast или не блокирует ли трафик VPN/файрвол!")
                print("=" * 88 + "\n")
                return

            resolved_station_id = AZURACAST_STATION_ID
            try:
                r_stations = await client.get(f"{AZURACAST_URL}/api/stations", headers=headers)
                print(f" 2. Список радиостанций ({AZURACAST_URL}/api/stations) -> HTTP {r_stations.status_code}")
                if r_stations.status_code == 200 and isinstance(st_list := r_stations.json(), list):
                    print(f"    Найдено радиостанций: {len(st_list)} шт.")
                    for st in st_list:
                        st_id = st.get("id")
                        st_short = st.get("short_name")
                        st_name = st.get("name")
                        print(f"      • ID: {st_id} | short_name (служебное): '{st_short}' | Название: '{st_name}'")
                    resolved_station_id = await resolve_azuracast_station_id(client, headers)
                    if not resolved_station_id and len(st_list) == 1:
                        resolved_station_id = str(st_list[0].get("id") or "")
            except Exception as e:
                print(f" 2. [ОШИБКА] Сбой при запросе списка станций: {type(e).__name__}: {e}")

            if resolved_station_id and AZURACAST_API_KEY:
                try:
                    r_pls = await client.get(
                        f"{AZURACAST_URL}/api/station/{resolved_station_id}/playlists",
                        headers=headers,
                    )
                    print(
                        f" 3. Запрос плейлистов станции '{AZURACAST_STATION_ID or resolved_station_id}' "
                        f"(Resolved ID: {resolved_station_id}) -> HTTP {r_pls.status_code}"
                    )
                    if r_pls.status_code == 200 and isinstance(pls := r_pls.json(), list):
                        print(f"    Найдено плейлистов: {len(pls)} шт.")
                        for p in pls:
                            print(f"      • ID: {p.get('id')} | Имя: '{p.get('name')}' | Тип: {p.get('type')}")
                    else:
                        print(f"    Ответ: {r_pls.text[:300].strip()}")
                except Exception as e:
                    print(f" 3. [ОШИБКА] Сбой при запросе плейлистов: {type(e).__name__}: {e}")
        print("=" * 88 + "\n")
    finally:
        current_ctx.reset(token_ctx)


async def resolve_azuracast_playlist_id(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    station_id: str,
) -> tuple[int | None, str]:
    target_pl_id: int | None = int(AZURACAST_PLAYLIST_ID) if AZURACAST_PLAYLIST_ID.isdigit() else None
    target_pl_name = AZURACAST_PLAYLIST_NAME

    url = f"{AZURACAST_URL}/api/station/{station_id}/playlists"
    r_pls = await client.get(url, headers=headers)
    if r_pls.status_code == 200 and isinstance(pls_data := r_pls.json(), list):
        for pl in pls_data:
            if target_pl_id and int(pl.get("id", 0)) == target_pl_id:
                target_pl_name = pl.get("name", str(target_pl_id))
                break
            if target_pl_name and str(pl.get("name", "")).strip().lower() == target_pl_name.lower():
                target_pl_id = int(pl["id"])
                break
    elif r_pls.status_code != 200:
        logger.warning(f"AzuraCast вернул HTTP {r_pls.status_code} при запросе {url}: {r_pls.text[:200]}")
    return target_pl_id, (target_pl_name or str(target_pl_id or ""))


async def clear_azuracast_queue(client: httpx.AsyncClient, headers: dict[str, str], station_id: str) -> None:
    try:
        r_clear = await client.post(
            f"{AZURACAST_URL}/api/station/{station_id}/queue/clear",
            headers=headers,
        )
        if r_clear.status_code in (200, 204):
            logger.info(f"[AZURACAST] Очередь AutoDJ станции #{station_id} сброшена (ротация обновлена).")
        else:
            logger.debug(f"[AZURACAST] Статус сброса очереди: HTTP {r_clear.status_code}")
    except Exception as e:
        logger.debug(f"[AZURACAST] Не удалось сбросить очередь станции #{station_id}: {e}")


async def unassign_tracks_from_azuracast_playlist(filenames_to_remove: list[str]) -> None:
    if not filenames_to_remove or not is_azuracast_configured() or is_shutting_down():
        return
    token_ctx = current_ctx.set("AZURACAST-DEL")
    try:
        headers = {"X-API-Key": AZURACAST_API_KEY, "Accept": "application/json", "User-Agent": "SpotiSync/7.3"}
        async with httpx.AsyncClient(timeout=45.0, verify=False, follow_redirects=True) as client:
            station_id = await resolve_azuracast_station_id(client, headers)
            target_pl_id, target_pl_name = await resolve_azuracast_playlist_id(client, headers, station_id)
            if not target_pl_id:
                return
            r_files = await client.get(f"{AZURACAST_URL}/api/station/{station_id}/files", headers=headers)
            if r_files.status_code != 200 or not isinstance(station_files := r_files.json(), list):
                return

            remove_set = set(filenames_to_remove)
            unlink_groups: dict[tuple[int, ...], list[str]] = {}
            for f_obj in station_files:
                rel_path = str(f_obj.get("path") or "")
                if AZURACAST_MEDIA_SUBDIR and not rel_path.startswith(f"{AZURACAST_MEDIA_SUBDIR}/"):
                    continue
                if Path(rel_path).name not in remove_set:
                    continue
                current_pl_ids = {
                    int(p["id"]) if isinstance(p, dict) and p.get("id") is not None else int(p)
                    for p in (f_obj.get("playlists") or [])
                    if (isinstance(p, dict) and p.get("id") is not None) or isinstance(p, int)
                }
                if target_pl_id in current_pl_ids:
                    unlink_groups.setdefault(tuple(sorted(current_pl_ids - {target_pl_id})), []).append(rel_path)

            total_unlinked = 0
            for rem_pl_tuple, file_paths in unlink_groups.items():
                for i in range(0, len(file_paths), 100):
                    chunk = file_paths[i : i + 100]
                    r_batch = await client.put(
                        f"{AZURACAST_URL}/api/station/{station_id}/files/batch",
                        headers=headers,
                        json={"do": "playlist", "playlists": list(rem_pl_tuple), "files": chunk},
                    )
                    if r_batch.status_code in (200, 204):
                        total_unlinked += len(chunk)
            if total_unlinked > 0:
                logger.info(f"Убрано {total_unlinked} треков из плейлиста AzuraCast '{target_pl_name}' (ID: {target_pl_id}).")
                await clear_azuracast_queue(client, headers, station_id)
    except Exception as e:
        logger.warning(f"Ошибка при удалении треков из плейлиста AzuraCast: {e}")
    finally:
        current_ctx.reset(token_ctx)


async def sync_with_azuracast(expected_filenames: list[str], new_downloads_count: int) -> None:
    if not expected_filenames or not is_azuracast_configured() or is_shutting_down():
        return
    token_ctx = current_ctx.set("AZURACAST")
    try:
        headers = {"X-API-Key": AZURACAST_API_KEY, "Accept": "application/json", "User-Agent": "SpotiSync/7.3"}
        async with httpx.AsyncClient(timeout=60.0, verify=False, follow_redirects=True) as client:
            station_id = await resolve_azuracast_station_id(client, headers)
            target_pl_id, target_pl_name = await resolve_azuracast_playlist_id(client, headers, station_id)
            if not target_pl_id:
                logger.warning("Не удалось найти целевой плейлист в AzuraCast!")
                return

            logger.info(
                f"Синхронизация со станцией AzuraCast #{station_id}, плейлист '{target_pl_name}' (ID: {target_pl_id})..."
            )

            expected_set = set(expected_filenames)
            max_poll_attempts = 12 if new_downloads_count > 0 else 3
            station_files: list[dict[str, Any]] = []

            for poll_idx in range(1, max_poll_attempts + 1):
                if is_shutting_down():
                    return
                if poll_idx == 1 and new_downloads_count > 0:
                    try:
                        await client.get(
                            f"{AZURACAST_URL}/api/station/{station_id}/files/list",
                            headers=headers,
                            params={"internal": "true", "flushCache": "true"},
                        )
                        await client.put(
                            f"{AZURACAST_URL}/api/station/{station_id}/files/batch",
                            headers=headers,
                            json={
                                "do": "reprocess",
                                "files": [],
                                "directories": [AZURACAST_MEDIA_SUBDIR] if AZURACAST_MEDIA_SUBDIR else [""],
                            },
                        )
                    except Exception:
                        pass
                    await sleep_interruptible(3.0)

                r_files = await client.get(f"{AZURACAST_URL}/api/station/{station_id}/files", headers=headers)
                if r_files.status_code != 200 or not isinstance(f_list := r_files.json(), list):
                    logger.warning(f"Ошибка получения списка файлов AzuraCast: HTTP {r_files.status_code} ({r_files.text[:200]})")
                    return
                station_files = f_list

                indexed_names: set[str] = set()
                for f_obj in station_files:
                    rel_path = str(f_obj.get("path") or "")
                    if AZURACAST_MEDIA_SUBDIR and not rel_path.startswith(f"{AZURACAST_MEDIA_SUBDIR}/"):
                        continue
                    fname = Path(rel_path).name
                    if fname in expected_set:
                        indexed_names.add(fname)

                missing_in_azura = expected_set - indexed_names
                if not missing_in_azura or poll_idx == max_poll_attempts:
                    if missing_in_azura:
                        logger.warning(
                            f"[AZURACAST] {len(missing_in_azura)} файлов с диска еще не проиндексированы в БД AzuraCast "
                            f"(например: {', '.join(sorted(missing_in_azura)[:3])}). Они будут добавлены в следующем цикле."
                        )
                    break

                logger.info(
                    f"[AZURACAST WAIT {poll_idx}/{max_poll_attempts}] Ожидание индексации новых файлов в БД AzuraCast "
                    f"(осталось проиндексировать: {len(missing_in_azura)} шт.)... Пауза 10сек"
                )
                try:
                    await client.get(
                        f"{AZURACAST_URL}/api/station/{station_id}/files/list",
                        headers=headers,
                        params={"internal": "true", "flushCache": "true"},
                    )
                except Exception:
                    pass
                await sleep_interruptible(10.0)

            playlist_groups: dict[tuple[int, ...], list[str]] = {}
            already_assigned = 0

            for f_obj in station_files:
                rel_path = str(f_obj.get("path") or "")
                if AZURACAST_MEDIA_SUBDIR and not rel_path.startswith(f"{AZURACAST_MEDIA_SUBDIR}/"):
                    continue
                if Path(rel_path).name not in expected_set:
                    continue
                current_pl_ids = {
                    int(p["id"]) if isinstance(p, dict) and p.get("id") is not None else int(p)
                    for p in (f_obj.get("playlists") or [])
                    if (isinstance(p, dict) and p.get("id") is not None) or isinstance(p, int)
                }
                if target_pl_id in current_pl_ids:
                    already_assigned += 1
                else:
                    playlist_groups.setdefault(tuple(sorted(current_pl_ids | {target_pl_id})), []).append(rel_path)

            if not playlist_groups:
                logger.success(
                    f"Все проиндексированные треки ({already_assigned}/{len(expected_set)} шт.) "
                    f"уже состоят в плейлисте AzuraCast #{target_pl_id}!"
                )
                return

            total_added = 0
            for pl_tuple, file_paths in playlist_groups.items():
                for i in range(0, len(file_paths), 100):
                    chunk = file_paths[i : i + 100]
                    r_batch = await client.put(
                        f"{AZURACAST_URL}/api/station/{station_id}/files/batch",
                        headers=headers,
                        json={"do": "playlist", "playlists": list(pl_tuple), "files": chunk},
                    )
                    if r_batch.status_code in (200, 204):
                        total_added += len(chunk)
            if total_added > 0:
                logger.success(
                    f"В плейлист AzuraCast #{target_pl_id} добавлено {total_added} новых треков (всего: {already_assigned + total_added})!"
                )
                await clear_azuracast_queue(client, headers, station_id)
    except Exception as e:
        logger.warning(f"Ошибка синхронизации с AzuraCast API: {e}")
    finally:
        current_ctx.reset(token_ctx)


def tag_mp3_file(file_path: Path, meta: TrackMeta) -> None:
    try:
        audio = EasyID3(file_path)
    except ID3NoHeaderError:
        ID3().save(file_path, v2_version=3)
        audio = EasyID3(file_path)

    audio["title"] = meta.title
    audio["artist"] = ", ".join(meta.artists_all)
    audio["album"] = meta.album
    audio["albumartist"] = meta.album_artist
    if meta.release_date:
        audio["date"] = meta.release_date
    if meta.track_number:
        audio["tracknumber"] = meta.track_number
    if meta.disc_number:
        audio["discnumber"] = meta.disc_number
    audio.save(file_path, v2_version=3)

    raw_id3 = ID3(file_path)
    raw_id3.delall("TXXX:SPOTIFY_TRACK_ID")
    raw_id3.add(TXXX(encoding=3, desc="SPOTIFY_TRACK_ID", text=meta.spotify_id))
    if meta.isrc:
        raw_id3.delall("TSRC")
        raw_id3.add(TSRC(encoding=3, text=meta.isrc))

    cover_to_fetch = meta.cover_url or DEFAULT_PLAYLIST_COVER_URL
    if cover_to_fetch and (cov_res := fetch_cover_cached(cover_to_fetch)):
        cov_bytes, mime = cov_res
        raw_id3.delall("APIC")
        raw_id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cov_bytes))

    raw_id3.save(file_path, v2_version=3)


def print_cli_help() -> None:
    sep_main = "=" * 100
    sep_sub = "-" * 100
    az_key_status = f"Задан ({len(AZURACAST_API_KEY)} симв.)" if AZURACAST_API_KEY else "НЕ ЗАДАН"

    print(
        f"\n{sep_main}\n"
        f" ПОЛНЫЙ СПРАВОЧНИК КОМАНД, ФЛАГОВ И ПЕРЕМЕННЫХ SPOTISYNC\n"
        f" Версия сборки: {BUILD_VERSION}\n"
        f"{sep_main}\n\n"
        f" [1] СИНТАКСИС ЗАПУСКА В DOCKER\n"
        f"{sep_sub}\n"
        f"   • Внутри запущенного контейнера (рекомендуется для быстрых команд):\n"
        f"     docker exec -it azuracast_spotisync python /app/sync_spotify.py <флаги>\n\n"
        f"   • Разовый запуск нового контейнера через Docker Compose:\n"
        f"     docker compose run --rm -it spotisync -- <флаги>\n\n"
        f"   • Передача любой переменной окружения на лету (два способа):\n"
        f"     --env KEY=VALUE          (например: --env CONCURRENT_DOWNLOADS=8)\n"
        f"     --флаг=ЗНАЧЕНИЕ          (например: --workers=8 или --workers 8)\n\n"
        f" [2] СЕРВИСНЫЕ И ДИАГНОСТИЧЕСКИЕ КОМАНДЫ (выполняются и завершают работу)\n"
        f"{sep_sub}\n"
        f"   --help, -h                 Показать этот подробный справочник команд и текущих настроек.\n"
        f"   --status, --stats          Полная сводка состояния: кэш (.spotisync.json), файлы на диске,\n"
        f"                              кастомные треки (.spotitracks.json), место на диске, статус куки\n"
        f"                              YouTube и подробный список треков в карантине.\n"
        f"   --quarantine-list,         Показать только список треков в карантине (причина сбоя, число\n"
        f"   --quarantine               попыток, статус файла на диске и остаток времени до разблокировки).\n"
        f"   --custom-tracks,           Отчет по файлу {CUSTOM_TRACKS_PATH.name}: черный список (ignores),\n"
        f"   --spotitracks              переопределения ссылок (overrides) и кастомные треки (custom_tracks).\n"
        f"   --auth                     Интерактивный мастер настройки и проверки куки YouTube (18+ Age-Gate).\n"
        f"                              Принимает 'Copy as cURL (bash)', заголовок 'Cookie:' или Netscape-файл.\n"
        f"   --retag                    Принудительно перезаписать ID3v2.3 теги, ISRC и обложки у всех уже\n"
        f"                              скачанных MP3-файлов на диске по данным из локального кэша.\n"
        f"                              (Добавьте --sync или --loop, чтобы после ретега сразу запустить цикл).\n"
        f"   --azura-check              Полная диагностика подключения к AzuraCast API: пинг /api/status,\n"
        f"                              проверка редиректов, вывод всех станций (ID и short_name) и плейлистов.\n"
        f"   --azura-only               Мгновенно синхронизировать все имеющиеся на диске MP3-файлы с целевым\n"
        f"                              плейлистом AzuraCast без парсинга Spotify и без скачивания треков.\n\n"
        f" [3] ТОЧЕЧНОЕ СКАЧИВАНИЕ / ПЕРЕКАЧКА ОТДЕЛЬНЫХ ТРЕКОВ (--track)\n"
        f"{sep_sub}\n"
        f"   --track <запрос>,          Найти и принудительно (пере)скачать конкретный трек вне очереди.\n"
        f"   -t <запрос>,               Флаг можно указывать несколько раз в одной команде!\n"
        f"   --track=<запрос>           Поддерживаемые форматы <запроса>:\n"
        f"                                1) Spotify ID (22 символа) или ссылка https://open.spotify.com/track/...\n"
        f"                                2) Прямая ссылка на YouTube / YouTube Music / SoundCloud\n"
        f"                                3) Поисковая строка по имени: \"Artist - Title\" (или часть названия)\n"
        f"   --allow-external           Разрешить скачивание трека через --track, даже если его НЕТ в вашем\n"
        f"   --no-allow-external        плейлисте Spotify или кэше (создаст внешний трек или вытянет метаданные\n"
        f"                              из Spotify Embed + Deezer). [Сейчас: {yn(TRACK_ALLOW_EXTERNAL)}]\n\n"
        f" [4] УПРАВЛЕНИЕ ЦИКЛОМ СИНХРОНИЗАЦИИ И ОЧИСТКОЙ БАЗЫ\n"
        f"{sep_sub}\n"
        f"   --once, -1                 Выполнить ровно один цикл синхронизации и завершиться.\n"
        f"   --loop, --daemon           Бесконечный цикл синхронизации каждые SYNC_INTERVAL_MINUTES минут.\n"
        f"   --dry-run                  Тестовый прогон (парсинг Spotify, обогащение ISRC/Года, обновление кэша\n"
        f"                              и проверка очереди), но БЕЗ реального скачивания и удаления MP3.\n"
        f"   --ignore-quarantine,       Игнорировать карантин (failed_tracks.json) в первом проходе цикла и\n"
        f"   --no-quarantine            попытаться заново скачать все ранее проблемные треки.\n"
        f"   --redownload, --force-dl   Удалить все управляемые MP3-файлы перед стартом и перекачать их с нуля\n"
        f"                              (с сохранением обогащенных метаданных ISRC/Года в кэше).\n"
        f"   --rebuild                  Полный сброс: удалить кэш (.spotisync.json), карантин и управляемые MP3,\n"
        f"                              отвязать их из плейлиста AzuraCast и сразу запустить чистую синхронизацию.\n"
        f"   --remove-cache             Удалить кэш (.spotisync.json), карантин и все скачанные треки проекта.\n"
        f"                              (Файл кастомных треков {CUSTOM_TRACKS_PATH.name} бережно сохраняется!).\n"
        f"   --remove-cache --all       Полная зачистка папки {OUTPUT_DIR} (удаляет абсолютно все файлы и папки,\n"
        f"                              кроме {CUSTOM_TRACKS_PATH.name}). Добавьте --sync для запуска цикла после очистки.\n\n"
        f" [5] ПАРАМЕТРЫ КОНФИГУРАЦИИ (ФЛАГ CLI  <-->  ПЕРЕМЕННАЯ .ENV  [ТЕКУЩЕЕ ЗНАЧЕНИЕ])\n"
        f"{sep_sub}\n"
        f"   • Spotify и Регион:\n"
        f"     --playlist-url <URL>     PLAYLIST_URL                    [{PLAYLIST_URL or 'НЕ ЗАДАН'}]\n"
        f"     --spotify-market <CODE>  SPOTIFY_MARKET                  [{SPOTIFY_MARKET}]\n"
        f"     --interval <МИН>         SYNC_INTERVAL_MINUTES           [{format_duration(SYNC_INTERVAL_MINUTES * 60)}]\n\n"
        f"   • Пути, Файлы и Права доступа:\n"
        f"     --output-dir <PATH>      OUTPUT_DIR                      [{OUTPUT_DIR}]\n"
        f"     --staging-dir <PATH>     STAGING_DIR                     [{STAGING_DIR}]\n"
        f"     --cache-filename <NAME>  CACHE_FILENAME                  [{CACHE_FILENAME}]\n"
        f"     --spotitracks-filename   CUSTOM_TRACKS_FILENAME          [{CUSTOM_TRACKS_FILENAME}]\n"
        f"     --failed-cache-file <P>  FAILED_CACHE_FILE               [{FAILED_CACHE_FILE}]\n"
        f"     --cookie-file <PATH>     YT_COOKIE_FILE                  [{YT_COOKIE_FILE}]\n"
        f"     --pot-provider-url <URL> POT_PROVIDER_URL                [{POT_PROVIDER_URL or 'Нет'}]\n"
        f"     --puid <UID> / --pgid    PUID / PGID                     [{PUID}:{PGID}]\n\n"
        f"   • Производительность, Таймауты Watchdog и Карантин:\n"
        f"     --workers <N>            CONCURRENT_DOWNLOADS            [{CONCURRENT_DOWNLOADS} потоков]\n"
        f"     --retries <N>            YTDLP_RETRIES                   [{YTDLP_RETRIES} попытки]\n"
        f"     --socket-timeout <SEC>   YTDLP_SOCKET_TIMEOUT_SEC        [{format_duration(YTDLP_SOCKET_TIMEOUT_SEC)}]\n"
        f"     --worker-timeout <SEC>   WORKER_TIMEOUT_SEC              [{format_duration(WORKER_BASE_TIMEOUT_SEC)}]\n"
        f"     --search-timeout <SEC>   WORKER_SEARCH_STEP_TIMEOUT_SEC  [{format_duration(WORKER_SEARCH_TIMEOUT_SEC)}]\n"
        f"     --stall-timeout <SEC>    WORKER_STALL_TIMEOUT_SEC        [{format_duration(WORKER_STALL_TIMEOUT_SEC)} без данных]\n"
        f"     --max-hard-timeout <SEC> WORKER_MAX_HARD_TIMEOUT_SEC     [{format_duration(WORKER_STAGE_MAX_TIMEOUT_SEC)}]\n"
        f"     --min-speed-kbps <KBPS>  MIN_ACCEPTABLE_SPEED_KBPS       [{MIN_ACCEPTABLE_SPEED_KBPS:g} KB/s]\n"
        f"     --fail-ttl <HOURS>       FAIL_TTL_HOURS                  [{format_duration(FAIL_TTL_HOURS * 3600)} макс. карантин]\n"
        f"     --meta-fail-ttl <HOURS>  META_FAIL_TTL_HOURS             [{format_duration((max(24.0, META_FAIL_TTL_HOURS) if META_FAIL_TTL_HOURS > 0 else 0.0) * 3600)} мин. мета-карантин]\n"
        f"     --failed-meta-file <P>   FAILED_META_CACHE_FILE          [{FAILED_META_CACHE_FILE}]\n\n"
        f"   • Интеграция с AzuraCast API (Активна: {yn(is_azuracast_configured())}):\n"
        f"     --azuracast-url <URL>           AZURACAST_URL            [{AZURACAST_URL or 'Нет'}]\n"
        f"     --azuracast-api-key <KEY>       AZURACAST_API_KEY        [{az_key_status}]\n"
        f"     --azuracast-station-id <ID/STR> AZURACAST_STATION_ID     [{AZURACAST_STATION_ID or 'Нет'}]\n"
        f"                                     (принимает числовой ID, short_name типа 'spotify_music' или имя)\n"
        f"     --azuracast-playlist-id <ID>    AZURACAST_PLAYLIST_ID    [{AZURACAST_PLAYLIST_ID or 'Нет'}]\n"
        f"     --azuracast-playlist-name <STR> AZURACAST_PLAYLIST_NAME  [{AZURACAST_PLAYLIST_NAME or 'Нет'}]\n"
        f"     --azuracast-media-subdir <DIR>  AZURACAST_MEDIA_SUBDIR   [{AZURACAST_MEDIA_SUBDIR or 'Корень /'}]\n\n"
        f"   • Логирование и Формат времени:\n"
        f"     --log-level <LEVEL>      LOG_LEVEL (TRACE|DEBUG|INFO|WARN|ERROR) [{LOG_LEVEL_STR}]\n"
        f"     --log-file <PATH>        LOG_FILE                        [{LOG_FILE or 'Отключен'}]\n"
        f"     --time-format <FMT>      TIME_FORMAT                     [{TIME_FORMAT}]\n\n"
        f" [6] БУЛЕВЫ ПЕРЕКЛЮЧАТЕЛИ (--флаг включает true | --no-флаг выключает false)\n"
        f"{sep_sub}\n"
        f"   --[no-]filter-unavailable  FILTER_UNAVAILABLE_SPOTIFY      [{yn(FILTER_UNAVAILABLE_SPOTIFY)}]\n"
        f"                              Отсеивать недоступные, пустые и локальные треки из плейлиста Spotify.\n"
        f"   --[no-]ignore-cached-urls  IGNORE_CACHED_URLS              [{yn(IGNORE_CACHED_URLS)}]\n"
        f"   (--use-cached-urls)        Если Да — при перекачке трека искать лучший источник заново.\n"
        f"   --[no-]direct-url-fallback DIRECT_URL_FALLBACK             [{yn(DIRECT_URL_FALLBACK)}]\n"
        f"                              Если прямая ссылка из .spotitracks.json недоступна (Geo-Block/Удалена),\n"
        f"                              автоматически переходить к каскадному поиску (ISRC/Topic/YT/SC).\n"
        f"   --[no-]fallback-search     ENABLE_FALLBACK_SEARCH          [{yn(ENABLE_FALLBACK_SEARCH)}]\n"
        f"                              Разрешить Уровень 4 («фоллбэк последнего шанса» — первое видео YouTube,\n"
        f"                              если строгие уровни 1–3 ничего не нашли).\n"
        f"   --[no-]sync-delete         SYNC_DELETE_REMOVED             [{yn(SYNC_DELETE_REMOVED)}]\n"
        f"                              Удалять с диска и из AzuraCast треки, удаленные из плейлиста Spotify.\n"
        f"   --[no-]sync-delete-ignored SYNC_DELETE_IGNORED             [{yn(SYNC_DELETE_IGNORED)}]\n"
        f"                              Удалять с диска и из AzuraCast ранее скачанные треки, попавшие в ignores.\n"
        f"   --[no-]normalize           AUDIO_NORMALIZE                 [{yn(AUDIO_NORMALIZE)}]\n"
        f"                              Нормализация громкости FFmpeg по стандарту радиовещания (-14 LUFS).\n"
        f"   --[no-]trim-silence        AUDIO_TRIM_SILENCE              [{yn(AUDIO_TRIM_SILENCE)}]\n"
        f"                              Мягкая обрезка цифровой пустоты (-72dB) в начале и конце трека.\n"
        f"   --[no-]show-parsed         LOG_SHOW_PARSED_TRACKS          [{yn(LOG_SHOW_PARSED_TRACKS)}]\n"
        f"                              Выводить в лог полный список всех спарсенных треков Spotify.\n"
        f"   --[no-]show-scoring        LOG_SHOW_SCORING                [{yn(LOG_SHOW_SCORING)}]\n"
        f"                              Подробно логировать баллы и причины отклонения каждого кандидата.\n"
        f"   --[no-]show-skipped        LOG_SHOW_SKIPPED                [{yn(LOG_SHOW_SKIPPED)}]\n"
        f"                              Логировать каждый пропущенный трек, который уже скачан на диск.\n"
        f"   --[no-]show-quarantine     LOG_SHOW_QUARANTINE             [{yn(LOG_SHOW_QUARANTINE)}]\n"
        f"                              Логировать треки, пропускаемые из-за активного карантина.\n"
        f"   --[no-]download-progress   LOG_DOWNLOAD_PROGRESS           [{yn(LOG_DOWNLOAD_PROGRESS)}]\n"
        f"                              Показывать шаги 25% / 50% / 75% / 100% и скорость скачивания.\n"
        f"   --[no-]colors              LOG_COLORS                      [{yn(LOG_COLORS)}]\n"
        f"                              Цветное ANSI-оформление вывода в консоли.\n\n"
        f" [7] ПРАКТИЧЕСКИЕ ПРИМЕРЫ ИСПОЛЬЗОВАНИЯ\n"
        f"{sep_sub}\n"
        f"   1. Проверить статус базы, диска, куки и карантина:\n"
        f"      docker exec -it azuracast_spotisync python /app/sync_spotify.py --status\n\n"
        f"   2. Проверить связь с AzuraCast и узнать ID/short_name станций и плейлистов:\n"
        f"      docker exec -it azuracast_spotisync python /app/sync_spotify.py --azura-check\n\n"
        f"   3. Настроить куки YouTube (для обхода 18+ Age-Gate и защиты от ботов):\n"
        f"      docker exec -it azuracast_spotisync python /app/sync_spotify.py --auth\n\n"
        f"   4. Принудительно перекачать один трек из плейлиста по названию или ID:\n"
        f"      docker exec -it azuracast_spotisync python /app/sync_spotify.py --track \"JR Serpent - Epic Sax\"\n\n"
        f"   5. Скачать любой сторонний трек (которого нет в плейлисте) и добавить в AzuraCast:\n"
        f"      docker exec -it azuracast_spotisync python /app/sync_spotify.py --track \"https://open.spotify.com/track/2wm6XXZr3nV8trRDVQLpUI\" --allow-external\n\n"
        f"   6. Запустить один проход синхронизации в 8 потоков с игнорированием карантина:\n"
        f"      docker exec -it azuracast_spotisync python /app/sync_spotify.py --once --ignore-quarantine --workers 8\n\n"
        f"   7. Запустить синхронизацию строго по проверенным источникам (без фоллбэка 4-го уровня):\n"
        f"      docker exec -it azuracast_spotisync python /app/sync_spotify.py --once --no-fallback-search\n"
        f"{sep_main}\n"
    )


def print_quarantine_report(
    quarantine: dict[str, dict[str, Any]] | None = None,
    cache_data: dict[str, Any] | None = None,
    meta_quarantine: dict[str, dict[str, Any]] | None = None,
) -> None:
    if quarantine is None:
        quarantine = load_quarantine()
    if meta_quarantine is None:
        meta_quarantine = load_meta_quarantine()
    if cache_data is None:
        cache_data = load_folder_cache()

    tracks_cache = cache_data.get("tracks") or {}
    now = time.time()

    print("\n" + "=" * 88)
    print(f" 1. КАРАНТИН СКАЧИВАНИЯ ТРЕКОВ ({len(quarantine)} шт.) | Файл: {FAILED_CACHE_FILE}")
    print("=" * 88)
    if not quarantine:
        print(" Карантин скачивания пуст! Все треки доступны для обработки.")
    else:
        width = len(str(len(quarantine)))
        for idx, (sp_id, q_info) in enumerate(quarantine.items(), 1):
            meta_dict = (tracks_cache.get(sp_id) or {}).get("meta") or {}
            artist = meta_dict.get("artist") or "Unknown"
            title = meta_dict.get("title") or sp_id
            disp = f"{artist} - {title}" if title != sp_id else sp_id
            q_time = float(q_info.get("time", now))
            q_ttl = float(q_info.get("ttl_hours", FAIL_TTL_HOURS))
            left_sec = max(0.0, q_ttl * 3600 - (now - q_time))
            on_disk = (OUTPUT_DIR / f"{sp_id}.mp3").exists()
            print(
                f" [{idx:0{width}d}/{len(quarantine)}] {disp} ({sp_id}.mp3)\n"
                f"       • На диске: {yn(on_disk)} | Попыток: {int(q_info.get('attempts', 1))} | "
                f"Осталось: {format_duration(left_sec)} из {format_duration(q_ttl * 3600)}\n"
                f"       • Причина: {q_info.get('reason', 'Неизвестная ошибка')}"
            )

    print("\n" + "=" * 88)
    print(f" 2. КАРАНТИН ОБОГАЩЕНИЯ МЕТАДАННЫХ ({len(meta_quarantine)} шт.) | Файл: {FAILED_META_CACHE_FILE}")
    print("=" * 88)
    if not meta_quarantine:
        print(" Карантин метаданных пуст!")
    else:
        width_m = len(str(len(meta_quarantine)))
        for idx, (sp_id, mq_info) in enumerate(meta_quarantine.items(), 1):
            meta_dict = (tracks_cache.get(sp_id) or {}).get("meta") or {}
            artist = meta_dict.get("artist") or "Unknown"
            title = meta_dict.get("title") or sp_id
            disp = f"{artist} - {title}" if title != sp_id else sp_id
            mq_time = float(mq_info.get("time", now))
            mq_ttl = float(mq_info.get("ttl_hours", max(24.0, META_FAIL_TTL_HOURS)))
            left_sec = max(0.0, mq_ttl * 3600 - (now - mq_time))
            print(
                f" [{idx:0{width_m}d}/{len(meta_quarantine)}] {disp} ({sp_id})\n"
                f"       • Не найдено: {mq_info.get('missing', 'ISRC/Год')} | Циклов: {int(mq_info.get('attempts', 1))} | "
                f"Фейлов сервисов: {int(mq_info.get('service_fails', 1))} | "
                f"Осталось: {format_duration(left_sec)} из {format_duration(mq_ttl * 3600)}\n"
                f"       • Причина: {mq_info.get('reason', 'Неизвестно')}"
            )
    print("=" * 88 + "\n")


def print_custom_tracks_report() -> None:
    cache_data = load_folder_cache()
    tracks_cache = cache_data.get("tracks") or {}
    print("\n" + "=" * 88)
    print(f" КАСТОМНЫЕ ТРЕКИ, ИГНОР-ЛИСТ И ПЕРЕОПРЕДЕЛЕНИЯ ({CUSTOM_TRACKS_PATH})")
    print("=" * 88)
    if not CUSTOM_TRACKS_PATH.exists():
        print(f" Файл {CUSTOM_TRACKS_PATH} еще не создан.\n" + "=" * 88 + "\n")
        return
    try:
        raw = parse_jsonc(CUSTOM_TRACKS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f" [ОШИБКА] Не удалось прочитать {CUSTOM_TRACKS_PATH}: {e}\n" + "=" * 88 + "\n")
        return

    ignore_entries = parse_raw_ignore_entries(raw.get("ignores"))
    ignored_ids_set = {sp_id for sp_id, _ in ignore_entries}
    print(f" 0. ИГНОРИРУЕМЫЕ ТРЕКИ SPOTIFY (ignores): {len(ignore_entries)} шт.\n" + "-" * 88)
    if not ignore_entries:
        print("   (Нет игнорируемых треков)")
    for idx, (sp_id, reason_ign) in enumerate(ignore_entries, 1):
        m_dict = (tracks_cache.get(sp_id) or {}).get("meta") or {}
        if m_dict.get("artist") and m_dict.get("title"):
            disp_ign = f"{m_dict['artist']} - {m_dict['title']} ({sp_id})"
        else:
            disp_ign = sp_id
        reason_suffix = f" | Причина: {reason_ign}" if reason_ign else ""
        print(f"   [{idx:02d}] {disp_ign}{reason_suffix}")

    valid_overrides: list[tuple[str, str, bool]] = []
    for k, val in (raw.get("overrides") or {}).items():
        if k.startswith("EXAMPLE_") or "EXAMPLE_ID" in k:
            continue
        url_val = val.get("url", "").strip() if isinstance(val, dict) else str(val).strip()
        if url_val and "EXAMPLE" not in url_val:
            sp_ov = extract_spotify_track_id(k)
            is_blocked_by_ignore = bool(sp_ov and sp_ov in ignored_ids_set)
            valid_overrides.append((k.strip(), url_val, is_blocked_by_ignore))

    print("\n" + "-" * 88 + f"\n 1. ПЕРЕОПРЕДЕЛЕНИЯ ССЫЛОК (overrides): {len(valid_overrides)} шт.\n" + "-" * 88)
    if not valid_overrides:
        print("   (Нет активных переопределений)")
    for idx, (k_str, url_str, is_blocked) in enumerate(valid_overrides, 1):
        matched_id = extract_spotify_track_id(k_str) or ""
        matched_name, on_disk, f_size = k_str, False, 0
        if matched_id:
            m_dict = (tracks_cache.get(matched_id) or {}).get("meta") or {}
            if m_dict.get("artist") and m_dict.get("title"):
                matched_name = f"{m_dict['artist']} - {m_dict['title']} ({matched_id})"
            fpath = OUTPUT_DIR / f"{matched_id}.mp3"
            if fpath.exists() and fpath.stat().st_size > 50_000:
                on_disk, f_size = True, fpath.stat().st_size
        status_note = " [ПРОПУЩЕНО: В СПИСКЕ IGNORES]" if is_blocked else ""
        print(
            f"   [{idx:02d}] {matched_name}{status_note} | "
            f"На диске: {yn(on_disk)}{f' ({format_bytes(f_size)})' if on_disk else ''} -> {url_str}"
        )

    real_custom = [
        item for item in (raw.get("custom_tracks") or [])
        if isinstance(item, dict) and item.get("url") and "EXAMPLE" not in str(item.get("url"))
    ]
    print("\n" + "-" * 88 + f"\n 2. КАСТОМНЫЕ ТРЕКИ (custom_tracks): {len(real_custom)} шт.\n" + "-" * 88)
    if not real_custom:
        print("   (Нет кастомных треков)")
    for idx, item in enumerate(real_custom, 1):
        enabled = item.get("enabled", True) is not False
        url = str(item.get("url") or "").strip()
        raw_id = str(item.get("id") or f"custom_{hashlib.md5(url.encode()).hexdigest()[:10]}").strip()
        custom_id = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_id)
        if not custom_id.startswith("custom_"):
            custom_id = f"custom_{custom_id}"
        fpath = OUTPUT_DIR / f"{custom_id}.mp3"
        on_disk = fpath.exists() and fpath.stat().st_size > 50_000
        print(
            f"   [{idx:02d}] {item.get('artist', 'Custom Artist')} - {item.get('title', f'Track #{idx}')} ({custom_id}.mp3) | "
            f"Активен: {yn(enabled)} | На диске: {yn(on_disk)} -> {url}"
        )
    print("=" * 88 + "\n")


def print_status_report() -> None:
    cache_data = load_folder_cache()
    local_managed, pending_cnt = reconcile_cache_with_disk(cache_data)
    overrides_map, custom_tracks, _, ignores_cnt = load_custom_spotitracks()
    unique_overrides_cnt = len({id(v) for v in overrides_map.values()})
    quarantine = load_quarantine()
    meta_quarantine = load_meta_quarantine()
    total_bytes, mp3_count, free_bytes = measure_directory_stats(OUTPUT_DIR)
    tracks_cache = cache_data.get("tracks") or {}
    full_meta_cnt = sum(1 for e in tracks_cache.values() if (e.get("meta") or {}).get("isrc") and (e.get("meta") or {}).get("release_date"))
    ok_cookie, cookie_msg = inspect_cookie_file_health(YT_COOKIE_FILE)

    print("\n" + "=" * 88)
    print(f" СТАТУС СИСТЕМЫ И БАЗЫ ДАННЫХ SPOTISYNC | {BUILD_VERSION}")
    print("=" * 88)
    print(f" • Плейлист Spotify          : {PLAYLIST_URL or cache_data.get('playlist_url') or 'Не задан'} (Рынок: {SPOTIFY_MARKET})")
    print(f" • Snapshot ID кэша          : {cache_data.get('snapshot_id') or 'Нет'} (Полный список: {yn(cache_data.get('is_full_playlist'))})")
    print(f" • Треков в кэше (.spotisync): {len(tracks_cache)} шт. (Полные ISRC+Год: {full_meta_cnt} шт.)")
    print(f" • Скачано на диск ({OUTPUT_DIR}): {len(local_managed)} шт. (Всего MP3 в папке: {mp3_count} шт.)")
    print(
        f" • Ожидают докачки / Карантин: Докачка: {pending_cnt} шт. | "
        f"Карантин треков: {len(quarantine)} шт. | Карантин метаданных: {len(meta_quarantine)} шт."
    )
    print(
        f" • Кастомных (.spotitracks)  : Треков: {len(custom_tracks)} шт. | "
        f"Переопределений: {unique_overrides_cnt} шт. | В черном списке (ignores): {ignores_cnt} шт. "
        f"(Удаление скачанных: {yn(SYNC_DELETE_IGNORED)})"
    )
    print(f" • Место на диске            : Занято: {format_bytes(total_bytes)} | Свободно: {format_bytes(free_bytes)}")
    print(f" • Авторизация YouTube (18+) : {yn(ok_cookie)} ({cookie_msg})")
    print(f" • Фоллбэки поиска           : DirectURL->Каскад: {yn(DIRECT_URL_FALLBACK)} | Фоллбэк YT #4: {yn(ENABLE_FALLBACK_SEARCH)}")
    print("=" * 88)
    if quarantine or meta_quarantine:
        print_quarantine_report(quarantine=quarantine, cache_data=cache_data, meta_quarantine=meta_quarantine)
    else:
        print()


async def run_retag_all_on_disk() -> None:
    token_ctx = current_ctx.set("RETAG")
    try:
        cache_data = load_folder_cache()
        local_managed, _ = reconcile_cache_with_disk(cache_data)
        overrides_map, _, _, _ = load_custom_spotitracks()
        tracks_cache = cache_data.get("tracks") or {}

        logger.info(f"=== Запуск перетегирования (--retag) для {len(local_managed)} файлов на диске ===")
        retagged = 0
        for sp_id, mp3_path in local_managed.items():
            if is_shutting_down():
                break
            if not (meta_dict := (tracks_cache.get(sp_id) or {}).get("meta")):
                continue
            meta = TrackMeta.from_dict(meta_dict)
            apply_overrides_to_tracks([meta], overrides_map)
            try:
                await asyncio.to_thread(tag_mp3_file, mp3_path, meta)
                retagged += 1
                if retagged % 25 == 0 or retagged == len(local_managed):
                    logger.info(f"[RETAG] Обновлены ID3-теги: {retagged}/{len(local_managed)}")
            except Exception as e:
                logger.warning(f"Ошибка обновления тегов для {mp3_path.name}: {e}")
        logger.success(f"Перетегирование завершено! Обновлено файлов: {retagged} шт.")
    finally:
        current_ctx.reset(token_ctx)


def match_tracks_by_query(query: str, pool: list[TrackMeta], cache_tracks_map: dict[str, Any]) -> list[TrackMeta]:
    q_raw = query.strip()
    if not q_raw:
        return []
    q_low = q_raw.lower()

    sp_match = re.search(r"(?:track/|spotify:track:)([a-zA-Z0-9]{22})", q_raw)
    extracted_sp_id = sp_match.group(1) if sp_match else (q_raw if len(q_raw) == 22 and q_raw.isalnum() else None)
    yt_match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([a-zA-Z0-9_-]{11})", q_raw)
    extracted_yt_id = yt_match.group(1) if yt_match else (q_raw if re.fullmatch(r"[a-zA-Z0-9_-]{11}", q_raw) else None)
    is_sc_url = "soundcloud.com/" in q_low

    exact_matches: list[TrackMeta] = []
    fuzzy_matches: list[TrackMeta] = []
    q_compact = compact_alnum(q_raw)
    q_tokens = normalize_tokens(q_raw)

    for t in pool:
        c_entry = cache_tracks_map.get(t.spotify_id) or {}
        combined_urls = f"{c_entry.get('source_url') or ''} {t.direct_url or ''}"
        if (
            (extracted_sp_id and t.spotify_id.lower() == extracted_sp_id.lower())
            or (extracted_yt_id and extracted_yt_id in combined_urls)
            or (is_sc_url and q_low.rstrip("/") in combined_urls.lower())
        ):
            exact_matches.append(t)
            continue

        full_disp = f"{t.artist} - {t.title}".lower()
        all_art_disp = f"{', '.join(t.artists_all)} - {t.title} {t.album}".lower()
        if q_low in full_disp or q_low in all_art_disp or (q_compact and q_compact in compact_alnum(full_disp)):
            fuzzy_matches.append(t)
            continue
        if q_tokens:
            disp_tokens = normalize_tokens(all_art_disp)
            if all(any(w.startswith(tok) or tok in w for w in disp_tokens) for tok in q_tokens):
                fuzzy_matches.append(t)

    return exact_matches or fuzzy_matches


async def handle_single_track_cli(queries: list[str], allow_external: bool) -> None:
    token_ctx = current_ctx.set("TRACK-CLI")
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        cache_data = load_folder_cache()
        reconcile_cache_with_disk(cache_data)
        overrides_map, custom_tracks, ignored_keys, _ = load_custom_spotitracks()
        quarantine = load_quarantine()

        tracks_cache = cache_data.setdefault("tracks", {})
        pool_map: dict[str, TrackMeta] = {
            sp_id: TrackMeta.from_dict(m_dict)
            for sp_id, entry in tracks_cache.items()
            if (m_dict := entry.get("meta"))
        }
        for ct in custom_tracks:
            pool_map[ct.spotify_id] = ct

        if not pool_map and PLAYLIST_URL:
            logger.info("Локальный кэш пуст, загружаем список треков плейлиста со Spotify...")
            fetched, snap, _, _, _, _, _ = await fetch_spotify_tracks_with_cache(
                PLAYLIST_URL, cache_data, ignored_keys=ignored_keys
            )
            if snap:
                cache_data["snapshot_id"] = snap
            for ft in fetched:
                pool_map[ft.spotify_id] = ft

        pool_list = apply_overrides_to_tracks(list(pool_map.values()), overrides_map)
        targets_to_process: list[TrackMeta] = []
        seen_target_ids: set[str] = set()

        async with httpx.AsyncClient(timeout=20.0) as http_client:
            for q in queries:
                if is_shutting_down():
                    break
                if matched := match_tracks_by_query(q, pool_list, tracks_cache):
                    logger.info(f"[TRACK SEARCH] По запросу '{q}' найдено совпадений: {len(matched)} шт.")
                    for m in matched:
                        if m.spotify_id not in seen_target_ids:
                            seen_target_ids.add(m.spotify_id)
                            targets_to_process.append(m)
                    continue

                if not allow_external:
                    logger.error(f"✖ Трек '{q}' НЕ НАЙДЕН в базе (TRACK_ALLOW_EXTERNAL: {yn(allow_external)})! Добавьте --allow-external.")
                    continue

                logger.warning(f"[EXTERNAL TRACK] Создаем внешнюю задачу для '{q}'...")
                q_strip = q.strip()
                sp_m = re.search(r"(?:track/|spotify:track:)([a-zA-Z0-9]{22})", q_strip)
                sp_id_ext = sp_m.group(1) if sp_m else (q_strip if len(q_strip) == 22 and q_strip.isalnum() else None)
                yt_m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([a-zA-Z0-9_-]{11})", q_strip)
                yt_id_ext = yt_m.group(1) if yt_m else (q_strip if re.fullmatch(r"[a-zA-Z0-9_-]{11}", q_strip) else None)

                if sp_id_ext:
                    ext_meta = await fetch_single_spotify_embed_meta(http_client, sp_id_ext)
                    if not ext_meta:
                        logger.error(f"Не удалось получить метаданные со Spotify для ID: {sp_id_ext}")
                        continue
                    await enrich_single_track_via_deezer(http_client, asyncio.Semaphore(1), ext_meta)
                    targets_to_process.append(ext_meta)
                elif yt_id_ext or q_strip.startswith(("http://", "https://")):
                    direct_link = f"https://www.youtube.com/watch?v={yt_id_ext}" if (yt_id_ext and not q_strip.startswith("http")) else q_strip
                    ext_id = f"custom_ext_{yt_id_ext or hashlib.md5(direct_link.encode()).hexdigest()[:10]}"
                    targets_to_process.append(
                        TrackMeta(
                            spotify_id=ext_id, title="External Track", artist="External Artist",
                            artists_all=["External Artist"], album="External Download", album_artist="External Artist",
                            release_date="", track_number="1", disc_number="1", duration_sec=0,
                            isrc=None, cover_url=None, direct_url=direct_link,
                        )
                    )
                else:
                    art_s, tit_s = (p.strip() for p in q_strip.split("-", 1)) if "-" in q_strip else ("Unknown Artist", q_strip)
                    ext_id = f"custom_ext_{hashlib.md5(q_strip.lower().encode()).hexdigest()[:10]}"
                    ext_meta = TrackMeta(
                        spotify_id=ext_id, title=tit_s, artist=art_s, artists_all=[art_s],
                        album=tit_s, album_artist=art_s, release_date="", track_number="1",
                        disc_number="1", duration_sec=0, isrc=None, cover_url=None,
                    )
                    await enrich_single_track_via_deezer(http_client, asyncio.Semaphore(1), ext_meta)
                    targets_to_process.append(ext_meta)

        if not targets_to_process or is_shutting_down():
            return

        keep_alive_youtube_cookies()
        ok_cnt = 0
        for idx, meta in enumerate(targets_to_process, 1):
            if is_shutting_down():
                break

            label = f"TRACK #{idx}/{len(targets_to_process)} | {meta.safe_id[:10]} | {meta.display_name[:25]}"
            ok, reason = await run_worker_with_adaptive_watchdog(meta, label, cache_data, quarantine)
            if ok:
                ok_cnt += 1
                clear_quarantine_entry(quarantine, meta.spotify_id)
            elif not is_shutting_down():
                ttl_h = register_quarantine_failure(quarantine, meta.spotify_id, reason or "Ошибка --track")
                logger.error(f"[{label}] Не удалось скачать трек: {reason} (карантин: {format_duration(ttl_h * 3600)})")

        if ok_cnt > 0 and is_azuracast_configured() and not is_shutting_down():
            all_mp3s = [p.name for p in OUTPUT_DIR.glob("*.mp3") if p.stat().st_size > 50_000]
            await sync_with_azuracast(all_mp3s, ok_cnt)
        logger.success(f"Команда --track завершена: успешно скачано {ok_cnt} из {len(targets_to_process)} шт.")
    finally:
        current_ctx.reset(token_ctx)


async def run_worker_with_adaptive_watchdog(
    meta: TrackMeta,
    label: str,
    cache_data: dict[str, Any],
    quarantine: dict[str, dict[str, Any]],
) -> tuple[bool, str]:
    watchdog = AdaptiveWorkerWatchdog(base_timeout=float(WORKER_BASE_TIMEOUT_SEC))
    with _active_watchdogs_lock:
        _active_watchdogs.add(watchdog)
    try:
        task = asyncio.create_task(
            asyncio.to_thread(process_track_sync, meta, label, cache_data, watchdog, quarantine)
        )
        while not task.done():
            await asyncio.sleep(1.0)
            if task.done():
                break
            expired, exp_reason = watchdog.check_expired()
            if expired:
                watchdog.cancel(exp_reason)
                if is_shutting_down():
                    return False, exp_reason
                try:
                    async with asyncio.timeout(20.0):
                        return await asyncio.shield(task)
                except TimeoutError:
                    return False, exp_reason
        return await task
    finally:
        with _active_watchdogs_lock:
            _active_watchdogs.discard(watchdog)


def cleanup_stale_staging_dirs() -> None:
    if STAGING_DIR.exists():
        for d in STAGING_DIR.glob("sp_*"):
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)


async def run_sync_cycle(
    ignore_quarantine: bool = False,
    redownload_all: bool = False,
    dry_run: bool = False,
) -> None:
    global soundcloud_consecutive_403, DEFAULT_PLAYLIST_COVER_URL
    if is_shutting_down():
        return
    with soundcloud_lock:
        soundcloud_consecutive_403 = 0

    cycle_start = time.monotonic()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_stale_staging_dirs()
    size_before, _, _ = measure_directory_stats(OUTPUT_DIR)
    cache_data = load_folder_cache()

    if redownload_all and not dry_run:
        logger.warning("=== АКТИВИРОВАН РЕЖИМ --redownload: Удаляем старые MP3 ===")
        for mp3_f in OUTPUT_DIR.glob("*.mp3"):
            stem = mp3_f.stem
            if stem in (cache_data.get("tracks") or {}) or (len(stem) == 22 and stem.isalnum()) or stem.startswith("custom_"):
                mp3_f.unlink(missing_ok=True)

    local_managed, _ = reconcile_cache_with_disk(cache_data)
    overrides_map, custom_tracks, ignored_keys, _ = load_custom_spotitracks()

    target_playlist = PLAYLIST_URL or cache_data.get("playlist_url", "")
    if not target_playlist and not custom_tracks:
        logger.error("Не указан PLAYLIST_URL и нет кастомных треков в .spotitracks.json!")
        return

    logger.info(
        f"=== Старт цикла синхронизации [{BUILD_VERSION}] | "
        f"Фильтр недоступных: {yn(FILTER_UNAVAILABLE_SPOTIFY)} | IgnoreCachedURLs: {yn(IGNORE_CACHED_URLS)} | "
        f"DirectURL->Каскад: {yn(DIRECT_URL_FALLBACK)} | Фоллбэк YT #4: {yn(ENABLE_FALLBACK_SEARCH)} | "
        f"DelRemoved: {yn(SYNC_DELETE_REMOVED)} | DelIgnored: {yn(SYNC_DELETE_IGNORED)} | "
        f"Обход карантина: {yn(ignore_quarantine)} | AzuraCast: {yn(is_azuracast_configured())} | "
        f"DryRun: {yn(dry_run)} | Плейлист: {target_playlist} ==="
    )
    keep_alive_youtube_cookies()
    if is_shutting_down():
        return

    tracks: list[TrackMeta] = []
    new_snapshot = ""
    from_cache_hit = False
    raw_playlist_total = filtered_unavailable_count = 0

    if target_playlist:
        (
            tracks, new_snapshot, from_cache_hit, raw_playlist_total,
            filtered_unavailable_count, parse_dt, enrich_dt,
        ) = await fetch_spotify_tracks_with_cache(
            target_playlist, cache_data, ignored_keys=ignored_keys, ignore_quarantine=ignore_quarantine
        )

        for t in tracks:
            if t.cover_url:
                DEFAULT_PLAYLIST_COVER_URL = t.cover_url
                break

        if not from_cache_hit:
            logger.info(
                f"Итог чтения базы: Парсинг Spotify: {format_duration(parse_dt)} | "
                f"Обогащение (ISRC+Год): {format_duration(enrich_dt)} | "
                f"Всего в плейлисте: {raw_playlist_total} | Отфильтровано: {filtered_unavailable_count} | Доступно: {len(tracks)}"
            )

    if is_shutting_down():
        return

    if ignored_keys and tracks:
        before_ign = len(tracks)
        tracks = [t for t in tracks if not is_track_in_ignore_set(t, ignored_keys)]
        ign_removed = before_ign - len(tracks)
        if ign_removed > 0:
            filtered_unavailable_count += ign_removed
            logger.info(
                f"[SPOTITRACKS IGNORES] Отфильтровано по черному списку ignores из {CUSTOM_TRACKS_PATH.name}: {ign_removed} треков."
            )

    tracks = apply_overrides_to_tracks(tracks, overrides_map)
    before_valid_len = len(tracks)
    tracks = [t for t in tracks if is_valid_spotify_track(t)]
    filtered_unavailable_count += before_valid_len - len(tracks)
    spotify_final_count = len(tracks)

    existing_ids = {t.spotify_id for t in tracks}
    for ct in custom_tracks:
        if ct.spotify_id not in existing_ids:
            tracks.append(ct)
            existing_ids.add(ct.spotify_id)

    playlist_ids = {t.spotify_id for t in tracks}
    cache_tracks_map = cache_data.setdefault("tracks", {})

    deleted_count = 0
    if ignored_keys:
        ignored_downloaded_ids = [
            ign_id for ign_id in ignored_keys
            if ign_id in local_managed or ign_id in cache_tracks_map
        ]
        if SYNC_DELETE_IGNORED and ignored_downloaded_ids and not dry_run:
            ign_files_to_unlink = [
                local_managed[ign_id].name
                for ign_id in ignored_downloaded_ids
                if ign_id in local_managed
            ]
            if ign_files_to_unlink and is_azuracast_configured():
                await unassign_tracks_from_azuracast_playlist(ign_files_to_unlink)

            for ign_id in ignored_downloaded_ids:
                meta_d = (cache_tracks_map.get(ign_id) or {}).get("meta") or {}
                disp_ign = (
                    f"{meta_d['artist']} - {meta_d['title']}"
                    if meta_d.get("artist") and meta_d.get("title")
                    else ign_id
                )
                if mp3_path := local_managed.pop(ign_id, None):
                    try:
                        mp3_path.unlink(missing_ok=True)
                        deleted_count += 1
                        logger.info(
                            f"[IGNORES-DEL] Удален ранее скачанный трек из черного списка ignores: "
                            f"{mp3_path.name} ({disp_ign})"
                        )
                    except OSError as e:
                        logger.error(f"Ошибка удаления игнорируемого трека {mp3_path.name}: {e}")
                cache_tracks_map.pop(ign_id, None)
        else:
            for ign_id in ignored_downloaded_ids:
                if ign_id not in local_managed:
                    cache_tracks_map.pop(ign_id, None)

    forced_resync_count = 0
    for t in tracks:
        prev_entry = cache_tracks_map.get(t.spotify_id)
        prev_source_url = (prev_entry.get("source_url") or "").strip() if prev_entry else ""
        prev_override_url = (prev_entry.get("override_url") or "").strip() if prev_entry else ""
        prev_source_type = (prev_entry.get("source_type") or "").strip() if prev_entry else ""

        if t.direct_url and t.spotify_id in local_managed and not dry_run:
            already_synced_with_this_override = (
                is_same_media_url(prev_source_url, t.direct_url)
                or is_same_media_url(prev_override_url, t.direct_url)
            )
            if not already_synced_with_this_override:
                local_managed.pop(t.spotify_id, None)
                forced_resync_count += 1
                logger.warning(
                    f"[SPOTITRACKS RESYNC] Источник для '{t.display_name}' изменен -> "
                    f"проверяем новый источник (текущий MP3 сохранен на случай совпадения или сбоя)!"
                )

        is_currently_on_disk = (OUTPUT_DIR / t.id_filename).exists() and (OUTPUT_DIR / t.id_filename).stat().st_size > 50_000
        if t.spotify_id not in cache_tracks_map:
            cache_tracks_map[t.spotify_id] = {
                "meta": asdict(t),
                "filename": t.id_filename,
                "source_url": t.direct_url,
                "override_url": t.direct_url,
                "source_type": "spotitracks" if t.direct_url else None,
                "score": 100 if t.direct_url else 0,
                "downloaded": is_currently_on_disk,
                "file_size": (OUTPUT_DIR / t.id_filename).stat().st_size if is_currently_on_disk else 0,
                "synced_at": int(time.time()),
            }
        else:
            cache_tracks_map[t.spotify_id]["meta"] = asdict(t)
            cache_tracks_map[t.spotify_id]["filename"] = t.id_filename
            if t.direct_url and t.spotify_id in local_managed:
                cache_tracks_map[t.spotify_id]["override_url"] = t.direct_url
                if is_same_media_url(prev_source_url, t.direct_url) or not prev_source_url:
                    cache_tracks_map[t.spotify_id]["source_url"] = t.direct_url
                    cache_tracks_map[t.spotify_id]["source_type"] = "spotitracks"
            elif not t.direct_url:
                cache_tracks_map[t.spotify_id]["override_url"] = None
            cache_tracks_map[t.spotify_id]["downloaded"] = is_currently_on_disk

    cache_data["playlist_url"] = target_playlist
    if new_snapshot:
        cache_data["snapshot_id"] = new_snapshot

    removed_ids = ((set(local_managed.keys()) | set(cache_tracks_map.keys())) - playlist_ids) - ignored_keys
    if SYNC_DELETE_REMOVED and removed_ids and not dry_run:
        files_to_unlink = [local_managed[rem_id].name for rem_id in removed_ids if rem_id in local_managed]
        if files_to_unlink and is_azuracast_configured():
            await unassign_tracks_from_azuracast_playlist(files_to_unlink)
        for rem_id in removed_ids:
            if rem_id in local_managed:
                try:
                    local_managed[rem_id].unlink()
                    deleted_count += 1
                    logger.info(f"[SYNC-DEL] Удален выбывший трек: {local_managed[rem_id].name}")
                except OSError as e:
                    logger.error(f"Ошибка удаления {local_managed[rem_id].name}: {e}")
            cache_tracks_map.pop(rem_id, None)

    save_folder_cache(cache_data)

    quarantine = load_quarantine()
    meta_quarantine = load_meta_quarantine()
    if ignored_keys:
        for q_id in list(quarantine.keys()):
            if q_id in ignored_keys:
                clear_quarantine_entry(quarantine, q_id)
        for mq_id in list(meta_quarantine.keys()):
            if mq_id in ignored_keys:
                clear_meta_quarantine_entry(meta_quarantine, mq_id)

    to_download: list[TrackMeta] = []
    skipped_count = quarantined_count = 0
    show_skipped = LOG_SHOW_SKIPPED or logger.isEnabledFor(logging.DEBUG)
    skip_log_fn = logger.info if LOG_SHOW_SKIPPED else logger.debug
    show_quarantine = LOG_SHOW_QUARANTINE or logger.isEnabledFor(logging.DEBUG)
    quar_log_fn = logger.info if LOG_SHOW_QUARANTINE else logger.debug

    for t in tracks:
        if t.spotify_id in local_managed:
            skipped_count += 1
            if show_skipped:
                skip_log_fn(f"[SKIP] Уже на диске: {t.id_filename} ({t.display_name})")
        elif (not ignore_quarantine) and (t.spotify_id in quarantine) and (not t.direct_url):
            quarantined_count += 1
            q_info = quarantine[t.spotify_id]
            q_ttl = float(q_info.get("ttl_hours", FAIL_TTL_HOURS))
            left_sec = max(0.0, q_ttl * 3600 - (time.time() - float(q_info.get("time", time.time()))))
            if show_quarantine:
                quar_log_fn(
                    f"[QUARANTINE] Пропуск ({format_duration(left_sec)} из {format_duration(q_ttl * 3600)} осталось | "
                    f"попыток: {int(q_info.get('attempts', 1))}) | "
                    f"Причина: {q_info.get('reason', 'Неизвестно')} -> {t.display_name}"
                )
        else:
            to_download.append(t)

    logger.info(
        f"Статус базы: Всего: {raw_playlist_total} | Отфильтровано: {filtered_unavailable_count} | "
        f"Из Spotify: {spotify_final_count} | Кастомных: {len(custom_tracks)} | Перекачка: {forced_resync_count} | "
        f"В работе: {len(tracks)} | На диске: {skipped_count} | В карантине: {quarantined_count} | "
        f"К скачиванию: {len(to_download)} | Удалено: {deleted_count}"
    )

    downloaded_ok = failed_count = 0
    if dry_run:
        logger.info(f"[DRY-RUN] Пропускаем скачивание {len(to_download)} треков.")
    elif to_download and not is_shutting_down():
        sem = asyncio.Semaphore(max(1, CONCURRENT_DOWNLOADS))
        total_dl = len(to_download)
        width = len(str(total_dl))

        async def worker(idx: int, meta: TrackMeta) -> bool:
            if is_shutting_down():
                return False
            async with sem:
                if is_shutting_down():
                    return False
                label = f"#{idx:0{width}d}/{total_dl} | {meta.safe_id[:8]} | {meta.display_name[:25]}"
                try:
                    ok, fail_reason = await run_worker_with_adaptive_watchdog(meta, label, cache_data, quarantine)
                except asyncio.CancelledError:
                    return False
                except Exception as e:
                    ok, fail_reason = False, f"Исключение воркера: {e}"

                final_fpath = OUTPUT_DIR / meta.id_filename
                if ok or (final_fpath.exists() and final_fpath.stat().st_size > 50_000):
                    clear_quarantine_entry(quarantine, meta.spotify_id)
                    return True
                if is_shutting_down():
                    return False

                reason_str = fail_reason or "Не удалось найти или скачать трек"
                ttl_assigned = register_quarantine_failure(quarantine, meta.spotify_id, reason_str)
                logger.warning(f"[{label}] Отправлен в карантин на {format_duration(ttl_assigned * 3600)} | Причина: {reason_str}")
                return False

        results = await asyncio.gather(*(worker(i, t) for i, t in enumerate(to_download, 1)), return_exceptions=True)
        downloaded_ok = sum(1 for r in results if r is True)
        failed_count = sum(1 for r in results if r is False)

    if is_azuracast_configured() and not dry_run and not is_shutting_down():
        expected_mp3_names = [t.id_filename for t in tracks if (OUTPUT_DIR / t.id_filename).exists()]
        await sync_with_azuracast(expected_mp3_names, downloaded_ok)

    if is_shutting_down():
        return

    total_time = time.monotonic() - cycle_start
    size_after, total_mp3_files, free_disk_bytes = measure_directory_stats(OUTPUT_DIR)
    size_delta = size_after - size_before
    logger.info(
        f"=== Итоги цикла: В плейлисте: {raw_playlist_total} | Финально: {len(tracks)} | Уже было: {skipped_count} | "
        f"Скачано: {downloaded_ok} | Ошибок: {failed_count} | В карантине: {len(quarantine)} | "
        f"Средняя скорость: {format_speed(speed_tracker.avg_speed_bps)} | "
        f"Время: {format_duration(total_time)} ==="
    )
    token_ctx = current_ctx.set("STORAGE")
    try:
        logger.info(
            f"Вес папки {OUTPUT_DIR}: {format_bytes(size_after)} ({'+' if size_delta >= 0 else ''}{format_bytes(size_delta)}) | "
            f"Всего MP3: {total_mp3_files} | Свободно: {format_bytes(free_disk_bytes)}"
        )
    finally:
        current_ctx.reset(token_ctx)


async def sleep_interruptible(total_seconds: float) -> None:
    end_t = time.monotonic() + total_seconds
    while (rem := end_t - time.monotonic()) > 0 and not is_shutting_down():
        await asyncio.sleep(min(1.0, rem))


async def main_loop(
    once: bool = False,
    ignore_quarantine: bool = False,
    redownload_all: bool = False,
    dry_run: bool = False,
) -> None:
    logger.info(f"Инициализация SpotiSync | Build: {BUILD_VERSION} | Лог: {LOG_FILE}")
    first_pass = True
    while not is_shutting_down():
        try:
            await run_sync_cycle(
                ignore_quarantine=(ignore_quarantine if first_pass else False),
                redownload_all=(redownload_all if first_pass else False),
                dry_run=dry_run,
            )
        except asyncio.CancelledError:
            break
        except Exception as e:
            if not is_shutting_down():
                logger.exception(f"Критическая ошибка цикла синхронизации: {e}")

        first_pass = False
        if once or SYNC_INTERVAL_MINUTES <= 0 or is_shutting_down():
            break
        logger.info(f"Сон до следующей синхронизации ({format_duration(SYNC_INTERVAL_MINUTES * 60)})...")
        await sleep_interruptible(SYNC_INTERVAL_MINUTES * 60)


def apply_env_key_value_override(key: str, val: str) -> None:
    global PLAYLIST_URL, SPOTIFY_MARKET, FILTER_UNAVAILABLE_SPOTIFY, IGNORE_CACHED_URLS, TRACK_ALLOW_EXTERNAL
    global SYNC_INTERVAL_MINUTES, OUTPUT_DIR, CACHE_FILENAME, CUSTOM_TRACKS_FILENAME, STAGING_DIR
    global SYNC_DELETE_REMOVED, SYNC_DELETE_IGNORED, PUID, PGID, CONCURRENT_DOWNLOADS, YTDLP_RETRIES, YTDLP_SOCKET_TIMEOUT_SEC
    global WORKER_BASE_TIMEOUT_SEC, WORKER_SEARCH_TIMEOUT_SEC, WORKER_STALL_TIMEOUT_SEC
    global WORKER_STAGE_MAX_TIMEOUT_SEC, MIN_ACCEPTABLE_SPEED_KBPS, FAIL_TTL_HOURS, META_FAIL_TTL_HOURS
    global FAILED_CACHE_FILE, FAILED_META_CACHE_FILE, YT_COOKIE_FILE, POT_PROVIDER_URL, AUDIO_NORMALIZE, AUDIO_TRIM_SILENCE
    global ENABLE_FALLBACK_SEARCH, DIRECT_URL_FALLBACK, TIME_FORMAT
    global AZURACAST_URL, AZURACAST_API_KEY, AZURACAST_STATION_ID, AZURACAST_PLAYLIST_ID
    global AZURACAST_PLAYLIST_NAME, AZURACAST_MEDIA_SUBDIR
    global LOG_LEVEL_STR, LOG_COLORS, LOG_FILE, LOG_SHOW_PARSED_TRACKS, LOG_SHOW_SCORING
    global LOG_SHOW_SKIPPED, LOG_SHOW_QUARANTINE, LOG_DOWNLOAD_PROGRESS

    k = key.upper().strip().replace("-", "_")
    v = val.strip()
    try:
        match k:
            case "TIME_FORMAT":
                if val:
                    TIME_FORMAT = val
            case "PLAYLIST_URL" | "PLAYLIST":
                PLAYLIST_URL = v
            case "SPOTIFY_MARKET" | "MARKET":
                SPOTIFY_MARKET = v or "US"
            case "FILTER_UNAVAILABLE_SPOTIFY" | "FILTER_UNAVAILABLE":
                FILTER_UNAVAILABLE_SPOTIFY = parse_str_bool(v)
            case "IGNORE_CACHED_URLS":
                IGNORE_CACHED_URLS = parse_str_bool(v)
            case "TRACK_ALLOW_EXTERNAL" | "ALLOW_EXTERNAL":
                TRACK_ALLOW_EXTERNAL = parse_str_bool(v)
            case "SYNC_INTERVAL_MINUTES" | "SYNC_INTERVAL" | "INTERVAL":
                SYNC_INTERVAL_MINUTES = float(v)
            case "OUTPUT_DIR":
                OUTPUT_DIR = Path(v)
            case "CACHE_FILENAME":
                CACHE_FILENAME = v
            case "CUSTOM_TRACKS_FILENAME" | "SPOTITRACKS_FILENAME":
                CUSTOM_TRACKS_FILENAME = v
            case "STAGING_DIR":
                STAGING_DIR = Path(v)
            case "SYNC_DELETE_REMOVED" | "SYNC_DELETE":
                SYNC_DELETE_REMOVED = parse_str_bool(v)
            case "SYNC_DELETE_IGNORED" | "DELETE_IGNORED_TRACKS" | "SYNC_DELETE_IGNORES":
                SYNC_DELETE_IGNORED = parse_str_bool(v)
            case "PUID":
                PUID = int(v)
            case "PGID":
                PGID = int(v)
            case "CONCURRENT_DOWNLOADS" | "WORKERS":
                CONCURRENT_DOWNLOADS = max(1, int(v))
            case "YTDLP_RETRIES" | "RETRIES":
                YTDLP_RETRIES = max(1, int(v))
            case "YTDLP_SOCKET_TIMEOUT_SEC" | "SOCKET_TIMEOUT":
                YTDLP_SOCKET_TIMEOUT_SEC = max(5, int(v))
            case "WORKER_TIMEOUT_SEC" | "WORKER_BASE_TIMEOUT_SEC" | "WORKER_TIMEOUT":
                WORKER_BASE_TIMEOUT_SEC = max(30, int(v))
            case "WORKER_SEARCH_STEP_TIMEOUT_SEC" | "WORKER_SEARCH_TIMEOUT_SEC" | "SEARCH_TIMEOUT":
                WORKER_SEARCH_TIMEOUT_SEC = max(15.0, float(v))
            case "WORKER_STALL_TIMEOUT_SEC" | "STALL_TIMEOUT":
                WORKER_STALL_TIMEOUT_SEC = max(15, int(v))
            case "WORKER_MAX_HARD_TIMEOUT_SEC" | "WORKER_STAGE_MAX_TIMEOUT_SEC" | "MAX_HARD_TIMEOUT":
                WORKER_STAGE_MAX_TIMEOUT_SEC = max(60, int(v))
            case "MIN_ACCEPTABLE_SPEED_KBPS" | "MIN_SPEED_KBPS":
                MIN_ACCEPTABLE_SPEED_KBPS = max(1.0, float(v))
            case "FAIL_TTL_HOURS" | "FAIL_TTL":
                FAIL_TTL_HOURS = max(0.0, float(v))
            case "FAILED_CACHE_FILE":
                FAILED_CACHE_FILE = Path(v)
            case "META_FAIL_TTL_HOURS" | "META_FAIL_TTL":
                META_FAIL_TTL_HOURS = max(0.0, float(v))
            case "FAILED_META_CACHE_FILE" | "FAILED_META_FILE":
                FAILED_META_CACHE_FILE = Path(v)
            case "YT_COOKIE_FILE" | "COOKIE_FILE":
                YT_COOKIE_FILE = Path(v)
            case "POT_PROVIDER_URL":
                POT_PROVIDER_URL = v
            case "AUDIO_NORMALIZE" | "NORMALIZE":
                AUDIO_NORMALIZE = parse_str_bool(v)
            case "AUDIO_TRIM_SILENCE" | "TRIM_SILENCE":
                AUDIO_TRIM_SILENCE = parse_str_bool(v)
            case "ENABLE_FALLBACK_SEARCH" | "FALLBACK_SEARCH":
                ENABLE_FALLBACK_SEARCH = parse_str_bool(v)
            case "DIRECT_URL_FALLBACK":
                DIRECT_URL_FALLBACK = parse_str_bool(v)
            case "AZURACAST_URL":
                AZURACAST_URL = v.rstrip("/")
            case "AZURACAST_API_KEY":
                AZURACAST_API_KEY = v
            case "AZURACAST_STATION_ID":
                AZURACAST_STATION_ID = v
            case "AZURACAST_PLAYLIST_ID":
                AZURACAST_PLAYLIST_ID = v
            case "AZURACAST_PLAYLIST_NAME":
                AZURACAST_PLAYLIST_NAME = v
            case "AZURACAST_MEDIA_SUBDIR":
                AZURACAST_MEDIA_SUBDIR = v.strip("/")
            case "LOG_LEVEL":
                LOG_LEVEL_STR = v.upper()
            case "LOG_COLORS" | "COLORS":
                LOG_COLORS = parse_str_bool(v)
            case "LOG_FILE":
                LOG_FILE = v
            case "LOG_SHOW_PARSED_TRACKS" | "SHOW_PARSED":
                LOG_SHOW_PARSED_TRACKS = parse_str_bool(v)
            case "LOG_SHOW_SCORING" | "SHOW_SCORING":
                LOG_SHOW_SCORING = parse_str_bool(v)
            case "LOG_SHOW_SKIPPED" | "SHOW_SKIPPED":
                LOG_SHOW_SKIPPED = parse_str_bool(v)
            case "LOG_SHOW_QUARANTINE" | "SHOW_QUARANTINE":
                LOG_SHOW_QUARANTINE = parse_str_bool(v)
            case "LOG_DOWNLOAD_PROGRESS" | "DOWNLOAD_PROGRESS":
                LOG_DOWNLOAD_PROGRESS = parse_str_bool(v)
    except ValueError as e:
        logger.warning(f"Некорректное значение для параметра {key}='{val}': {e}")


VALUE_CLI_FLAGS_MAP: dict[str, str] = {
    "--playlist-url": "PLAYLIST_URL",
    "--playlist": "PLAYLIST_URL",
    "--spotify-market": "SPOTIFY_MARKET",
    "--market": "SPOTIFY_MARKET",
    "--sync-interval": "SYNC_INTERVAL_MINUTES",
    "--sync-interval-minutes": "SYNC_INTERVAL_MINUTES",
    "--interval": "SYNC_INTERVAL_MINUTES",
    "--output-dir": "OUTPUT_DIR",
    "--cache-filename": "CACHE_FILENAME",
    "--custom-tracks-filename": "CUSTOM_TRACKS_FILENAME",
    "--spotitracks-filename": "CUSTOM_TRACKS_FILENAME",
    "--staging-dir": "STAGING_DIR",
    "--puid": "PUID",
    "--pgid": "PGID",
    "--concurrent-downloads": "CONCURRENT_DOWNLOADS",
    "--workers": "CONCURRENT_DOWNLOADS",
    "--ytdlp-retries": "YTDLP_RETRIES",
    "--retries": "YTDLP_RETRIES",
    "--ytdlp-socket-timeout": "YTDLP_SOCKET_TIMEOUT_SEC",
    "--socket-timeout": "YTDLP_SOCKET_TIMEOUT_SEC",
    "--worker-timeout": "WORKER_TIMEOUT_SEC",
    "--worker-timeout-sec": "WORKER_TIMEOUT_SEC",
    "--search-timeout": "WORKER_SEARCH_STEP_TIMEOUT_SEC",
    "--worker-search-step-timeout-sec": "WORKER_SEARCH_STEP_TIMEOUT_SEC",
    "--stall-timeout": "WORKER_STALL_TIMEOUT_SEC",
    "--worker-stall-timeout-sec": "WORKER_STALL_TIMEOUT_SEC",
    "--max-hard-timeout": "WORKER_MAX_HARD_TIMEOUT_SEC",
    "--worker-max-hard-timeout-sec": "WORKER_MAX_HARD_TIMEOUT_SEC",
    "--min-speed-kbps": "MIN_ACCEPTABLE_SPEED_KBPS",
    "--min-acceptable-speed-kbps": "MIN_ACCEPTABLE_SPEED_KBPS",
    "--fail-ttl-hours": "FAIL_TTL_HOURS",
    "--fail-ttl": "FAIL_TTL_HOURS",
    "--failed-cache-file": "FAILED_CACHE_FILE",
    "--meta-fail-ttl-hours": "META_FAIL_TTL_HOURS",
    "--meta-fail-ttl": "META_FAIL_TTL_HOURS",
    "--failed-meta-cache-file": "FAILED_META_CACHE_FILE",
    "--failed-meta-file": "FAILED_META_CACHE_FILE",
    "--time-format": "TIME_FORMAT",
    "--yt-cookie-file": "YT_COOKIE_FILE",
    "--cookie-file": "YT_COOKIE_FILE",
    "--pot-provider-url": "POT_PROVIDER_URL",
    "--azuracast-url": "AZURACAST_URL",
    "--azuracast-api-key": "AZURACAST_API_KEY",
    "--azuracast-station-id": "AZURACAST_STATION_ID",
    "--azuracast-playlist-id": "AZURACAST_PLAYLIST_ID",
    "--azuracast-playlist-name": "AZURACAST_PLAYLIST_NAME",
    "--azuracast-media-subdir": "AZURACAST_MEDIA_SUBDIR",
    "--log-level": "LOG_LEVEL",
    "--log-file": "LOG_FILE",
}

BOOLEAN_CLI_FLAGS_MAP: dict[str, tuple[str, str]] = {
    "--filter-unavailable": ("FILTER_UNAVAILABLE_SPOTIFY", "true"),
    "--no-filter-unavailable": ("FILTER_UNAVAILABLE_SPOTIFY", "false"),
    "--ignore-cached-urls": ("IGNORE_CACHED_URLS", "true"),
    "--no-ignore-cached-urls": ("IGNORE_CACHED_URLS", "false"),
    "--use-cached-urls": ("IGNORE_CACHED_URLS", "false"),
    "--allow-external": ("TRACK_ALLOW_EXTERNAL", "true"),
    "--no-allow-external": ("TRACK_ALLOW_EXTERNAL", "false"),
    "--sync-delete": ("SYNC_DELETE_REMOVED", "true"),
    "--no-sync-delete": ("SYNC_DELETE_REMOVED", "false"),
    "--sync-delete-ignored": ("SYNC_DELETE_IGNORED", "true"),
    "--no-sync-delete-ignored": ("SYNC_DELETE_IGNORED", "false"),
    "--normalize": ("AUDIO_NORMALIZE", "true"),
    "--no-normalize": ("AUDIO_NORMALIZE", "false"),
    "--trim-silence": ("AUDIO_TRIM_SILENCE", "true"),
    "--no-trim-silence": ("AUDIO_TRIM_SILENCE", "false"),
    "--fallback-search": ("ENABLE_FALLBACK_SEARCH", "true"),
    "--no-fallback-search": ("ENABLE_FALLBACK_SEARCH", "false"),
    "--direct-url-fallback": ("DIRECT_URL_FALLBACK", "true"),
    "--no-direct-url-fallback": ("DIRECT_URL_FALLBACK", "false"),
    "--show-parsed": ("LOG_SHOW_PARSED_TRACKS", "true"),
    "--no-show-parsed": ("LOG_SHOW_PARSED_TRACKS", "false"),
    "--show-scoring": ("LOG_SHOW_SCORING", "true"),
    "--no-show-scoring": ("LOG_SHOW_SCORING", "false"),
    "--show-skipped": ("LOG_SHOW_SKIPPED", "true"),
    "--no-show-skipped": ("LOG_SHOW_SKIPPED", "false"),
    "--show-quarantine": ("LOG_SHOW_QUARANTINE", "true"),
    "--no-show-quarantine": ("LOG_SHOW_QUARANTINE", "false"),
    "--download-progress": ("LOG_DOWNLOAD_PROGRESS", "true"),
    "--no-download-progress": ("LOG_DOWNLOAD_PROGRESS", "false"),
    "--colors": ("LOG_COLORS", "true"),
    "--no-colors": ("LOG_COLORS", "false"),
}


def parse_cli_arguments(raw_argv: list[str]) -> tuple[list[str], list[str]]:
    norm_args: list[str] = []
    track_queries: list[str] = []
    i = 0
    while i < len(raw_argv):
        raw_token = raw_argv[i]
        if raw_token in ("--", "—"):
            i += 1
            continue
        token = re.sub(r"^—+", "--", raw_token)

        if token in ("--track", "-t") and i + 1 < len(raw_argv):
            track_queries.append(raw_argv[i + 1])
            i += 2
            continue
        if token.startswith("--track="):
            track_queries.append(token.split("=", 1)[1])
            i += 1
            continue
        if token == "--env" and i + 1 < len(raw_argv):
            if "=" in (kv := raw_argv[i + 1]):
                ek, ev = kv.split("=", 1)
                apply_env_key_value_override(ek, ev)
            i += 2
            continue
        if token.startswith("--env="):
            if "=" in (kv := token.split("=", 1)[1]):
                ek, ev = kv.split("=", 1)
                apply_env_key_value_override(ek, ev)
            i += 1
            continue
        if "=" in token and token.startswith("--"):
            flag_part, val_part = token.split("=", 1)
            flag_low = flag_part.lower()
            apply_env_key_value_override(VALUE_CLI_FLAGS_MAP.get(flag_low, flag_low.lstrip("-")), val_part)
            i += 1
            continue

        token_low = token.lower()
        if token_low in VALUE_CLI_FLAGS_MAP and i + 1 < len(raw_argv):
            apply_env_key_value_override(VALUE_CLI_FLAGS_MAP[token_low], raw_argv[i + 1])
            i += 2
            continue

        norm_args.append(token_low)
        i += 1

    for arg in norm_args:
        if mapping := BOOLEAN_CLI_FLAGS_MAP.get(arg):
            apply_env_key_value_override(mapping[0], mapping[1])

    refresh_derived_config()
    return norm_args, track_queries


def install_docker_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    def _on_signal(sig_name: str) -> None:
        trigger_graceful_shutdown(sig_name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", signal.SIGTERM)):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except (NotImplementedError, RuntimeError, ValueError):
            signal.signal(sig, lambda s, _f: trigger_graceful_shutdown(signal.Signals(s).name))


async def async_main() -> None:
    norm_args, track_queries = parse_cli_arguments(sys.argv[1:])
    loop = asyncio.get_running_loop()
    install_docker_signal_handlers(loop)

    executor = ThreadPoolExecutor(max_workers=max(32, CONCURRENT_DOWNLOADS * 4))
    loop.set_default_executor(executor)

    force_loop = any(f in norm_args for f in ("--loop", "--daemon"))
    once_flag = any(f in norm_args for f in ("--once", "-1", "--dry-run")) and not force_loop
    ignore_quarantine_flag = any(f in norm_args for f in ("--ignore-quarantine", "--no-quarantine"))
    redownload_flag = any(f in norm_args for f in ("--redownload", "--force-dl"))
    rebuild_flag = "--rebuild" in norm_args
    dry_run_flag = "--dry-run" in norm_args

    try:
        if any(f in norm_args for f in ("--help", "-h")):
            print_cli_help()
        elif "--auth" in norm_args:
            run_interactive_auth()
        elif any(f in norm_args for f in ("--status", "--stats")):
            print_status_report()
        elif any(f in norm_args for f in ("--quarantine-list", "--quarantine")):
            print_quarantine_report()
        elif any(f in norm_args for f in ("--custom-tracks", "--spotitracks")):
            print_custom_tracks_report()
        elif "--retag" in norm_args:
            await run_retag_all_on_disk()
            if (force_loop or "--sync" in norm_args) and not is_shutting_down():
                await main_loop(once=not force_loop)
        elif "--azura-check" in norm_args:
            await check_azuracast_connectivity()
        elif "--azura-only" in norm_args:
            all_mp3 = [p.name for p in OUTPUT_DIR.glob("*.mp3") if p.stat().st_size > 50_000] if OUTPUT_DIR.exists() else []
            await sync_with_azuracast(all_mp3, 0)
        elif track_queries:
            await handle_single_track_cli(track_queries, allow_external=TRACK_ALLOW_EXTERNAL)
            if (force_loop or "--sync" in norm_args) and not is_shutting_down():
                await main_loop(once=not force_loop)
        elif rebuild_flag:
            await remove_cache_and_managed_files_async(wipe_all=False)
            if not is_shutting_down():
                await main_loop(
                    once=(not force_loop and "--once" in norm_args),
                    ignore_quarantine=True,
                    redownload_all=False,
                    dry_run=dry_run_flag,
                )
        elif "--remove-cache" in norm_args or "--all" in norm_args:
            await remove_cache_and_managed_files_async(wipe_all="--all" in norm_args)
            if ("--sync" in norm_args or force_loop) and not is_shutting_down():
                await main_loop(once=not force_loop)
        else:
            await main_loop(
                once=once_flag,
                ignore_quarantine=ignore_quarantine_flag,
                redownload_all=redownload_flag,
                dry_run=dry_run_flag,
            )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        cleanup_stale_staging_dirs()


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        cleanup_stale_staging_dirs()
        if is_shutting_down():
            token_ctx = current_ctx.set("DOCKER-STOP")
            try:
                logger.info(
                    f"Контейнер SpotiSync корректно остановлен ({shutdown_signal_name or 'SIGINT'}). "
                    f"Временные папки очищены."
                )
            finally:
                current_ctx.reset(token_ctx)
        logging.shutdown()
