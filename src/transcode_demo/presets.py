"""Destination-format presets for the transcode demo.

Each preset maps a friendly dropdown label onto the FLAPI RenderDeliverable
fields needed to produce it. We deliberately keep a small, editorial-proxy
oriented set (the workflow Konsole's Illusion lives in): an HEVC QuickTime and
a ProRes proxy QuickTime, plus a lightweight H.264 MP4 that mirrors the proxy
the multimedia-enrichment pipeline already renders.

The exact movie-type / codec *keys* a given Baselight build accepts can be
enumerated live (RenderSetup.get_movie_types / get_movie_codecs), so
``resolve_codecs`` reconciles each preset against what the connected flapid
actually offers and falls back gracefully. Nothing here imports flapi at module
load, so the module stays importable on hosts without the wheel.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Preset:
    key: str                      # stable id used by the API/UI
    label: str                    # dropdown text
    description: str              # one-liner shown under the dropdown
    file_type: str                # FLAPI RenderDeliverable.FileType (movie type key)
    file_type_aliases: list[str]  # acceptable alternates if the primary key is absent
    extension: str                # output container extension
    movie_codec: str              # preferred RenderDeliverable.MovieCodec
    movie_codec_aliases: list[str]
    audio_codec: str = "aac128"
    # Editorial proxies are delivered in display space; HEVC deliverables for
    # review can stay in a wider space if the build supports it. Keep it simple
    # and colour-manage everything to sRGB for the demo.
    colour_space: str = "sRGB"
    image_options: dict = field(default_factory=dict)
    # Plays inline in a browser <video> (H.264/MP4 yes; ProRes/HEVC-mov no).
    web_playable: bool = False
    # Short codec name shown in the player / row (matches what was selected).
    codec_label: str = ""

    @property
    def result_label(self) -> str:
        """e.g. 'H.264 · MP4' — the resulting codec + container."""
        return f"{self.codec_label} · {self.extension.lstrip('.').upper()}"


# Codec key candidates differ slightly between builds; we list a few spellings
# and pick whichever the live flapid reports. h264hw/hevchw are the
# hardware-accelerated encoders on Apple silicon (fast, what a DIT cart wants).
# File-type aliases that resolve to whatever QuickTime/MP4 movie key the build
# uses (lqtmov on BaselightLOOK, lqtmp4/quicktime on full Baselight).
_QT = ["lqtmov", "quicktime", "mov", "qt", "lqtmp4", "mp4"]

PRESETS: list[Preset] = [
    Preset(
        key="h264_web",
        label="H.264 (MP4) — web playable, HD",
        description="HD H.264 + AAC in MP4, faststart. Plays straight in the browser; the safe default.",
        file_type="lqtmp4",
        file_type_aliases=["mp4", "lqtmov", "quicktime", "mov", "qt"],
        extension=".mp4",
        movie_codec="h264hw",
        movie_codec_aliases=["h264", "x264", "avc1", "h264_videotoolbox"],
        image_options={"kbitrate": 8000},
        web_playable=True,
        codec_label="H.264",
    ),
    Preset(
        key="prores_proxy",
        label="ProRes 422 Proxy (QuickTime)",
        description="Editorial proxy — the DIT default. (Full Baselight; download to view.)",
        file_type="lqtmov",
        file_type_aliases=_QT,
        extension=".mov",
        movie_codec="prores_422_proxy",
        movie_codec_aliases=["prores422proxy", "apco", "prores_proxy", "prores422_proxy"],
        codec_label="ProRes 422 Proxy",
    ),
    Preset(
        key="hevc_mov",
        label="HEVC Movie (QuickTime)",
        description="HEVC/H.265 in QuickTime — compact review deliverable. (Full Baselight.)",
        file_type="lqtmov",
        file_type_aliases=_QT,
        extension=".mov",
        movie_codec="hevchw",
        movie_codec_aliases=["hevc", "h265", "hevc_videotoolbox", "hev1"],
        image_options={"kbitrate": 12000},
        codec_label="HEVC",
    ),
]

PRESET_BY_KEY = {p.key: p for p in PRESETS}


def _pick(primary: str, aliases: list[str], available: list[str]) -> str | None:
    """Return the first of (primary, *aliases) present in ``available``.

    Match is case-insensitive and tolerant of separators so 'prores_422_proxy'
    matches 'ProRes422Proxy'. Returns None when nothing matches.
    """
    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    avail_norm = {norm(a): a for a in available}
    for cand in [primary, *aliases]:
        hit = avail_norm.get(norm(cand))
        if hit is not None:
            return hit
    return None


def resolve_codecs(preset: Preset, movie_types: list[str], codecs_for_type) -> dict | None:
    """Reconcile a preset against the live build's capabilities.

    ``movie_types`` is RenderSetup.get_movie_types(); ``codecs_for_type`` is a
    callable type_key -> list[codec_key] (RenderSetup.get_movie_codecs). Returns
    a dict of the resolved {file_type, movie_codec} or None if unsupported here.
    """
    ftype = _pick(preset.file_type, preset.file_type_aliases, movie_types)
    if ftype is None:
        return None
    codecs = codecs_for_type(ftype) or []
    codec = _pick(preset.movie_codec, preset.movie_codec_aliases, codecs)
    if codec is None:
        return None
    return {"file_type": ftype, "movie_codec": codec}
