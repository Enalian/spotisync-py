import math
import re

from pydantic import BaseModel, Field

from src.core.config import settings


def format_duration(seconds: float | int) -> str:
    total_sec = math.ceil(max(0.0, float(seconds)))
    fmt = settings.time_format or "%hч %mмин %sсек"
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
    parts = []
    for m in tokens:
        unit, suffix = m.group(1), m.group(2).rstrip()
        val = values[unit]
        if val > 0 or (unit == "s" and total_sec == 0 and not parts):
            parts.append(f"{val}{suffix}")

    if not parts:
        parts.append(f"0{tokens[-1].group(2).rstrip()}")

    return " ".join(parts)


class TrackMeta(BaseModel):
    spotify_id: str
    title: str
    artist: str
    artists_all: list[str] = Field(default_factory=list)
    album: str = "Single"
    album_artist: str = "Unknown"
    release_date: str = ""
    track_number: str = "1"
    disc_number: str = "1"
    duration_sec: int = 0
    isrc: str | None = None
    cover_url: str | None = None
    direct_url: str | None = None

    @property
    def base_title(self) -> str:
        cleaned = re.sub(r"[\(\[].*?[\)\]]", "", self.title)
        cleaned = re.sub(r"\s+-\s+.*$", "", cleaned)
        return cleaned.strip() or self.title

    @property
    def clean_title(self) -> str:
        keep_kw = (
            "remix",
            "rmx",
            "slow",
            "sped",
            "speed",
            "nightcore",
            "instrumental",
            "extended",
            "vip",
            "mix",
            "edit",
            "version",
            "ver",
            "japanese",
            "russian",
            "acoustic",
            "live",
            "cover",
            "ost",
            "soundtrack",
            "theme",
            "vision",
            "flip",
        )

        def filter_brackets(m: re.Match) -> str:
            content = m.group(0)
            low = content.lower()
            if any(k in low for k in ("feat.", "ft.", "prod.", "produced by", "with ")):
                if not any(kw in low for kw in keep_kw):
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
        return "AUTO" if self.duration_sec <= 0 else format_duration(self.duration_sec)

    @property
    def release_year(self) -> str:
        return self.release_date[:4] if len(self.release_date) >= 4 else "N/A"

    @property
    def has_full_meta(self) -> bool:
        return bool(self.isrc and self.release_year != "N/A")
