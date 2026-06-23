"""FLAPI engine: connect, probe codecs, render poster thumbnails, transcode.

Adapted from the multimedia-enrichment project's render.py + FLAPIDecoder. Two
connection styles are used, matching what each job needs:

* a long-lived connection to the *already running* flapid on localhost
  (the "baseline server" the demo's first tab reports on) — used for cheap
  metadata reads and poster-thumbnail still exports during discovery;
* a fresh child flapid launched per transcode (``Connection().launch()``),
  one render per child, because a reused render daemon wedges after a couple
  of renders. Three worker threads each drive their own child — this is the
  same shard-per-flapid shape the enrichment pipeline uses, and the shape the
  "mass transcode" Netflix workflow uses across render nodes.

This module assumes ``import flapi`` works, i.e. it runs inside the bootstrapped
venv that pip-installed the build's filmlightapi wheel. It is imported lazily by
the server so the package still imports on a machine without the wheel.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import flapi

# FLAPI's Connection.launch() makes a fifo named /tmp/flapid-fifo-<pid>, keyed
# only on the process id — so two threads launching child flapids at once would
# collide on the same fifo. Serialize just the launch handshake; the rendering
# that follows still runs fully in parallel on the separate child daemons.
_LAUNCH_LOCK = threading.Lock()

# --------------------------------------------------------------------------- #
# Curated FLAPI-call tracing — feeds the demo's live "FLAPI call log" drawer.
# The server registers a tracer; engine code emits a friendly line per call.
# A thread-local op context tags each line (probe / scan / transcode + label).
# --------------------------------------------------------------------------- #
_tracer = None
_trace_ctx = threading.local()


def set_tracer(fn) -> None:
    global _tracer
    _tracer = fn


@contextlib.contextmanager
def trace_op(op: str, label: str = ""):
    prev = (getattr(_trace_ctx, "op", None), getattr(_trace_ctx, "label", ""))
    _trace_ctx.op, _trace_ctx.label = op, label
    try:
        yield
    finally:
        _trace_ctx.op, _trace_ctx.label = prev


def _trace(method: str, detail: str = "") -> None:
    fn = _tracer
    op = getattr(_trace_ctx, "op", None)
    if fn is None or op is None:
        return
    with contextlib.suppress(Exception):
        fn({"type": "flapi_call", "op": op, "label": getattr(_trace_ctx, "label", ""),
            "method": method, "detail": detail})

from .presets import Preset, resolve_codecs

# Poster thumbnails and proxies are colour-managed to display space.
THUMB_FORMAT = "HD 1920x1080"
THUMB_COLOURSPACE = "sRGB"

# Output is capped at HD so proxies stay light and web-friendly regardless of
# the (often 4K-8K) camera-original resolution.
RENDER_FORMAT = "HD 1920x1080"

# Deliverables are colour-managed from each clip's native camera space (LogC,
# REDWideGamut, S-Gamut3, etc.) to a Rec.709 / Rec.1886 display target — the
# standard for editorial proxies. Baselight applies the debayer + colour
# transform on the GPU as part of the render.
RENDER_COLOURSPACE = "Video_Full"          # Rec.709 primaries, BT.1886 EOTF
RENDER_COLOURSPACE_LABEL = "Rec.1886 (Rec.709)"

# Curated output colourspaces surfaced to the top of the selector, by workflow.
# Filtered at runtime to those the connected build actually offers.
RECOMMENDED_CS = [
    ("Video_Full", "Rec.1886 (Rec.709) · editorial proxy"),
    ("sRGB_Display", "sRGB · web review"),
    ("Apple_Display_P3D65", "Display P3 · Apple review"),
    ("DCI_P3D65", "DCI-P3 D65 · theatrical"),
    ("ACEScg", "ACEScg · VFX linear"),
    ("Rec709_lin", "Rec.709 linear · compositing"),
]
_CS_LABELS = dict(RECOMMENDED_CS)


def cs_label(key: str) -> str:
    """Friendly label for a colourspace key (recommended label, else prettified)."""
    if key in _CS_LABELS:
        # strip the workflow suffix for the compact pipeline display
        return _CS_LABELS[key].split(" · ")[0]
    return (key or "").replace("_", " ")

# Render watchdog timings (seconds) — lifted from render.py's proven values.
STALL_S = 75.0
FINALIZE_S = 150.0
HARD_CAP_BASE = 300.0
HARD_CAP_PER_S = 20.0


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #
def connect_local():
    """Connect to the flapid already listening on localhost:1984."""
    conn = flapi.Connection("localhost")
    conn.connect()
    return conn


# --------------------------------------------------------------------------- #
# Process registry — so the Teardown tab can show/kill the flapids we spawned.
# --------------------------------------------------------------------------- #
_procs: dict[int, dict] = {}
_procs_lock = threading.Lock()


def _proc_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _register_proc(conn, role: str) -> None:
    p = getattr(conn, "process", None)
    if p is None:
        return
    with _procs_lock:
        _procs[p.pid] = {"role": role, "started": time.time(), "conn": conn}


def _unregister_proc(conn) -> None:
    p = getattr(conn, "process", None)
    if p is None:
        return
    with _procs_lock:
        _procs.pop(p.pid, None)


def list_procs() -> list[dict]:
    """Live flapids we launched, with role + uptime (drops dead ones)."""
    with _procs_lock:
        items = list(_procs.items())
    out = []
    for pid, d in items:
        if not _proc_alive(pid):
            with _procs_lock:
                _procs.pop(pid, None)
            continue
        out.append({"pid": pid, "role": d["role"],
                    "uptime_s": round(time.time() - d["started"], 1)})
    out.sort(key=lambda x: (x["role"] != "search", x["pid"]))
    return out


def kill_proc(pid: int) -> bool:
    with _procs_lock:
        d = _procs.get(pid)
    if not d:
        return False
    with contextlib.suppress(Exception):
        teardown_child(d["conn"])   # also unregisters
    with _procs_lock:
        _procs.pop(pid, None)
    return True


def launch_child(role: str = "render"):
    """Launch a private child flapid (one render per child; see module docs).

    The launch handshake is serialized (see _LAUNCH_LOCK) so concurrent workers
    don't collide on FLAPI's per-pid fifo; the daemons run in parallel after.
    """
    with _LAUNCH_LOCK:
        conn = flapi.Connection()
        conn.launch(quiet=True)
    _register_proc(conn, role)
    _trace("Connection().launch()", f"spawned {role} flapid")
    return conn


def teardown_child(conn) -> None:
    """Graceful close, then SIGTERM/SIGKILL the child flapid if it lingers."""
    _trace("conn.close()", "tear down flapid")
    with contextlib.suppress(Exception):
        conn.close()
    p = getattr(conn, "process", None)
    if p is None:
        return
    for step in ("wait", "terminate", "kill"):
        try:
            if step == "wait":
                p.wait(timeout=8)
                return
            getattr(p, step)()
            p.wait(timeout=5)
            return
        except Exception:
            continue


class ServiceFlapid:
    """One long-lived 'search/probe' flapid, distinct from the per-render worker
    flapids. It's spun up once (warmed at startup) and reused for probing,
    ImageSearcher scans, metadata and poster thumbnails — so those don't pay a
    fresh flapid launch each time. flapi connections aren't thread-safe, so all
    access is serialized through ``use()``; if a call fails the connection is
    dropped and relaunched on next use.

    A background reaper tears the flapid down after ``idle_timeout`` seconds of
    no use (freeing its licence seat / GPU), and the next ``use()`` relaunches
    it transparently. The per-render worker flapids are separate and already
    torn down immediately after each render.
    """

    def __init__(self, idle_timeout: float = 300.0) -> None:
        self._conn = None
        self._qm = None
        self._lock = threading.RLock()
        self._last_used = 0.0
        self._idle_timeout = idle_timeout
        self._reaper_started = False

    @contextlib.contextmanager
    def use(self):
        with self._lock:
            # If the flapid was killed (via the Teardown tab or externally), drop
            # the stale handle so we transparently spin up a fresh one.
            if self._conn is not None:
                p = getattr(self._conn, "process", None)
                if p is not None and p.poll() is not None:
                    self._drop()
            if self._conn is None:
                self._conn = launch_child("search")
                self._qm = self._conn.QueueManager.create_no_database()
                self._ensure_reaper()
            self._last_used = time.time()
            try:
                yield self._conn, self._qm
            except Exception:
                self._drop()
                raise
            finally:
                self._last_used = time.time()

    def _ensure_reaper(self) -> None:
        if self._reaper_started:
            return
        self._reaper_started = True
        threading.Thread(target=self._reap, name="service-flapid-reaper",
                         daemon=True).start()

    def _reap(self) -> None:
        while True:
            time.sleep(30)
            with self._lock:
                if self._conn is not None and \
                        (time.time() - self._last_used) > self._idle_timeout:
                    self._drop()

    def _drop(self) -> None:
        conn, self._conn, self._qm = self._conn, None, None
        if conn is not None:
            with contextlib.suppress(Exception):
                teardown_child(conn)

    def warm(self) -> None:
        with self.use():
            pass

    def pid(self):
        with self._lock:
            p = getattr(self._conn, "process", None) if self._conn is not None else None
            return getattr(p, "pid", None)

    def drop(self) -> None:
        with self._lock:
            self._drop()

    def status(self) -> dict:
        with self._lock:
            up = self._conn is not None
            remaining = None
            if up:
                remaining = max(0.0, self._idle_timeout - (time.time() - self._last_used))
        return {
            "up": up,
            "pid": self.pid(),
            "idle_timeout_s": self._idle_timeout,
            "idle_remaining_s": round(remaining, 1) if remaining is not None else None,
        }


# --------------------------------------------------------------------------- #
# Environment / capability probes
# --------------------------------------------------------------------------- #
def _info_key(obj) -> str | None:
    """Extract the string .Key from a RenderFileTypeInfo / RenderCodecInfo."""
    k = getattr(obj, "Key", None)
    if k is None and isinstance(obj, dict):
        k = obj.get("Key")
    return str(k) if k else None


def probe_codecs(conn) -> dict:
    """Enumerate the live build's movie types + codecs and resolve our presets.

    Returns {"movie_types": [...], "codecs": {type: [...]},
             "presets": [{key,label,...,supported:bool,resolved:{...}}]}.
    """
    from .presets import PRESETS

    rs = conn.RenderSetup  # static methods must route through the connection
    try:
        raw_types = list(rs.get_movie_types())
    except Exception:
        raw_types = []

    # get_movie_types() / get_movie_codecs() return RenderFileTypeInfo /
    # RenderCodecInfo objects; we want their string .Key. Audio-only file types
    # (AIFF/WAV) yield no video codecs, so they drop out here.
    movie_types: list[str] = []
    codecs: dict[str, list[str]] = {}
    type_ext: dict[str, str] = {}            # type key -> container extension (".mov")
    codec_text: dict[tuple, str] = {}        # (type, codec) -> friendly Text
    for t in raw_types:
        key = _info_key(t)
        if not key:
            continue
        exts = getattr(t, "Extensions", None)
        if exts is None and isinstance(t, dict):
            exts = t.get("Extensions")
        ext = (exts[0] if exts else "") or ""
        ck: list[str] = []
        with contextlib.suppress(Exception):
            for c in rs.get_movie_codecs(key):
                ckey = _info_key(c)
                if not ckey:
                    continue
                ck.append(ckey)
                txt = getattr(c, "Text", None)
                if txt is None and isinstance(c, dict):
                    txt = c.get("Text")
                codec_text[(key, ckey)] = txt or ckey
        if not ck:
            continue
        movie_types.append(key)
        codecs[key] = ck
        type_ext[key] = ext

    # Recommended deliverables (curated presets) + every supported movie codec.
    presets_out = []
    deliverables: list[dict] = []
    for p in PRESETS:
        resolved = resolve_codecs(p, movie_types, lambda t: codecs.get(t, []))
        presets_out.append({
            "key": p.key, "label": p.label, "description": p.description,
            "extension": p.extension, "web_playable": p.web_playable,
            "result_label": p.result_label,
            "supported": resolved is not None, "resolved": resolved,
        })
        if resolved is not None:
            deliverables.append({
                "key": p.key, "label": p.result_label, "recommended": True,
                "file_type": resolved["file_type"], "movie_codec": resolved["movie_codec"],
                "extension": p.extension, "audio_codec": p.audio_codec,
                "image_options": dict(p.image_options), "web_playable": p.web_playable,
            })
    # Only offer single-file movie containers (.mov/.mp4). This drops the DCP /
    # IMF / MXF *package* formats that get_movie_types() also reports (their
    # "extension" is a package path like /ASSETMAP.XML) and avoids invalid
    # type×codec pairings (e.g. ProRes-in-MXF) that don't render.
    _MOVIE_CONTAINERS = {".mov", ".mp4"}
    _rec = {(d["file_type"], d["movie_codec"]) for d in deliverables}
    for vtype in movie_types:
        ext = type_ext.get(vtype) or ""
        if ext.lower() not in _MOVIE_CONTAINERS:
            continue
        for codec in codecs[vtype]:
            if (vtype, codec) in _rec:
                continue
            deliverables.append({
                "key": f"{vtype}:{codec}",
                "label": f"{codec_text.get((vtype, codec), codec)} · {ext.lstrip('.').upper()}",
                "recommended": False, "file_type": vtype, "movie_codec": codec,
                "extension": ext, "audio_codec": "aac128", "image_options": {},
                "web_playable": False,
            })

    # Only display-referred colourspaces make sense as a render *output* target
    # (skip the camera log / scene-linear input spaces). Use the friendly
    # DisplayName, e.g. "Rec.1886: 2.4 Gamma / Rec.709".
    cs_list: list[dict] = []
    with contextlib.suppress(Exception):
        fs = conn.FormatSet.factory_formats()
        for n in fs.get_colour_space_names():
            with contextlib.suppress(Exception):
                info = fs.get_colour_space_info(n)
                d = vars(info) if hasattr(info, "__dict__") else info
                if d.get("Type") == "display":
                    cs_list.append({"key": d.get("Name") or n,
                                    "label": d.get("DisplayName") or n})
        cs_list.sort(key=lambda c: c["label"].lower())
    cs_keys = {c["key"] for c in cs_list}
    recommended = [{"key": k, "label": lbl} for k, lbl in RECOMMENDED_CS if k in cs_keys]

    return {"movie_types": movie_types, "codecs": codecs, "presets": presets_out,
            "deliverables": deliverables,
            "colourspaces": cs_list, "recommended_colourspaces": recommended,
            "default_colourspace": RENDER_COLOURSPACE}


# --------------------------------------------------------------------------- #
# Metadata + poster thumbnail (uses the shared localhost connection)
# --------------------------------------------------------------------------- #
def _seq_metadata(seq) -> dict:
    """Dimensions, fps, duration, audio, timecode from a SequenceDescriptor."""
    w = int(seq.get_width())
    h = int(seq.get_height())
    fps = float(seq.get_movie_fps()) or 0.0
    sf = int(seq.get_start_frame())
    ef = int(seq.get_end_frame())
    frames = ef - sf + 1
    dur = frames / fps if fps else None
    audio = int(seq.get_audio_channels()) if seq.has_audio() else 0
    tc = ""
    with contextlib.suppress(Exception):
        tc = str(seq.get_start_timecode())
    return {
        "width": w or None, "height": h or None, "fps": fps or None,
        "frames": frames, "duration_seconds": dur,
        "audio_channels": audio, "start_timecode": tc,
    }


def read_metadata(conn, src: str) -> dict:
    """Dimensions, fps, duration, audio channels for a source clip."""
    seq = conn.SequenceDescriptor.get_for_file(src)
    try:
        return _seq_metadata(seq)
    finally:
        with contextlib.suppress(Exception):
            seq.release()


def scan_directory(conn, folder: str, recurse: bool = True,
                   timeout_s: float = 120.0) -> list[dict]:
    """Enumerate media in a folder using FLAPI's ImageSearcher.

    Returns a list of {"src", "name", "meta"} — one per clip. ImageSearcher is
    the native FLAPI directory scanner: it finds movies and groups image-
    sequence frames (ARRIRAW/DNG) into single clips, which a plain os.walk
    can't. Runs asynchronously inside flapid; we poll to completion.

    ImageSearcher can refuse some paths ("root dir is non-canonical" when a path
    component is a symlink, or permission quirks). We canonicalize first, and if
    it still raises *or* finds nothing we fall back to a plain directory crawl,
    so the demo degrades gracefully instead of showing an empty scan.
    """
    # Canonicalize: ImageSearcher rejects paths whose components aren't fully
    # resolved. realpath collapses symlinks and "..".
    folder = os.path.realpath(folder)
    out: list[dict] = []
    failed = False
    isr = None
    try:
        isr = conn.ImageSearcher.create()
        _trace("ImageSearcher.create()", "FLAPI directory scanner")
        isr.add_root_directory(folder, 1 if recurse else 0)
        _trace("ImageSearcher.add_root_directory()", folder)
        isr.scan()
        _trace("ImageSearcher.scan()", "async — movies + grouped sequences")
        t0 = time.time()
        while isr.is_scan_in_progress():
            if time.time() - t0 > timeout_s:
                isr.cancel()
                break
            time.sleep(0.15)
        track = flapi.IMAGESEARCHER_METADATA_TRACK.ISMT_FRAME_NUMBER
        seqs = isr.get_sequences(track, 4096)
        _trace("ImageSearcher.get_sequences()", f"{len(seqs)} clip(s) found")
        for seq in seqs:
            try:
                name = str(seq.get_name())
                path = str(seq.get_path())
                src = name if os.path.isabs(name) else os.path.join(path, name)
                out.append({"src": src, "name": name, "meta": _seq_metadata(seq)})
            except Exception:  # noqa: BLE001
                continue
            finally:
                with contextlib.suppress(Exception):
                    seq.release()
    except Exception as e:  # noqa: BLE001
        # add_root_directory / scan can raise (non-canonical path, permissions).
        failed = True
        _trace("ImageSearcher failed — falling back to crawl", str(e)[:80])
    finally:
        if isr is not None:
            with contextlib.suppress(Exception):
                isr.release()

    if not out and (failed or _has_media(folder)):
        return _crawl_directory(conn, folder, recurse)

    out.sort(key=lambda c: c["name"].lower())
    return out


def _has_media(folder: str) -> bool:
    """Cheap check: does a crawl find at least one media file under ``folder``?"""
    from . import media_scan
    return next(media_scan.crawl(folder), None) is not None


def _crawl_directory(conn, folder: str, recurse: bool = True) -> list[dict]:
    """Fallback scan: plain os.walk for media files when ImageSearcher can't.

    Each file becomes one clip, probed with SequenceDescriptor for metadata.
    This can't group multi-frame image sequences the way ImageSearcher does, but
    it works on any path the OS can read — the robust escape hatch.
    """
    from . import media_scan
    _trace("os.walk crawl (ImageSearcher fallback)", folder)
    out: list[dict] = []
    base = Path(folder)
    for p in media_scan.crawl(folder):
        if not recurse and p.parent != base:
            continue
        src = str(p)
        try:
            meta = read_metadata(conn, src)
        except Exception:  # noqa: BLE001
            meta = {}
        out.append({"src": src, "name": p.name, "meta": meta})
    _trace("crawl complete", f"{len(out)} clip(s) found")
    out.sort(key=lambda c: c["name"].lower())
    return out


def render_poster(conn, qm, src: str, out_path: str, timeout_s: float = 60.0,
                  on_info=None) -> dict:
    """Export a single colour-managed poster JPEG (centre frame) for ``src``.

    Uses a no-database queue running inside the existing flapid. Returns
    {"ok": bool, "codec": str, "input_cs": str} — the source codec and native
    camera colour space are captured from the inserted shot so the UI can show
    the RAW→display pipeline. Adapted from FLAPIDecoder._render_one_file.
    """
    seq = conn.SequenceDescriptor.get_for_file(src)
    scene = shot = ex = None
    codec = ""
    input_cs = ""
    tmp = tempfile.mkdtemp(prefix="tcdemo_poster_")
    try:
        fps = float(seq.get_movie_fps()) or 24.0
        sf = int(seq.get_start_frame())
        ef = int(seq.get_end_frame())
        mid = sf + (ef - sf) // 2

        scene = conn.Scene.temporary_scene({
            "format": THUMB_FORMAT,
            "colourspace": THUMB_COLOURSPACE,
            "frame_rate": fps,
            "field_order": flapi.FIELDORDER_PROGRESSIVE,
        })
        scene.start_delta("insert+mark")
        shot = scene.insert_sequence(seq, flapi.INSERT_END, None, None, None)
        with contextlib.suppress(Exception):
            codec = str(shot.get_codec())
        with contextlib.suppress(Exception):
            input_cs = str(shot.get_actual_input_colour_space())
        # Codec/colour space are known now (cheap) — let the caller publish the
        # row immediately, before the slow debayered still export below.
        if on_info is not None:
            with contextlib.suppress(Exception):
                on_info(codec, input_cs)
        cats = scene.get_mark_categories()
        category = cats[0] if cats else "DefaultMark"
        shot.add_mark(mid, category, None, None)
        scene.end_delta()

        settings = flapi.StillExportSettings()
        settings.Directory = tmp
        settings.ColourSpace = THUMB_COLOURSPACE
        settings.Format = THUMB_FORMAT
        settings.FileType = "JPEG"
        settings.Overwrite = flapi.EXPORT_OVERWRITE_REPLACE
        settings.Frames = flapi.EXPORT_FRAMES_MARKED
        settings.MarkCategory = {category}
        settings.Source = flapi.EXPORT_SOURCE_SELECTEDSHOTS
        # Thumbnails only need a fast debayer, not a finishing-grade decode.
        draft = getattr(flapi, "STILLEXPORT_DECODEQUALITY_GMDQ_DRAFT", None)
        if draft is not None:
            settings.DecodeQuality = draft

        ex = conn.Export.create()
        ex.select_shot(shot)
        _trace("Export.do_export_still()", "thumbnail (draft debayer)")
        info = ex.do_export_still(qm, scene, settings)
        _wait_op(qm, info.ID, timeout_s)

        rendered_dir = Path(tmp) / "undefined"
        if not rendered_dir.is_dir():
            rendered_dir = Path(tmp)
        jpgs = sorted(rendered_dir.rglob("*.jpg"))
        if not jpgs:
            return {"ok": False, "codec": codec, "input_cs": input_cs}
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(str(jpgs[0]), out_path)
        return {"ok": True, "codec": codec, "input_cs": input_cs}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for obj, meth in ((ex, "release"), (shot, "release"),
                          (scene, "close_scene"), (scene, "release"), (seq, "release")):
            if obj is not None:
                with contextlib.suppress(Exception):
                    getattr(obj, meth)()


def _wait_op(qm, op_id, timeout_s: float) -> None:
    t0 = time.time()
    while True:
        st = qm.get_operation_status(op_id)
        if st.Status == "Done":
            return
        if st.Status == "Failed":
            with contextlib.suppress(Exception):
                qm.delete_operation(op_id)
            raise RuntimeError(f"FLAPI op {op_id} failed: {st.ProgressText}")
        if time.time() - t0 > timeout_s:
            with contextlib.suppress(Exception):
                qm.delete_operation(op_id)
            raise TimeoutError(f"FLAPI op {op_id} timed out after {timeout_s}s")
        time.sleep(0.05)


# --------------------------------------------------------------------------- #
# Transcode (uses a private child flapid; on_progress(pct) gets 0..1)
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    """Raised inside transcode() when a cancel was requested."""


def transcode(conn, src: str, out_dir: str, out_stem: str,
              deliverable: dict, fps_hint: float | None = None,
              on_progress=None, should_cancel=None, colourspace: str | None = None) -> dict:
    """Render one transcoded movie for ``src`` into ``out_dir``/<out_stem><ext>.

    ``deliverable`` describes the output: {file_type, movie_codec, extension,
    audio_codec, image_options}. Returns {"output": path, "wall_seconds": float,
    "size_bytes": int}. Raises on stall / finalize-hang / failure.
    """
    out_dir_p = Path(out_dir)
    out_dir_p.mkdir(parents=True, exist_ok=True)
    extension = deliverable.get("extension") or ".mov"

    seq = conn.SequenceDescriptor.get_for_file(src)
    fps = fps_hint or float(seq.get_movie_fps()) or 24.0
    sf = int(seq.get_start_frame())
    ef = int(seq.get_end_frame())
    dur = (ef - sf + 1) / fps if fps else 0.0
    hard_cap = HARD_CAP_BASE + dur * HARD_CAP_PER_S

    cs = colourspace or RENDER_COLOURSPACE
    _trace("SequenceDescriptor.get_for_file", Path(src).name)
    opts = flapi.NewSceneOptions(
        format=RENDER_FORMAT, colourspace=cs, frame_rate=fps)
    scene = conn.Scene.temporary_scene(opts)
    _trace("Scene.temporary_scene", f"{RENDER_FORMAT} · {cs}")
    scene.start_delta("insert")
    scene.insert_sequence(seq, flapi.INSERT_START)
    scene.end_delta()
    _trace("scene.insert_sequence", "camera original → timeline")

    rs = conn.RenderSetup.create_from_scene(scene)
    rs.delete_all_deliverables()
    d = flapi.RenderDeliverable()
    d.Name = str(deliverable.get("key", "proxy"))[:30]
    d.Disabled = 0
    d.IsMovie = 1
    d.FileType = deliverable["file_type"]
    d.MovieCodec = deliverable["movie_codec"]
    d.AudioCodec = deliverable.get("audio_codec") or "aac128"
    d.FastStart = 1
    d.OutputDirectory = str(out_dir_p)
    d.FileNamePrefix = out_stem
    d.FileNamePostfix = ""
    d.FileNameExtension = extension
    d.RenderFormat = RENDER_FORMAT
    d.RenderColourSpace = cs
    d.RenderDecodeQuality = flapi.DECODEQUALITY_OPTIMISED
    if deliverable.get("image_options"):
        d.ImageOptions = dict(deliverable["image_options"])
    rs.add_deliverable(d)
    _trace("RenderSetup + RenderDeliverable", f"{deliverable['movie_codec']} → {cs}")

    rp = conn.RenderProcessor.get()
    t0 = time.time()
    rp.start(rs)
    _trace("RenderProcessor.start()", "GPU debayer + colour transform")
    _trace("RenderProcessor.get_progress()", "poll until Status == Done")
    best = -1.0
    last_advance = t0
    reached_100 = None
    try:
        while True:
            if should_cancel is not None and should_cancel():
                with contextlib.suppress(Exception):
                    rp.shutdown()
                raise Cancelled()
            st = rp.get_progress()
            status = getattr(st, "Status", "")
            if status == flapi.OPSTATUS_DONE:
                break
            if status == getattr(flapi, "OPSTATUS_FAILED", "Failed"):
                raise RuntimeError("render failed: %s" % getattr(st, "ProgressText", ""))
            pct = float(getattr(st, "Progress", 0) or 0)
            now = time.time()
            if pct > best + 1e-6:
                best = pct
                last_advance = now
                if on_progress:
                    with contextlib.suppress(Exception):
                        on_progress(min(pct, 0.999))
            if pct >= 0.999:
                reached_100 = reached_100 or now
                if now - reached_100 > FINALIZE_S:
                    raise TimeoutError("stuck finalizing at 100%% for %.0fs" % FINALIZE_S)
            elif now - last_advance > STALL_S:
                raise TimeoutError("stalled at %.0f%% for %.0fs" % (best * 100, STALL_S))
            if now - t0 > hard_cap:
                raise TimeoutError("exceeded hard cap %.0fs at %.0f%%" % (hard_cap, best * 100))
            time.sleep(0.5)
        wall = time.time() - t0
    finally:
        for fn in (rp.shutdown, rs.release, scene.close_scene, scene.release, seq.release):
            with contextlib.suppress(Exception):
                fn()

    out = out_dir_p / (out_stem + extension)
    if not out.exists() or not out.stat().st_size:
        raise RuntimeError("render produced no output file")
    _trace("render complete", f"{wall:.1f}s · {out.stat().st_size // 1000} kB")
    if on_progress:
        with contextlib.suppress(Exception):
            on_progress(1.0)
    return {"output": str(out), "wall_seconds": wall, "size_bytes": out.stat().st_size}
