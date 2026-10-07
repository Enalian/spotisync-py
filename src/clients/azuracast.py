import asyncio
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import httpx

from src.core.config import settings
from src.core.logger import logger


class AzuraCastClient:
    def __init__(self, http_client: httpx.AsyncClient):
        self.client = http_client
        self._cached_station_id: str = ""
        self._cached_playlist_id: int = 0
        self._cached_playlist_name: str = ""

    def is_configured(self) -> bool:
        """Проверяет минимальные требования для работы с API."""
        if not (
            settings.azuracast_url
            and settings.azuracast_api_key
            and settings.azuracast_station_id
        ):
            return False
        if not (settings.azuracast_playlist_id or settings.azuracast_playlist_name):
            return False
        if not settings.azuracast_url.startswith(("http://", "https://")):
            return False
        return len(settings.azuracast_api_key) >= 8

    def _headers(self) -> Dict[str, str]:
        return {
            "X-API-Key": settings.azuracast_api_key,
            "Accept": "application/json",
            "User-Agent": "SpotiSync/7.3",
        }

    async def resolve_ids(self) -> bool:
        """Разрешает текстовые имена в числовые ID станции и плейлиста (с кэшированием)."""
        if self._cached_station_id and self._cached_playlist_id:
            return True

        url_st = f"{settings.azuracast_url}/api/stations"
        raw_st = settings.azuracast_station_id.strip()
        try:
            r = await self.client.get(url_st, headers=self._headers())
            if r.status_code == 200 and isinstance(stations := r.json(), list):
                raw_low = raw_st.lower()
                for st in stations:
                    if raw_st == str(st.get("id")) or raw_low in (
                        str(st.get("short_name", "")).lower(),
                        str(st.get("name", "")).lower(),
                    ):
                        self._cached_station_id = str(st.get("id"))
                        break
        except Exception as e:
            logger.warning(f"Ошибка резолва станции AzuraCast: {e}")
            return False

        if not self._cached_station_id:
            self._cached_station_id = raw_st

        # Резолв плейлиста
        url_pl = (
            f"{settings.azuracast_url}/api/station/{self._cached_station_id}/playlists"
        )
        target_id = (
            int(settings.azuracast_playlist_id)
            if settings.azuracast_playlist_id.isdigit()
            else 0
        )
        target_name = settings.azuracast_playlist_name.lower().strip()

        try:
            r = await self.client.get(url_pl, headers=self._headers())
            if r.status_code == 200 and isinstance(pls := r.json(), list):
                for pl in pls:
                    if (target_id and pl.get("id") == target_id) or (
                        target_name
                        and pl.get("name", "").lower().strip() == target_name
                    ):
                        self._cached_playlist_id = int(pl["id"])
                        self._cached_playlist_name = pl.get(
                            "name", str(self._cached_playlist_id)
                        )
                        return True
        except Exception as e:
            logger.warning(f"Ошибка резолва плейлиста AzuraCast: {e}")
        return False

    async def get_all_files(self) -> List[Dict[str, Any]]:
        """Получает полный список файлов станции с ПАГИНАЦИЕЙ."""
        if not await self.resolve_ids():
            return []

        all_files = []
        current_page = 1
        has_more = True

        logger.debug(f"[AZURACAST] Запрашиваем файлы станции (Пагинация)...")
        while has_more:
            try:
                # Пагинация через rowCount и current
                r = await self.client.get(
                    f"{settings.azuracast_url}/api/station/{self._cached_station_id}/files",
                    headers=self._headers(),
                    params={"rowCount": 1000, "current": current_page},
                )
                if r.status_code != 200:
                    logger.warning(
                        f"Ошибка пагинации файлов (HTTP {r.status_code}): {r.text[:150]}"
                    )
                    break

                data = r.json()
                # Azuracast возвращает dict {"rows": [...]} при использовании параметров пагинации
                if isinstance(data, dict) and "rows" in data:
                    items = data["rows"]
                    total = data.get("total", len(items))
                elif isinstance(data, list):  # Legacy fallback
                    items = data
                    total = len(items)
                else:
                    break

                all_files.extend(items)

                # Проверяем, есть ли еще страницы
                if not items or len(items) < 1000 or len(all_files) >= total:
                    has_more = False
                else:
                    current_page += 1
            except Exception as e:
                logger.error(f"Сбой загрузки списка файлов Azuracast: {e}")
                break

        return all_files

    async def clear_queue(self) -> None:
        """Сброс очереди AutoDJ."""
        if not self._cached_station_id:
            return
        try:
            r = await self.client.post(
                f"{settings.azuracast_url}/api/station/{self._cached_station_id}/queue/clear",
                headers=self._headers(),
            )
            if r.status_code in (200, 204):
                logger.info(f"[AZURACAST] Очередь AutoDJ сброшена.")
        except Exception:
            pass

    async def sync_playlist(
        self, expected_filenames: List[str], new_downloads_count: int
    ) -> None:
        """Интеллектуальная синхронизация скачанных файлов с плейлистом AzuraCast."""
        if not expected_filenames or not self.is_configured():
            return

        if not await self.resolve_ids():
            logger.error(
                "AzuraCast: Невозможно выполнить синхронизацию (не найден плейлист/станция)."
            )
            return

        logger.info(
            f"Синхронизация со станцией #{self._cached_station_id}, плейлист '{self._cached_playlist_name}'."
        )
        expected_set = set(expected_filenames)
        max_poll_attempts = 12 if new_downloads_count > 0 else 3

        station_files = []
        for poll_idx in range(1, max_poll_attempts + 1):
            if poll_idx == 1 and new_downloads_count > 0:
                # Пинок кэша (Reprocess)
                try:
                    await self.client.get(
                        f"{settings.azuracast_url}/api/station/{self._cached_station_id}/files/list",
                        headers=self._headers(),
                        params={"internal": "true", "flushCache": "true"},
                    )
                    await self.client.put(
                        f"{settings.azuracast_url}/api/station/{self._cached_station_id}/files/batch",
                        headers=self._headers(),
                        json={
                            "do": "reprocess",
                            "files": [],
                            "directories": [settings.azuracast_media_subdir]
                            if settings.azuracast_media_subdir
                            else [""],
                        },
                    )
                except Exception:
                    pass
                await asyncio.sleep(3.0)

            station_files = await self.get_all_files()

            indexed_names = set()
            for f_obj in station_files:
                rel_path = str(f_obj.get("path") or "")
                if settings.azuracast_media_subdir and not rel_path.startswith(
                    f"{settings.azuracast_media_subdir}/"
                ):
                    continue
                fname = Path(rel_path).name
                if fname in expected_set:
                    indexed_names.add(fname)

            missing_in_azura = expected_set - indexed_names
            if not missing_in_azura or poll_idx == max_poll_attempts:
                if missing_in_azura:
                    logger.warning(
                        f"[AZURACAST] {len(missing_in_azura)} файлов еще не проиндексированы БД радиостанции."
                    )
                break

            logger.info(
                f"[AZURACAST WAIT {poll_idx}/{max_poll_attempts}] Ожидание индексации файлов (осталось {len(missing_in_azura)})... Пауза 10с"
            )
            await asyncio.sleep(10.0)

        # Подготовка батчей для добавления в плейлист
        playlist_groups = {}
        already_assigned = 0

        for f_obj in station_files:
            rel_path = str(f_obj.get("path") or "")
            if settings.azuracast_media_subdir and not rel_path.startswith(
                f"{settings.azuracast_media_subdir}/"
            ):
                continue
            if Path(rel_path).name not in expected_set:
                continue

            current_pls = {
                int(p["id"]) if isinstance(p, dict) else int(p)
                for p in (f_obj.get("playlists") or [])
            }
            if self._cached_playlist_id in current_pls:
                already_assigned += 1
            else:
                target_pls = tuple(sorted(current_pls | {self._cached_playlist_id}))
                playlist_groups.setdefault(target_pls, []).append(rel_path)

        if not playlist_groups:
            logger.success(
                f"Все проиндексированные треки ({already_assigned}/{len(expected_set)}) уже в плейлисте AzuraCast!"
            )
            return

        total_added = 0
        for pl_tuple, file_paths in playlist_groups.items():
            for i in range(0, len(file_paths), 100):
                chunk = file_paths[i : i + 100]
                try:
                    r = await self.client.put(
                        f"{settings.azuracast_url}/api/station/{self._cached_station_id}/files/batch",
                        headers=self._headers(),
                        json={
                            "do": "playlist",
                            "playlists": list(pl_tuple),
                            "files": chunk,
                        },
                    )
                    if r.status_code in (200, 204):
                        total_added += len(chunk)
                except Exception as e:
                    logger.error(f"Ошибка привязки треков: {e}")

        if total_added > 0:
            logger.success(
                f"В плейлист AzuraCast добавлено {total_added} треков (всего: {already_assigned + total_added})."
            )
            await self.clear_queue()
