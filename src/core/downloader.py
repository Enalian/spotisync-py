import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yt_dlp

from src.core.config import settings
from src.core.logger import is_shutting_down, logger
from src.core.models import TrackMeta

# Урезанный набор клиентов (без iOS, который сейчас сбоит у многих)
YT_CLIENT_PROFILES = (
    {"clients": ["mweb", "web"], "use_cookies": True},
    {"clients": ["tv_embedded", "mweb"], "use_cookies": True},
    {"clients": ["android_vr", "mweb"], "use_cookies": False},
)


class YtdlpLoggerAdapter:
    """Адаптер для перехвата логов yt-dlp в наш кастомный логгер"""

    def __init__(self):
        self.last_error_msg = ""
        self.last_warning_msg = ""

    def debug(self, msg: str):
        if not is_shutting_down():
            logger.trace(msg if msg.startswith("[debug] ") else f"[yt-dlp] {msg}")

    def info(self, msg: str):
        if not is_shutting_down():
            logger.trace(f"[yt-dlp] {msg}")

    def warning(self, msg: str):
        if is_shutting_down():
            return
        self.last_warning_msg = msg
        if "DRM protected" in msg or "Requested format is not available" in msg:
            logger.debug(f"[yt-dlp skip] {msg}")
        else:
            logger.debug(f"[yt-dlp warn] {msg}")

    def error(self, msg: str):
        if is_shutting_down():
            return
        self.last_error_msg = msg
        if "DRM protected" in msg or "Requested format is not available" in msg:
            logger.debug(f"[yt-dlp format-skip] {msg}")
        elif "Sign in to confirm your age" in msg:
            logger.debug(f"[yt-dlp age-gate] {msg}")
        else:
            logger.warning(f"[yt-dlp err] {msg}")


class AdaptiveWorkerWatchdog:
    """Сторожевой таймер для отсечения повисших сокетов yt-dlp"""

    def __init__(self, base_timeout: float):
        self._lock = threading.Lock()
        now = time.monotonic()
        self.deadline = now + base_timeout
        self.last_activity = now
        self.hard_cancelled = False
        self.abort_reason = ""
        self.stage = "init"

    def check_expired(self) -> Tuple[bool, str]:
        with self._lock:
            if is_shutting_down():
                return True, "Остановка контейнера"
            if self.hard_cancelled:
                return True, self.abort_reason
            now = time.monotonic()
            if (
                self.stage == "download"
                and (now - self.last_activity) > settings.worker_stall_timeout_sec
            ):
                return True, "Зависание скачивания (нет данных)"
            if now > self.deadline:
                return True, f"Таймаут этапа '{self.stage}'"
            return False, ""

    def cancel(self, reason: str):
        with self._lock:
            self.hard_cancelled = True
            self.abort_reason = reason

    def update_activity(self):
        with self._lock:
            self.last_activity = time.monotonic()


class TrackDownloader:
    def __init__(self):
        self.ffmpeg_args = self._build_ffmpeg_args()

    def _build_ffmpeg_args(self) -> list:
        filters = []
        if settings.audio_trim_silence:
            filters.append(
                "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-72dB:start_silence=0.25:stop_periods=1:stop_duration=3.0:stop_threshold=-72dB:stop_silence=0.5"
            )
        if settings.audio_normalize:
            filters.append(f"loudnorm=I={settings.audio_target_lufs}:TP=-1.5:LRA=11")

        args = ["-ar", "44100", "-ac", "2"]
        if filters:
            args.extend(["-af", ",".join(filters)])
        return args

    def _build_opts(
        self, attempt: int, isolated_cookie: Optional[Path], for_search: bool
    ) -> Dict[str, Any]:
        profile = YT_CLIENT_PROFILES[attempt % len(YT_CLIENT_PROFILES)]
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "force_ipv4": True,
            "socket_timeout": settings.ytdlp_socket_timeout_sec,
            "retries": settings.ytdlp_retries,
            "extractor_args": {"youtube": {"player_client": profile["clients"]}},
            "logger": YtdlpLoggerAdapter(),
        }

        if settings.pot_provider_url:
            opts["extractor_args"]["youtubepot-bgutilhttp"] = {
                "base_url": [settings.pot_provider_url]
            }

        if for_search:
            opts["ignore_no_formats_error"] = True
            opts["skip_download"] = True

        has_cookie = isolated_cookie and isolated_cookie.exists()
        if profile["use_cookies"] and has_cookie:
            opts["cookiefile"] = str(isolated_cookie)

        return opts

    def extract_info_sync(
        self, query: str, attempt: int, isolated_cookie: Optional[Path]
    ) -> list:
        """Синхронный парсинг инфы (запускается в пуле потоков)"""
        opts = self._build_opts(attempt, isolated_cookie, for_search=True)
        opts["extract_flat"] = False
        opts["ignoreerrors"] = True

        with yt_dlp.YoutubeDL(opts) as ydl:
            if attempt > 0:
                ydl.cache.remove()
            info = ydl.extract_info(query, download=False)
            return [
                e
                for e in (info.get("entries", []) if info else [])
                if e and not e.get("drm")
            ]

    def download_sync(
        self,
        url: str,
        out_base: Path,
        attempt: int,
        isolated_cookie: Optional[Path],
        watchdog: AdaptiveWorkerWatchdog,
    ) -> Tuple[Optional[Path], str]:
        """Синхронное скачивание (запускается в пуле потоков)"""
        opts = self._build_opts(attempt, isolated_cookie, for_search=False)

        def progress_hook(d):
            watchdog.update_activity()

        opts.update(
            {
                "format": "bestaudio/best",
                "outtmpl": f"{out_base}.%(ext)s",
                "progress_hooks": [progress_hook],
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    }
                ],
                "postprocessor_args": {"extractaudio": self.ffmpeg_args},
            }
        )

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                if attempt > 0:
                    ydl.cache.remove()
                ydl.extract_info(url, download=True)

            mp3_file = Path(f"{out_base}.mp3")
            if mp3_file.exists() and mp3_file.stat().st_size > 50_000:
                return mp3_file, ""
            return None, "Файл не создан или слишком мал"
        except Exception as e:
            return None, str(e)
