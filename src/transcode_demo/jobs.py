"""Clip-centric render orchestration: a persistent worker pool + an SSE bus.

The UI is a single Scan table whose rows are *clips*; each clip carries its own
state (idle → queued → rendering → done/failed) and progress. A pool of daemon
workers drains a queue of clip ids — "Transcode" enqueues one clip, "Transcode
all" enqueues them all, uniformly. Each worker launches its own child flapid per
render (one render per child, torn down after), so the pool is the single-machine
version of the Netflix "many render nodes" model.

Throughput scales with GPUs, not CPU cores: the debayer + colour transform + encode
run on the GPU, so on a multi-GPU host each child flapid claims its own GPU and they
render in parallel, whereas on a single-GPU machine they all funnel to one GPU and
serialize. Hence the default of 1 worker (one render worker per GPU); set
TCDEMO_WORKERS higher on a multi-GPU render node.

State lives in a registry of plain dicts owned by the server; this module mutates
those dicts and publishes them to SSE subscribers.
"""
from __future__ import annotations

import contextlib
import os
import queue
import threading
from pathlib import Path

# One render worker per GPU. Renders are GPU-bound, so extra workers on a single
# GPU just queue — default 1, raise TCDEMO_WORKERS on a multi-GPU host.
try:
    NUM_WORKERS = max(1, int(os.environ.get("TCDEMO_WORKERS", "1")))
except ValueError:
    NUM_WORKERS = 1


class Broadcaster:
    """Fan a stream of dict events out to every subscribed queue."""

    def __init__(self) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.put(event)


class ClipPool:
    """Persistent 3-worker pool that transcodes clips from a shared registry.

    ``render_one(clip, preset, resolved, set_progress) -> dict`` is injected (so
    this module stays flapi-free); it runs on a worker thread, may launch its own
    child flapid, and returns keys: output, output_url, size_bytes, wall_seconds,
    out_codec, out_colourspace.
    """

    def __init__(self, bus: Broadcaster, render_one, num_workers: int = NUM_WORKERS) -> None:
        self.bus = bus
        self.render_one = render_one
        self.n = num_workers
        self.q: queue.Queue = queue.Queue()
        self.registry: dict[str, dict] = {}
        self._started = False
        self._lock = threading.Lock()
        self._cancel = threading.Event()

    def attach(self, registry: dict[str, dict]) -> None:
        self.registry = registry

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for i in range(self.n):
                threading.Thread(target=self._worker, args=(i,),
                                 name=f"tc-worker-{i}", daemon=True).start()

    def enqueue(self, ids: list[str], deliverable: dict, colourspace=None) -> int:
        """Queue the given clip ids for transcode. Returns how many were queued."""
        self._ensure_workers()
        self._cancel.clear()
        n = 0
        for cid in ids:
            c = self.registry.get(cid)
            if not c or c.get("state") in ("queued", "rendering"):
                continue
            c.update(state="queued", progress=0.0, error=None,
                     preset_key=deliverable.get("key"), out_codec=deliverable.get("label"))
            self.bus.publish({"type": "clip", "clip": c})
            self.q.put((cid, deliverable, colourspace))
            n += 1
        return n

    def _worker(self, wid: int) -> None:
        while True:
            cid, deliverable, colourspace = self.q.get()
            c = self.registry.get(cid)
            if c is None:
                self.q.task_done()
                continue
            c.update(state="rendering", progress=0.0, worker=wid)
            self.bus.publish({"type": "clip", "clip": c, "worker": wid})

            def set_progress(p: float, c=c, wid=wid) -> None:
                c["progress"] = p
                self.bus.publish({"type": "clip", "clip": c, "worker": wid})

            if self._cancel.is_set():
                c.update(state="idle", progress=0.0)
                self.bus.publish({"type": "clip", "clip": c, "worker": wid})
                self.q.task_done()
                continue
            try:
                res = self.render_one(c, deliverable, set_progress,
                                      lambda: self._cancel.is_set(), colourspace)
                c.update(state="done", progress=1.0,
                         output=res.get("output"), output_url=res.get("output_url"),
                         size_bytes=res.get("size_bytes"), wall_seconds=res.get("wall_seconds"),
                         out_codec=res.get("out_codec"), out_colourspace=res.get("out_colourspace"))
            except Exception as e:  # noqa: BLE001
                # A cancel mid-render returns the clip to idle, not failed.
                if self._cancel.is_set():
                    c.update(state="idle", progress=0.0, error=None)
                else:
                    c.update(state="failed", error=str(e))
            self.bus.publish({"type": "clip", "clip": c, "worker": wid})
            self.q.task_done()

    def cancel(self) -> None:
        """Stop transcoding: drain the queue, abort in-flight renders, and
        return every queued/rendering clip to idle."""
        self._cancel.set()
        with contextlib.suppress(Exception):
            while True:
                self.q.get_nowait()
                self.q.task_done()
        for c in self.registry.values():
            if c.get("state") in ("queued", "rendering"):
                c.update(state="idle", progress=0.0, error=None)
                self.bus.publish({"type": "clip", "clip": c})

    def reset_transcodes(self) -> int:
        """Delete every rendered output and reset clips to metadata-only.

        Makes re-demoing instant. Returns how many transcodes were cleared.
        """
        n = 0
        for c in self.registry.values():
            out = c.get("output")
            if out:
                with contextlib.suppress(Exception):
                    Path(out).unlink(missing_ok=True)
                n += 1
            c.update(state="idle", progress=0.0, output=None, output_url=None,
                     size_bytes=None, wall_seconds=None, error=None)
            self.bus.publish({"type": "clip", "clip": c})
        return n
