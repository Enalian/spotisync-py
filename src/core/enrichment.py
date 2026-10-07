import asyncio
import re
from typing import Tuple

import httpx

from src.core.logger import logger
from src.core.models import TrackMeta
from src.core.scoring import score_candidate


class MetadataEnricher:
    def __init__(self, http_client: httpx.AsyncClient):
        self.client = http_client
        self.dz_sem = asyncio.Semaphore(5)
        self.mb_sem = asyncio.Semaphore(1)
        self.headers = {
            "User-Agent": "SpotiSyncRadio/7.3.19 ( https://github.com/spotisync )"
        }

    async def enrich_via_deezer(self, tr: TrackMeta) -> Tuple[bool, str]:
        async with self.dz_sem:
            await asyncio.sleep(0.15)
            last_err = ""
            for retry in range(4):
                try:
                    q_strict = f'artist:"{tr.artist}" track:"{tr.clean_title}"'
                    r = await self.client.get(
                        "https://api.deezer.com/search",
                        params={"q": q_strict, "limit": 5},
                    )

                    if r.status_code == 200:
                        rj = r.json()
                        if isinstance(rj, dict) and "error" in rj:
                            if rj["error"].get("code") == 4:  # Quota limit
                                await asyncio.sleep(1.5 * (retry + 1))
                                continue
                            return False, f"Deezer error: {rj['error'].get('message')}"

                        data = list(rj.get("data") or [])
                        if not data:
                            return False, "Нет в каталоге Deezer"

                        # Берем первый трек и запрашиваем его карточку для ISRC
                        best_cand = data[0]
                        cand_id = best_cand.get("id")

                        tj_r = await self.client.get(
                            f"https://api.deezer.com/track/{cand_id}"
                        )
                        if tj_r.status_code == 200:
                            tj = tj_r.json()
                            if not tr.isrc and tj.get("isrc"):
                                tr.isrc = str(tj["isrc"]).strip()
                            rel_d = tj.get("release_date") or (
                                tj.get("album") or {}
                            ).get("release_date")
                            if (
                                not tr.release_date
                                and rel_d
                                and str(rel_d) != "0000-00-00"
                            ):
                                tr.release_date = str(rel_d).strip()
                            return True, "OK"

                    elif r.status_code == 429:
                        await asyncio.sleep(2.0 * (retry + 1))
                        continue
                    else:
                        return False, f"HTTP {r.status_code}"
                except Exception as e:
                    last_err = f"Ошибка сети ({type(e).__name__})"
                    await asyncio.sleep(0.5)

            return False, last_err or "Таймаут Deezer"

    async def enrich_via_musicbrainz(self, tr: TrackMeta) -> Tuple[bool, str]:
        async with self.mb_sem:
            for retry in range(3):
                try:
                    await asyncio.sleep(1.2 * (retry + 1))
                    safe_art = re.sub(
                        r'["\\+\-&|!(){}\[\]^~*?:\/]', " ", tr.artist
                    ).strip()
                    safe_tit = re.sub(
                        r'["\\+\-&|!(){}\[\]^~*?:\/]', " ", tr.base_title
                    ).strip()
                    query = f'artist:"{safe_art}" AND recording:"{safe_tit}"'

                    r = await self.client.get(
                        "https://musicbrainz.org/ws/2/recording",
                        params={"query": query, "fmt": "json", "limit": 3},
                        headers=self.headers,
                    )

                    # Защита от лимитов (HTTP 429 / 503)
                    if r.status_code in (429, 503):
                        logger.debug(
                            f"[MusicBrainz] Лимит API (HTTP {r.status_code}), ретрай {retry + 1}/3..."
                        )
                        await asyncio.sleep(3.0 * (retry + 1))
                        continue

                    if r.status_code != 200:
                        return False, f"HTTP {r.status_code}"

                    recordings = r.json().get("recordings") or []
                    if not recordings:
                        return False, "0 записей в MusicBrainz"

                    for rec in recordings:
                        if isrc_list := rec.get("isrcs"):
                            if not tr.isrc:
                                first_isrc = isrc_list[0]
                                tr.isrc = str(
                                    first_isrc.get("id")
                                    if isinstance(first_isrc, dict)
                                    else first_isrc
                                ).strip()
                        if first_rel := str(rec.get("first-release-date") or "")[:10]:
                            if not tr.release_date:
                                tr.release_date = first_rel
                        if tr.isrc:
                            return True, "OK"

                    return False, "Записи найдены, но ISRC отсутствует"
                except Exception as e:
                    return False, f"Ошибка MB: {e}"
            return False, "Превышены ретраи MusicBrainz"
