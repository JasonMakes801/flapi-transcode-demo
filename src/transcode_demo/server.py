"""FastAPI app for the transcode demo.

Two working tabs, one page (Alpine.js, FilmLight aesthetic):

  Environment  GET  /api/probe        -> service/licence/decoders/formats
  Scan         POST /api/discover     -> crawl a dir; clips stream over SSE
               POST /api/transcode    -> transcode clip ids (or all) on 3 workers
               POST /api/delete_transcodes -> wipe outputs, reset rows (re-demo)

Discover and Render are one view: rows are clips, each with its own state and a
Transcode button, plus Transcode-all and Delete-all-transcodes. flapi connections
are confined to single threads — discovery owns one child flapid; each render
worker launches its own.
"""
from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response, StreamingResponse)

from . import media_scan, probe as probe_mod
from .jobs import Broadcaster, ClipPool
from .presets import PRESET_BY_KEY

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
STATIC = HERE / "static"

STATE_DIR = Path(os.environ.get("TCDEMO_STATE", str(Path.home() / ".transcode-demo")))
POSTERS = STATE_DIR / "posters"
DEFAULT_OUT = Path(os.environ.get("TCDEMO_OUT", str(Path.home() / "transcode_demo_output")))
POSTERS.mkdir(parents=True, exist_ok=True)
DEFAULT_OUT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="FilmLight Transcode Demo")
bus = Broadcaster()

# ---- shared mutable state (single-process demo) --------------------------- #
clips: dict[str, dict] = {}               # clip_id -> clip dict (Scan rows)
_discover_stop = threading.Event()
_discover_thread: threading.Thread | None = None
_caps_cache: dict | None = None
_caps_lock = threading.Lock()
_service = None                  # the shared search/probe flapid (lazy)
_service_init_lock = threading.Lock()


def _get_service():
    """The single long-lived search/probe flapid (separate from render workers)."""
    global _service
    with _service_init_lock:
        if _service is None:
            from . import flapi_engine as eng
            _service = eng.ServiceFlapid()
        return _service


def _render_one(clip: dict, deliverable: dict, set_progress,
                should_cancel=None, colourspace=None) -> dict:
    """Worker callback: launch own child flapid, transcode, teardown."""
    from . import flapi_engine as eng
    with eng.trace_op("transcode", clip.get("name", "")):
        conn = eng.launch_child()
        try:
            safe = str(deliverable.get("key", "out")).replace(":", "_")
            stem = f"{Path(clip['src']).stem}_{safe}"
            res = eng.transcode(conn, clip["src"], app.state.out_dir, stem, deliverable,
                                on_progress=set_progress, should_cancel=should_cancel,
                                colourspace=colourspace)
        finally:
            eng.teardown_child(conn)
    return {
        "output": res["output"],
        "output_url": f"/outputs/{Path(res['output']).name}",
        "size_bytes": res["size_bytes"],
        "wall_seconds": res["wall_seconds"],
        "out_codec": deliverable.get("label"),
        "out_colourspace": eng.cs_label(colourspace or eng.RENDER_COLOURSPACE),
    }


pool = ClipPool(bus, _render_one)
pool.attach(clips)
app.state.out_dir = str(DEFAULT_OUT)


def _capabilities() -> dict:
    """Probe + cache the build's movie types/codecs (launches a child flapid).

    Launching a child flapid + probing is slow (tens of seconds), so this is
    warmed in the background at startup (see _warm_capabilities); the first
    Transcode click then returns instantly instead of blocking on it.
    """
    global _caps_cache
    with _caps_lock:
        if _caps_cache is None:
            from . import flapi_engine as eng
            with _get_service().use() as (conn, _qm):
                _caps_cache = eng.probe_codecs(conn)
        return _caps_cache


@app.on_event("startup")
def _warm_service() -> None:
    from . import flapi_engine as eng
    eng.set_tracer(bus.publish)        # feed the live FLAPI-call log drawer
    def _warm():
        with contextlib.suppress(Exception):
            _get_service().warm()      # spin up the search/probe flapid
        with contextlib.suppress(Exception):
            _capabilities()            # populate codec cache (reuses it)
    threading.Thread(target=_warm, name="tc-warm", daemon=True).start()


# --------------------------------------------------------------------------- #
# Pages + assets
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB / "index.html").read_text())


@app.get("/favicon.ico")
def favicon():
    return FileResponse(str(STATIC / "favicon.svg"), media_type="image/svg+xml")


@app.get("/static/{name}")
def static_asset(name: str):
    p = STATIC / name
    if not p.is_file():
        return Response(status_code=404)
    mime, _ = mimetypes.guess_type(str(p))
    return FileResponse(str(p), media_type=mime or "application/octet-stream")


@app.get("/posters/{name}")
def poster(name: str):
    p = POSTERS / name
    if not p.is_file():
        return Response(status_code=404)
    return FileResponse(str(p), media_type="image/jpeg")


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
@app.get("/events")
def events(request: Request) -> StreamingResponse:
    q = bus.subscribe()

    def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    ev = q.get(timeout=15)
                    yield f"data: {json.dumps(ev)}\n\n"
                except Exception:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- #
# Environment tab
# --------------------------------------------------------------------------- #
@app.get("/api/probe")
def api_probe() -> JSONResponse:
    try:
        from . import flapi_engine as eng
        with _get_service().use() as (conn, _qm), eng.trace_op("probe"):
            result = probe_mod.probe(conn)
        result["service_up"] = True
    except Exception as e:  # noqa: BLE001
        result = {"service_up": False, "connected": False, "application": {},
                  "licence": {}, "capabilities": {}, "decoders": [],
                  "error": f"could not start search flapid: {e}"}
    return JSONResponse(result)


# --------------------------------------------------------------------------- #
# Scan tab — discover
# --------------------------------------------------------------------------- #
@app.get("/api/clips")
def api_clips() -> JSONResponse:
    return JSONResponse({"clips": list(clips.values())})


@app.get("/api/browse")
def api_browse(path: str = "") -> JSONResponse:
    """List sub-directories of ``path`` for the server-side folder chooser."""
    base = Path(path) if path else Path.home()
    if not base.is_dir():
        base = Path.home()
    base = base.resolve()
    dirs: list[str] = []
    with contextlib.suppress(Exception):
        dirs = sorted((d.name for d in base.iterdir()
                       if d.is_dir() and not d.name.startswith(".")), key=str.lower)
    volumes: list[str] = []
    with contextlib.suppress(Exception):
        volumes = sorted(d.name for d in Path("/Volumes").iterdir() if d.is_dir())
    return JSONResponse({"path": str(base), "parent": str(base.parent),
                         "dirs": dirs, "volumes": volumes, "home": str(Path.home())})


@app.get("/api/config")
def api_config() -> JSONResponse:
    """Runtime config the UI surfaces: output location + worker count."""
    return JSONResponse({"out_dir": app.state.out_dir, "workers": pool.n})


@app.post("/api/outdir")
async def api_set_outdir(request: Request) -> JSONResponse:
    """Point transcode output at a different folder (created if needed)."""
    body = await request.json()
    raw = (body or {}).get("out_dir", "").strip()
    if not raw:
        return JSONResponse({"error": "no directory given"}, status_code=400)
    out = Path(raw).expanduser()
    try:
        out.mkdir(parents=True, exist_ok=True)
        if not os.access(out, os.W_OK):
            raise PermissionError("not writable")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"can't use {out}: {e}"}, status_code=400)
    app.state.out_dir = str(out)
    return JSONResponse({"ok": True, "out_dir": app.state.out_dir})


@app.post("/api/discover")
async def api_discover(request: Request) -> JSONResponse:
    global _discover_thread
    body = await request.json()
    folder = (body or {}).get("dir", "").strip()
    if not folder:
        return JSONResponse({"error": "no directory given"}, status_code=400)
    if not Path(folder).exists():
        return JSONResponse({"error": f"path not found: {folder}"}, status_code=400)

    _discover_stop.set()
    if _discover_thread and _discover_thread.is_alive():
        _discover_thread.join(timeout=2)
    clips.clear()
    _discover_stop.clear()

    _discover_thread = threading.Thread(target=_run_discovery, args=(folder,),
                                        name="tc-discover", daemon=True)
    _discover_thread.start()
    return JSONResponse({"ok": True, "dir": folder})


def _run_discovery(folder: str) -> None:
    """Scan a folder with FLAPI's ImageSearcher, emit a row (with metadata) per
    clip, then fill codec/colour space + a debayered thumbnail per clip (the
    slow part) — thumbnails lazy-fill with a spinner.
    """
    from . import flapi_engine as eng
    bus.publish({"type": "discover_started", "dir": folder})
    try:
        # Reuse the persistent search flapid (no per-scan launch/teardown).
        with _get_service().use() as (conn, qm), eng.trace_op("scan"):
            # FLAPI ImageSearcher enumerates movies + grouped image sequences.
            try:
                found = eng.scan_directory(conn, folder)
            except Exception as e:  # noqa: BLE001
                bus.publish({"type": "discover_error", "error": str(e)})
                found = []

            items: list[str] = []
            for i, info in enumerate(found, start=1):
                if _discover_stop.is_set():
                    break
                tid = uuid.uuid4().hex[:8]
                ext = os.path.splitext(info["name"])[1].lower()
                cls0 = media_scan.classify("", ext)
                clip = {
                    "id": tid, "src": info["src"], "name": info["name"], "ext": ext,
                    "camera": cls0["camera"], "raw": cls0["raw"], "src_codec": cls0["src_codec"],
                    "input_cs": "", "meta": info["meta"], "poster_url": None, "thumb_state": "pending",
                    "state": "idle", "progress": 0.0, "preset_key": None,
                    "output": None, "output_url": None, "size_bytes": None, "wall_seconds": None,
                    "out_codec": None, "out_colourspace": None, "error": None,
                }
                clips[tid] = clip
                items.append(tid)
                bus.publish({"type": "clip", "clip": clip})
                bus.publish({"type": "scanning", "name": info["name"], "count": i})

            # Per clip: codec/colour space + debayered thumbnail (the slow part).
            for tid in items:
                if _discover_stop.is_set():
                    break
                clip = clips[tid]

                def _on_info(codec, input_cs, clip=clip):
                    cls = media_scan.classify(codec, clip["ext"])
                    clip.update(camera=cls["camera"], raw=cls["raw"],
                                src_codec=cls["src_codec"], input_cs=input_cs)
                    bus.publish({"type": "clip", "clip": clip})

                poster_name = f"{tid}.jpg"
                pr = {"ok": False, "codec": "", "input_cs": ""}
                with contextlib.suppress(Exception):
                    pr = eng.render_poster(conn, qm, clip["src"], str(POSTERS / poster_name),
                                           on_info=_on_info)
                clip["poster_url"] = f"/posters/{poster_name}" if pr.get("ok") else None
                clip["thumb_state"] = "ready" if pr.get("ok") else "none"
                bus.publish({"type": "clip", "clip": clip})
    except Exception as e:  # noqa: BLE001
        bus.publish({"type": "discover_error", "error": str(e)})
    finally:
        bus.publish({"type": "discover_done", "total": len(clips)})


# --------------------------------------------------------------------------- #
# Scan tab — transcode
# --------------------------------------------------------------------------- #
@app.post("/api/transcode")
async def api_transcode(request: Request) -> JSONResponse:
    body = await request.json()
    deliverable_key = (body or {}).get("deliverable") or (body or {}).get("preset", "")
    ids = (body or {}).get("ids")  # omit/empty => all idle/done clips
    colourspace = (body or {}).get("colourspace") or None

    try:
        caps = _capabilities()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"could not query build capabilities: {e}"}, status_code=500)
    spec = next((d for d in caps.get("deliverables", []) if d["key"] == deliverable_key), None)
    if spec is None:
        return JSONResponse({"error": f"deliverable '{deliverable_key}' not supported by this build"},
                            status_code=400)
    # Validate the chosen colourspace against the build (fall back to default).
    cs_keys = {c["key"] for c in (caps.get("colourspaces") or [])}
    if colourspace and colourspace not in cs_keys:
        colourspace = None

    if not ids:
        ids = [cid for cid, c in clips.items() if c["state"] in ("idle", "done", "failed")]
    queued = pool.enqueue(ids, spec, colourspace)
    return JSONResponse({"ok": True, "queued": queued})


@app.post("/api/cancel")
def api_cancel() -> JSONResponse:
    pool.cancel()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- #
# flapid process lifecycle (Environment shows these; Teardown can kill them)
# --------------------------------------------------------------------------- #
@app.get("/api/procs")
def api_procs() -> JSONResponse:
    from . import flapi_engine as eng
    return JSONResponse({"procs": eng.list_procs(), "service": _get_service().status()})


@app.post("/api/kill")
async def api_kill(request: Request) -> JSONResponse:
    from . import flapi_engine as eng
    body = await request.json()
    pid = (body or {}).get("pid")
    svc = _get_service()
    if pid:
        pid = int(pid)
        if svc.pid() == pid:      # the search flapid: reset the handle too
            svc.drop()
        else:
            eng.kill_proc(pid)
        return JSONResponse({"ok": True})
    # kill all: render workers + the search flapid
    for p in eng.list_procs():
        if p["pid"] == svc.pid():
            svc.drop()
        else:
            eng.kill_proc(p["pid"])
    return JSONResponse({"ok": True})


@app.post("/api/delete_transcodes")
def api_delete_transcodes() -> JSONResponse:
    n = pool.reset_transcodes()
    return JSONResponse({"ok": True, "cleared": n})


# --------------------------------------------------------------------------- #
# Output serving (range support for in-browser scrubbing)
# --------------------------------------------------------------------------- #
@app.get("/outputs/{name}")
def output_file(name: str, request: Request):
    p = Path(app.state.out_dir) / name
    if not p.is_file():
        return Response(status_code=404)
    return _ranged_file(p, request)


def _ranged_file(p: Path, request: Request):
    file_size = p.stat().st_size
    mime, _ = mimetypes.guess_type(str(p))
    mime = mime or "application/octet-stream"
    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(str(p), media_type=mime)
    try:
        _units, rng = range_header.split("=")
        start_s, end_s = rng.split("-")
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
    except Exception:
        return FileResponse(str(p), media_type=mime)
    end = min(end, file_size - 1)
    length = end - start + 1

    def gen():
        with open(p, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(gen(), status_code=206, media_type=mime, headers=headers)


def main() -> None:
    import uvicorn
    port = int(os.environ.get("TCDEMO_PORT", "8080"))
    host = os.environ.get("TCDEMO_HOST", "0.0.0.0")
    print(f"Transcode demo on http://{host}:{port}  (outputs -> {DEFAULT_OUT})", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
