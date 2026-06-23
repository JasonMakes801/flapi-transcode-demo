"""End-to-end happy path: drive the real browser UI through the whole flow.

A guided tour of the demo, asserted at every step:
  1. Environment comes up licensed (our own child flapid).
  2. Scan a media folder -> clips appear.
  3. The live FLAPI-call log shows real calls streaming.
  4. Pick a deliverable, transcode one clip -> it reaches "done".
  5. The rendered file is really on disk (non-empty).
  6. The player opens and the proxy actually plays.
  7. Delete-all clears the outputs (rows reset, files gone).
  8. Teardown lists flapids, Kill-all stops them.
  9. A fresh Scan respawns what it needs and finds clips again.

Run it headed (and slowed down) to watch it happen:
    ./run_e2e.sh
or directly:
    pytest tests/test_e2e_happy_path.py --headed --slowmo 500
"""
import re

from playwright.sync_api import expect

MOVIE_SUFFIXES = {".mp4", ".mov", ".m4v", ".mxf"}


def _output_files(out_dir):
    return [p for p in out_dir.iterdir()
            if p.is_file() and p.suffix.lower() in MOVIE_SUFFIXES]


def test_full_flow(page, app_url, out_dir, media_dir):
    page.set_default_timeout(15_000)
    page.goto(app_url)

    # 1. Environment tab: our own child flapid spins up and reports licensed.
    #    Generous timeout — covers flapid launch + the capability probe.
    expect(page.get_by_test_id("led-licensed")).to_have_class(
        re.compile(r"\bon\b"), timeout=90_000)

    # 2. Scan the media folder; clips stream in over SSE.
    page.get_by_test_id("tab-scan").click()
    page.get_by_test_id("scan-dir").fill(media_dir)
    page.get_by_test_id("scan-btn").click()

    rows = page.get_by_test_id("clip-row")
    expect(rows.first).to_be_visible(timeout=60_000)
    expect(rows.first).to_have_attribute("data-state", "idle", timeout=60_000)

    # 3. The live FLAPI-call log has real calls (probe + scan already happened).
    page.get_by_test_id("drawer").click()                      # expand the drawer
    expect(page.get_by_test_id("logline").first).to_be_visible(timeout=10_000)
    expect(page.get_by_test_id("drawer-count")).not_to_have_text("0 calls")
    page.get_by_test_id("drawer").click()                      # collapse again

    # 4. Choose a deliverable and transcode the first clip (real GPU debayer+encode).
    page.get_by_test_id("deliverable").select_option("h264_web")
    first = rows.first
    first.get_by_test_id("row-transcode").click()
    expect(first).to_have_attribute("data-state", "done", timeout=180_000)

    # 5. The output really landed on disk, non-empty.
    files = _output_files(out_dir)
    assert files, f"no rendered movie in {out_dir}"
    assert files[0].stat().st_size > 0, "rendered movie is empty"

    # 6. Open the player and confirm the proxy plays (currentTime advances).
    first.get_by_role("link", name=re.compile("play")).click()
    video = page.get_by_test_id("player-video")
    expect(video).to_be_visible()
    video.evaluate("async v => { try { await v.play() } catch (e) {} }")
    page.wait_for_timeout(1_500)
    assert video.evaluate("v => v.currentTime") > 0, "player did not advance — proxy not playing"
    page.get_by_test_id("player-close").click()

    # 7. Delete all transcodes — rows reset to idle, files removed.
    page.get_by_test_id("delete-all").click()
    expect(first).to_have_attribute("data-state", "idle", timeout=15_000)
    assert not _output_files(out_dir), "outputs not cleared by delete-all"

    # 8. Teardown tab: flapids are listed; Kill-all stops them.
    page.get_by_test_id("tab-teardown").click()
    visible_procs = page.locator('[data-testid="proc-row"]:visible')
    expect(visible_procs.first).to_be_visible(timeout=10_000)
    page.get_by_test_id("kill-all").click()
    expect(visible_procs).to_have_count(0, timeout=15_000)

    # 9. A fresh Scan respawns the flapid it needs and finds clips again.
    page.get_by_test_id("tab-scan").click()
    page.get_by_test_id("scan-btn").click()
    expect(rows.first).to_have_attribute("data-state", "idle", timeout=60_000)
