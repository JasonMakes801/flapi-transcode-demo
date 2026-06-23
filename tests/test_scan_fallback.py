"""Unit tests for the os.walk fallback in scan_directory.

The e2e happy path scans a folder where ImageSearcher succeeds, so it never
exercises the fallback. These do: with a fake `conn` that makes ImageSearcher
fail (or return nothing), scan_directory must drop to the plain directory crawl
and still return the media files. No flapid / licence / browser needed.
"""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from transcode_demo import flapi_engine as eng  # noqa: E402


class _FakeSeq:
    """Minimal stand-in for a SequenceDescriptor (used by the crawl's metadata read)."""
    def get_width(self): return 1920
    def get_height(self): return 1080
    def get_movie_fps(self): return 24.0
    def get_start_frame(self): return 0
    def get_end_frame(self): return 23
    def has_audio(self): return False
    def get_audio_channels(self): return 0
    def get_start_timecode(self): return "00:00:00:00"
    def release(self): pass


class _FakeSeqDesc:
    def get_for_file(self, src): return _FakeSeq()


class _FailingISR:
    """An ImageSearcher whose add_root_directory blows up (Peter's case)."""
    def add_root_directory(self, d, recurse):
        raise RuntimeError("Root dir is non-canonical")
    def release(self): pass


class _EmptyISR:
    """An ImageSearcher that runs fine but finds nothing."""
    def add_root_directory(self, d, recurse): return 1
    def scan(self): pass
    def is_scan_in_progress(self): return False
    def get_sequences(self, track, n): return []
    def release(self): pass


def _conn(isr):
    return type("FakeConn", (), {
        "ImageSearcher": type("F", (), {"create": staticmethod(lambda: isr)})(),
        "SequenceDescriptor": _FakeSeqDesc(),
    })()


def _media_tree(tmp_path):
    (tmp_path / "A001.braw").write_bytes(b"x")
    (tmp_path / "B002.r3d").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignore me")          # not media
    (tmp_path / "._A001.braw").write_bytes(b"x")              # AppleDouble stub
    return tmp_path


def test_fallback_when_imagesearcher_raises(tmp_path):
    _media_tree(tmp_path)
    clips = eng.scan_directory(_conn(_FailingISR()), str(tmp_path))
    names = sorted(c["name"] for c in clips)
    assert names == ["A001.braw", "B002.r3d"]                 # media found, junk skipped
    assert clips[0]["meta"]["width"] == 1920                  # metadata read via crawl


def test_fallback_when_imagesearcher_finds_nothing(tmp_path):
    _media_tree(tmp_path)
    clips = eng.scan_directory(_conn(_EmptyISR()), str(tmp_path))
    names = sorted(c["name"] for c in clips)
    assert names == ["A001.braw", "B002.r3d"]


def test_no_fallback_noise_on_empty_dir(tmp_path):
    # Empty (no media): ImageSearcher returns nothing, crawl finds nothing -> [].
    clips = eng.scan_directory(_conn(_EmptyISR()), str(tmp_path))
    assert clips == []
