"""Fixtures for the end-to-end UI test.

Boots the real FastAPI app in a uvicorn subprocess (its own temp state/output
dirs so it doesn't touch ~/transcode_demo_output), waits until it answers, and
hands the test a base URL. The app warms its own child flapid on startup, so
this needs a licensed Baselight on the machine — same constraint as the manual
smoke test, just push-button.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
# The folder of short camera clips the demo was developed against. Override with
# TCDEMO_TEST_MEDIA. If it's missing the e2e test skips rather than fails.
DEFAULT_MEDIA = "/Volumes/Extreme SSD/transcode_demo_media"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="session")
def media_dir() -> str:
    d = os.environ.get("TCDEMO_TEST_MEDIA", DEFAULT_MEDIA)
    if not Path(d).is_dir():
        pytest.skip(f"no test media at {d} — set TCDEMO_TEST_MEDIA to a folder of camera clips")
    return d


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    """A running instance of the demo. Yields .url and .out_dir; tears it down.

    Output and state go to throwaway temp dirs, so the test never touches your
    real ~/transcode_demo_output.
    """
    port = _free_port()
    out_dir = tmp_path_factory.mktemp("out")
    env = dict(
        os.environ,
        TCDEMO_STATE=str(tmp_path_factory.mktemp("state")),
        TCDEMO_OUT=str(out_dir),
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "transcode_demo.server:app",
         "--app-dir", str(SRC), "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited early:\n" + (proc.stdout.read() if proc.stdout else ""))
        try:
            urllib.request.urlopen(url + "/api/config", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    else:
        proc.terminate()
        raise RuntimeError("server did not become ready within 30s")

    yield SimpleNamespace(url=url, out_dir=out_dir)

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()


@pytest.fixture()
def app_url(server) -> str:
    return server.url


@pytest.fixture()
def out_dir(server) -> Path:
    return server.out_dir
