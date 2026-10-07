from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Pydantic автоматически прочитает переменные из .env и окружения ОС
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Конфигурация Spotify и Основные настройки ---
    playlist_url: str = Field(default="")
    spotify_market: str = Field(default="US")
    sync_interval_hours: float = Field(
        default=24.0, ge=1.0
    )  # ge=1.0 означает "не меньше 1"

    # --- Пути и Файлы ---
    output_dir: Path = Field(default=Path("/music"))
    staging_dir: Path = Field(default=Path("/tmp/spotisync_staging"))
    cache_filename: str = Field(default=".spotisync.json")
    custom_tracks_filename: str = Field(default=".spotitracks.json")
    failed_cache_file: Path = Field(default=Path("/app/data/failed_tracks.json"))
    failed_meta_cache_file: Path = Field(default=Path("/app/data/failed_metadata.json"))
    yt_cookie_file: Path = Field(default=Path("/app/data/cookies.txt"))
    pot_provider_url: str = Field(default="")

    # --- Права доступа (Docker) ---
    puid: int = Field(default=1000)
    pgid: int = Field(default=1000)

    # --- Логика и Поведение ---
    filter_unavailable_spotify: bool = Field(default=True)
    ignore_cached_urls: bool = Field(default=True)
    track_allow_external: bool = Field(default=False)
    sync_delete_removed: bool = Field(default=False)
    sync_delete_ignored: bool = Field(default=True)
    enable_fallback_search: bool = Field(default=True)
    direct_url_fallback: bool = Field(default=True)

    # --- Обработка Аудио (FFmpeg) ---
    audio_normalize: bool = Field(default=True)
    audio_target_lufs: float = Field(default=-14.0)
    audio_trim_silence: bool = Field(default=True)

    # --- Таймауты и Производительность ---
    concurrent_downloads: int = Field(default=5, ge=1)
    ytdlp_retries: int = Field(default=3, ge=1)
    ytdlp_socket_timeout_sec: int = Field(default=30, ge=5)
    worker_timeout_sec: float = Field(default=360.0, ge=30.0)
    worker_search_step_timeout_sec: float = Field(default=180.0, ge=15.0)
    worker_stall_timeout_sec: float = Field(default=120.0, ge=15.0)
    worker_max_hard_timeout_sec: float = Field(default=1500.0, ge=60.0)
    min_acceptable_speed_kbps: float = Field(default=10.0, ge=1.0)
    fail_ttl_hours: float = Field(default=72.0, ge=0.0)
    meta_fail_ttl_hours: float = Field(default=24.0, ge=0.0)

    # --- AzuraCast ---
    azuracast_url: str = Field(default="")
    azuracast_api_key: str = Field(default="")
    azuracast_station_id: str = Field(default="")
    azuracast_playlist_id: str = Field(default="")
    azuracast_playlist_name: str = Field(default="")
    azuracast_media_subdir: str = Field(default="")

    # --- Логирование ---
    log_level: str = Field(default="INFO")
    log_file: str = Field(default="/app/data/spotisync.log")
    time_format: str = Field(default="%hч %mмин %sсек")
    log_colors: bool = Field(default=True)
    enable_logs: bool = Field(default=True)
    log_quiet: bool = Field(default=False)
    log_max_size_mb: int = Field(default=15)
    log_backup_count: int = Field(default=5)


# Создаем глобальный объект настроек. Pydantic проверит всё при старте скрипта.
settings = Settings()
