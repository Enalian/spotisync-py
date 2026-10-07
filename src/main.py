import asyncio
import shutil
import signal
import sys
import time
from pathlib import Path

import httpx
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3, TSRC, ID3NoHeaderError

from src.clients.azuracast import AzuraCastClient
from src.clients.spotify import SpotifyClient
from src.core.config import settings
from src.core.downloader import AdaptiveWorkerWatchdog, TrackDownloader
from src.core.enrichment import MetadataEnricher
from src.core.logger import (
    is_shutting_down,
    logger,
    setup_logger,
    trigger_graceful_shutdown,
)
from src.core.models import TrackMeta


def tag_mp3_file(file_path: Path, meta: TrackMeta) -> None:
    try:
        audio = EasyID3(file_path)
    except ID3NoHeaderError:
        ID3().save(file_path, v2_version=3)
        audio = EasyID3(file_path)

    audio["title"] = meta.title
    audio["artist"] = ", ".join(meta.artists_all)
    audio["album"] = meta.album
    if meta.release_date:
        audio["date"] = meta.release_date
    audio.save(file_path, v2_version=3)

    raw_id3 = ID3(file_path)
    if meta.isrc:
        raw_id3.delall("TSRC")
        raw_id3.add(TSRC(encoding=3, text=meta.isrc))
    raw_id3.save(file_path, v2_version=3)


def cleanup_stale_staging_dirs() -> None:
    if settings.staging_dir.exists():
        now = time.time()
        for d in settings.staging_dir.glob("sp_*"):
            if d.is_dir() and (now - d.stat().st_mtime) > 7200:
                shutil.rmtree(d, ignore_errors=True)


async def download_worker(idx: int, tr: TrackMeta, downloader: TrackDownloader) -> bool:
    """Изолированный воркер для скачивания (возвращает True если скачано)"""
    if is_shutting_down():
        return False

    mp3_path = settings.output_dir / tr.id_filename
    if mp3_path.exists() and mp3_path.stat().st_size > 50_000:
        logger.debug(f"[SKIP] Уже на диске: {tr.display_name}")
        return True

    logger.info(f"[{idx}] Скачиваем: {tr.display_name} (ISRC: {tr.isrc})")
    watchdog = AdaptiveWorkerWatchdog(settings.worker_timeout_sec)

    query = f"ytsearch3:{tr.artist} - {tr.title}"
    try:
        entries = await asyncio.to_thread(
            downloader.extract_info_sync, query, 0, settings.yt_cookie_file
        )

        if entries and entries[0].get("webpage_url"):
            url = entries[0]["webpage_url"]
            temp_dir = settings.staging_dir / f"sp_{tr.safe_id}"
            temp_dir.mkdir(parents=True, exist_ok=True)
            out_base = temp_dir / "track"

            staged_mp3, err = await asyncio.to_thread(
                downloader.download_sync,
                url,
                out_base,
                0,
                settings.yt_cookie_file,
                watchdog,
            )

            if staged_mp3:
                await asyncio.to_thread(tag_mp3_file, staged_mp3, tr)
                shutil.move(staged_mp3, mp3_path)
                logger.success(f"Готово: {tr.display_name} -> {mp3_path.name}")
                shutil.rmtree(temp_dir, ignore_errors=True)
                return True
            else:
                logger.error(f"Ошибка скачивания {tr.display_name}: {err}")
                shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as e:
        logger.error(f"Сбой воркера {tr.display_name}: {e}")

    return False


async def run_sync_cycle(http_client: httpx.AsyncClient):
    cycle_start = time.monotonic()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_staging_dirs()

    sp_client = SpotifyClient(http_client)
    az_client = AzuraCastClient(http_client)
    enricher = MetadataEnricher(http_client)
    downloader = TrackDownloader()

    if not settings.playlist_url:
        logger.error("PLAYLIST_URL не задан! Завершение.")
        return

    logger.info(
        f"=== Старт цикла синхронизации | Плейлист: {settings.playlist_url} ==="
    )

    tracks, _, _, raw_total, filtered = await sp_client.fetch_embed_session_and_preview(
        settings.playlist_url
    )
    if not tracks:
        logger.error("Spotify не вернул треки.")
        return

    # Предварительное обогащение
    for tr in tracks:
        if not tr.has_full_meta:
            await enricher.enrich_via_deezer(tr)
            if not tr.isrc:
                await enricher.enrich_via_musicbrainz(tr)

    expected_files = [tr.id_filename for tr in tracks]

    # Современный конкурентный запуск через TaskGroup (Python 3.11+)
    tasks = []
    async with asyncio.TaskGroup() as tg:
        for idx, tr in enumerate(tracks, 1):
            tasks.append(tg.create_task(download_worker(idx, tr, downloader)))

    # Считаем успешные скачивания
    downloaded_ok = sum(1 for t in tasks if t.result() is True)

    if az_client.is_configured() and downloaded_ok > 0 and not is_shutting_down():
        await az_client.sync_playlist(expected_files, downloaded_ok)

    logger.info(
        f"=== Итог цикла: Заняло {time.monotonic() - cycle_start:.1f} сек. Скачано/на диске: {downloaded_ok} ==="
    )


async def async_main():
    clear_logs = "--clear-logs" in sys.argv
    exit_immediately = "--exit" in sys.argv
    once = "--once" in sys.argv

    setup_logger(clear_logs=clear_logs)

    if exit_immediately:
        logger.info("Флаг --exit обнаружен. Завершение работы.")
        return

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: trigger_graceful_shutdown(s.name))

    async with httpx.AsyncClient(timeout=20.0) as client:
        while not is_shutting_down():
            try:
                await run_sync_cycle(client)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Критическая ошибка цикла: {e}")

            if once or settings.sync_interval_hours <= 0 or is_shutting_down():
                break

            sleep_sec = settings.sync_interval_hours * 3600
            logger.info(
                f"Сон на {settings.sync_interval_hours} ч. до следующего цикла..."
            )

            end_t = time.monotonic() + sleep_sec
            while time.monotonic() < end_t and not is_shutting_down():
                await asyncio.sleep(1.0)


if __name__ == "__main__":
    try:
        # В Python 3.14 asyncio.run - это де-факто стандарт (уже не нужен uvloop для базовых задач)
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass
    finally:
        cleanup_stale_staging_dirs()
        logging.shutdown()
