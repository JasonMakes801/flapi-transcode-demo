"""Environment probe for the demo's first tab — "is the render service ready?".

The demo is self-contained: rather than depend on a Baselight GUI or a daemon
already listening on :1984, it spins up its *own* child flapid (the same model
the render workers use) and reports:
  * can we launch a baseline render service at all
  * is it licensed (querying capabilities requires a valid licence)
  * which Baselight build / SDK is serving
  * licence summary
  * which destination-format presets this build can actually produce

This mirrors what the multimedia-enrichment pipeline does: launch a private
flapid per task. No external daemon required.
"""
from __future__ import annotations

import contextlib

# flapi SDK keys worth surfacing as "camera formats this build can decode" — the
# wide-RAW-support story. Mapped to friendly names; flagged if camera RAW.
CAMERA_SDKS = {
    "arriraw5": ("ARRIRAW", True),
    "codexhde": ("ARRIRAW HDE", True),
    "red": ("RED (R3D)", True),
    "braw": ("Blackmagic RAW", True),
    "canonraw": ("Canon RAW", True),
    "sonyraw": ("Sony RAW / X-OCN", True),
    "proresraw": ("Apple ProRes RAW", True),
    "nikonipx": ("Nikon RAW", True),
    "libraw": ("Photo RAW", True),
    "prores": ("Apple ProRes", False),
    "aviddnx": ("Avid DNx", False),
    "sonyxavc": ("Sony XAVC", False),
    "comprimatodec": ("JPEG 2000", False),
}


def probe(conn) -> dict:
    """Environment snapshot gathered from an existing (service) flapid `conn`.

    Does not launch/teardown — the caller owns the connection (the shared
    search/probe flapid). The server adds "service_up". Never raises.
    """
    from . import flapi_engine as eng
    result: dict = {
        "connected": False,      # licensed (capabilities readable)
        "application": {},
        "licence": {},
        "capabilities": {},
        "decoders": [],          # camera-format SDKs (the wide-RAW-support story)
        "error": None,
    }
    eng._trace("Application.get_application_info()", "build + version")
    with contextlib.suppress(Exception):
        appd = _as_dict(conn.Application.get_application_info())  # static method
        # Derive a clean build version from the resolved build path (this is the
        # Current symlink target we launched from, e.g. 7.0.1.25379).
        path = appd.get("Path", "")
        ver = ""
        if "/Baselight/" in path:
            ver = path.split("/Baselight/")[1].split("/")[0]
        if not ver and appd.get("Major") is not None:
            ver = f"{appd.get('Major')}.{appd.get('Minor')}.{appd.get('Build')}"
        appd["Version"] = ver
        result["application"] = appd
    with contextlib.suppress(Exception):
        for s in conn.Application.get_sdk_versions():
            key = getattr(s, "Key", None)
            if key in CAMERA_SDKS:
                name, raw = CAMERA_SDKS[key]
                result["decoders"].append(
                    {"key": key, "name": name, "raw": raw,
                     "version": getattr(s, "Version", None)})
    eng._trace("Licence.get_licence_info()", "verify licence")
    with contextlib.suppress(Exception):
        result["licence"] = _summarise_licence(conn.Licence.get_licence_info())
    # Capability query is licence-gated, so success here proves we're licensed.
    eng._trace("RenderSetup.get_movie_types / get_movie_codecs", "enumerate deliverables")
    eng._trace("FormatSet.get_colour_space_info()", "enumerate display colourspaces")
    try:
        caps = eng.probe_codecs(conn)
        result["capabilities"] = caps
        result["connected"] = bool(caps.get("movie_types"))
    except Exception as e:  # noqa: BLE001
        result["error"] = f"flapid is up but not licensed: {e}"
    return result


def _as_dict(obj) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return {"value": str(obj)}


def _summarise_licence(lic) -> dict:
    """Reduce LicenceItems to one entry per product at its highest version.

    licence.flic lists 5.0/6.0/7.0 entries per product; we want the current
    (max) version, not whichever happened to be listed first.
    """
    items = lic if isinstance(lic, (list, tuple)) else [lic]
    best: dict[str, str] = {}
    for it in items:
        d = _as_dict(it)
        prod, ver = d.get("Product"), d.get("Version") or ""
        if not prod:
            continue
        if prod not in best or ver > best[prod]:
            best[prod] = ver
    return {"items": [{"Product": p, "Version": v} for p, v in best.items()],
            "products": sorted(best)}
