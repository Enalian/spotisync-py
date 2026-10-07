import hashlib
import json
import re

import httpx

from src.core.config import settings
from src.core.logger import logger
from src.core.models import TrackMeta

BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"


class SpotifyClient:
    def __init__(self, http_client: httpx.AsyncClient):
        self.client = http_client

    async def get_client_token(self, web_client_id: str | None = None) -> str | None:
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
            r = await self.client.post(
                "https://clienttoken.spotify.com/v1/clienttoken",
                json=payload,
                headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            )
            if r.status_code == 200 and (
                tok := r.json().get("granted_token", {}).get("token")
            ):
                logger.info("Получен client-token для GraphQL Pathfinder.")
                return str(tok)
        except Exception as e:
            logger.debug(f"Не удалось получить client-token: {e}")
        return None

    async def discover_dynamic_graphql_hashes(self, playlist_id: str) -> list[str]:
        discovered: list[str] = []
        try:
            r = await self.client.get(
                f"https://open.spotify.com/playlist/{playlist_id}",
                headers={"User-Agent": BROWSER_UA},
                follow_redirects=True,
            )
            if r.status_code != 200:
                return discovered
            js_urls = re.findall(
                r'src="(https://[^"]+spotifycdn\.com/cdn/build/web-player/[^"]+\.js)"',
                r.text,
            )
            for js_url in js_urls[:8]:
                try:
                    jr = await self.client.get(
                        js_url, headers={"User-Agent": BROWSER_UA}
                    )
                    if jr.status_code == 200 and "fetchPlaylist" in jr.text:
                        matches = re.findall(
                            r'"fetchPlaylist"[^}]{0,140}"([a-f0-9]{64})"|"([a-f0-9]{64})"[^}]{0,140}"fetchPlaylist"',
                            jr.text,
                        )
                        for m1, m2 in matches:
                            if h := m1 or m2:
                                if h not in discovered:
                                    discovered.append(h)
                                    logger.info(
                                        f"Найден актуальный sha256Hash: {h[:16]}..."
                                    )
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"Ошибка сканирования JS-бандлов: {e}")
        return discovered

    async def fetch_embed_session_and_preview(
        self, playlist_id: str
    ) -> tuple[list[TrackMeta], str | None, str | None, int, int]:
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        logger.info(f"Получаем гостевую сессию Веб-плеера через: {embed_url}")
        resp = await self.client.get(embed_url, headers={"User-Agent": BROWSER_UA})
        resp.raise_for_status()

        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
            resp.text,
            re.DOTALL,
        )
        if not match:
            return [], None, None, 0, 0

        state = (
            json.loads(match.group(1))
            .get("props", {})
            .get("pageProps", {})
            .get("state", {})
        )
        session_obj = state.get("settings", {}).get("session", {})
        embed_access_token = session_obj.get("accessToken")
        embed_client_id = session_obj.get("clientId")

        if not embed_access_token and (
            m_tok := re.search(r'"accessToken"\s*:\s*"([^"]+)"', resp.text)
        ):
            embed_access_token = m_tok.group(1)
        if not embed_client_id and (
            m_cid := re.search(r'"clientId"\s*:\s*"([a-f0-9]{32})"', resp.text)
        ):
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
            if settings.filter_unavailable_spotify and (
                not uri.startswith("spotify:track:") or item.get("isPlayable") is False
            ):
                filtered_out += 1
                continue
            sp_id = uri.split(":")[-1] if ":" in uri else ""
            if len(sp_id) != 22:
                filtered_out += 1
                continue

            title = (item.get("title") or "").strip()
            subtitle = (item.get("subtitle") or "").replace("\xa0", " ").strip()
            duration_sec = int(item.get("duration") or 0) // 1000

            if settings.filter_unavailable_spotify and (
                not title
                or not subtitle
                or subtitle.lower() == "unknown"
                or duration_sec <= 0
            ):
                filtered_out += 1
                continue

            artists_all = [a.strip() for a in subtitle.split(",") if a.strip()] or [
                "Unknown"
            ]
            tracks.append(
                TrackMeta(
                    spotify_id=sp_id,
                    title=title,
                    artist=artists_all[0],
                    artists_all=artists_all,
                    album=entity.get("name") or "Spotify Playlist",
                    album_artist=artists_all[0],
                    track_number=str(idx),
                    duration_sec=duration_sec,
                    cover_url=default_cover,
                )
            )

        return tracks, embed_access_token, embed_client_id, raw_count, filtered_out
