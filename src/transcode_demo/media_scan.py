"""Directory crawl for camera media files.

Lifted from the enrichment project's render.py MEDIA_EXTS + iter_media: walk a
tree, skip macOS AppleDouble stubs, keep things FLAPI can decode (including
camera-raw codecs ffmpeg can't read). ``crawl`` yields each hit as it is found
so the UI can pop thumbnails in during the walk.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

# FLAPI can discover camera-raw codecs ffmpeg can't; keep the superset.
MEDIA_EXTS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mxf", ".ts", ".mts",
    ".m2ts", ".wmv", ".flv", ".mpg", ".mpeg", ".ari", ".arx", ".arri", ".r3d",
    ".braw", ".cine", ".dng",
    ".crm", ".rmf",          # Canon Cinema RAW Light / RAW
    ".crm", ".cr2", ".cr3",  # Canon
    ".raf", ".nef", ".arw",  # Fuji / Nikon / Sony stills-raw (single-frame)
}


# Map a flapi codec string (or fallback extension) to a friendly camera/format
# label and whether it's camera RAW that needs debayering.
_CODEC_INFO = [
    ("arriraw", "ARRIRAW", True),
    ("r3d", "REDCODE RAW", True),
    ("braw", "Blackmagic RAW", True),
    ("canonraw", "Canon Cinema RAW Light", True),
    ("sonyraw", "Sony RAW / X-OCN", True),
    ("sonyxocn", "Sony X-OCN", True),
    ("xocn", "Sony X-OCN", True),
    ("dng", "CinemaDNG", True),
    ("prores", "Apple ProRes", False),
    ("xavc", "Sony XAVC", False),
    ("dnxhd", "Avid DNxHD", False),
    ("dnx", "Avid DNx", False),
    ("hevc", "HEVC", False),
    ("h264", "H.264", False),
]
_EXT_INFO = {
    ".braw": ("Blackmagic RAW", True), ".r3d": ("REDCODE RAW", True),
    ".crm": ("Canon Cinema RAW Light", True), ".rmf": ("Canon RAW", True),
    ".ari": ("ARRIRAW", True), ".arx": ("ARRIRAW HDE", True), ".arri": ("ARRIRAW", True),
    ".dng": ("CinemaDNG", True),
}


def classify(codec: str, ext: str) -> dict:
    """Return {camera, raw, src_codec} from a flapi codec string (preferred) or
    the file extension as fallback."""
    c = (codec or "").lower()
    for key, label, raw in _CODEC_INFO:
        if key in c:
            return {"camera": label, "raw": raw, "src_codec": codec or label}
    e = ext.lower()
    if e in _EXT_INFO:
        label, raw = _EXT_INFO[e]
        return {"camera": label, "raw": raw, "src_codec": codec or label}
    return {"camera": codec or e.lstrip(".").upper() or "Unknown",
            "raw": False, "src_codec": codec or ""}


def crawl(folder: str | Path) -> Iterator[Path]:
    """Yield media files under ``folder`` in sorted order, as they're found."""
    folder = Path(folder)
    if folder.is_file():
        if folder.suffix.lower() in MEDIA_EXTS:
            yield folder
        return
    for root, dirs, files in os.walk(folder):
        # Skip dot-dirs (Spotlight, Trashes, sidecars) so the demo crawl is clean.
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("._"):
                continue
            p = Path(root) / name
            if p.suffix.lower() in MEDIA_EXTS:
                yield p
