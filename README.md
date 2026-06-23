# FilmLight FLAPI Transcode Demo

A small, portable web GUI that drives the FilmLight render API (**FLAPI**) to do
**mass transcodes** of camera originals to colour-managed editorial proxies — the
way large facilities (e.g. Netflix) run them: a pool of render workers, each
spinning up its **own child `flapid`**, fed from a queue. No running Baselight app
is required; the demo launches its own licensed render service.

The point isn't to re-create the Baselight render manager — it's to show how
little code it takes to call our render API from *your own* application (the
Konsole / Illusion integration question), with wide camera-format support that
replaces Transkoder.

Four tabs, one page:

1. **Environment** — spins up a child `flapid`, confirms it's licensed, and reports
   the build + which destination formats and colourspaces it can produce.
2. **Scan** — point at a folder; FLAPI's `ImageSearcher` finds and groups camera
   clips (with an `os.walk` fallback), popping colour-managed thumbnails as it goes.
   Pick a deliverable + output colourspace (applies to all clips) and transcode any
   row, or all.
3. **Teardown** — watch the `flapid` processes spin up, idle-time-out, or kill them;
   Scan/Transcode respawn what they need.
4. **About** — the (deliberately tiny) stack, and the throughput model.

A live **FLAPI call log** streams along the bottom — the actual calls as they run.

The aesthetic (dark theme, FilmLight blue, Hero fonts, Alpine.js) is reused from
the multimodal-search UI; the engine code from the multimedia enrichment project;
the environment probing from the flapi-dev MCP.

## Requirements

- macOS with **Baselight 7** installed and **licensed** (the Environment tab
  verifies this). The demo launches its own child `flapid` — you do **not** need a
  Baselight GUI running.
- `enablefloplocal` present (ships with Baselight; needed for the local
  no-database render queue used for thumbnails).
- Python **3.10 or 3.11** (stock `python3` + `pip` — no `uv`, Poetry, or Docker).

No Docker: the app talks to the host's licensed `flapid` and GPU, and the
`filmlightapi` wheel comes from the local Baselight build, not PyPI.

## Install & run

```bash
git clone <repo-url> && cd transcode-with-webui
./bootstrap.sh        # makes .venv, installs the app + the build's flapi wheel (auto-detected)
./run.sh              # → http://localhost:8080
```

Then open `http://localhost:8080` (or `http://<this-mac>:8080` from another machine).
To update later: `git pull` (re-run `./bootstrap.sh` only if dependencies changed).

Override the wheel if auto-detection misses it:

```bash
FLAPI_WHEEL=/Applications/Baselight/Current/*.app/Contents/share/flapi/python/filmlightapi-*.whl ./bootstrap.sh
```

### Environment variables

| Var              | Default                   | Meaning                                        |
|------------------|---------------------------|------------------------------------------------|
| `TCDEMO_PORT`    | `8080`                    | web server port                                |
| `TCDEMO_HOST`    | `0.0.0.0`                 | bind address                                   |
| `TCDEMO_OUT`     | `~/transcode_demo_output` | rendered output directory (also set in the UI) |
| `TCDEMO_STATE`   | `~/.transcode-demo`       | thumbnail / poster cache dir                   |
| `TCDEMO_WORKERS` | `1`                       | render workers — one child `flapid` per GPU    |
| `FLAPI_WHEEL`    | auto-detected             | force a specific flapi wheel                   |

## Bring your own media

Point the **Scan** tab at a folder of camera originals (ARRIRAW, R3D, BRAW, Canon
Cinema RAW Light, Sony X-OCN, ProRes, etc.). Keep clips short for a snappy demo.

## Throughput — scales with GPUs, not CPU cores

The debayer + colour transform + encode all run on the **GPU**, so render
throughput scales with the number of **GPUs**, not CPU cores. On a multi-GPU
render node each child `flapid` claims its own GPU and they render in parallel;
on a single-GPU machine (e.g. any Apple Silicon Mac) they funnel to one GPU and
run one at a time — hence the default of **1 worker** (one per GPU). Raise
`TCDEMO_WORKERS` on a multi-GPU host.

## Architecture notes

- **Connections are single-threaded.** flapi connections aren't thread-safe, so a
  persistent **search** `flapid` owns metadata + thumbnails, and each render worker
  launches its **own child `flapid`** and does one render per child (then tears it
  down).
- **Scanning** uses FLAPI's `ImageSearcher` to find and group camera clips with
  real source resolutions; if it can't read a path (e.g. a non-canonical path), it
  falls back to a plain `os.walk` crawl so the scan still works.
- **Deliverables & colourspaces** are reconciled against the live build with
  `RenderSetup.get_movie_types()` / `get_movie_codecs()` and `FormatSet`, so the
  dropdowns only offer what this Baselight can actually produce.

## Tests

From the repo dir, after `./bootstrap.sh`:

```bash
source .venv/bin/activate               # activate the venv bootstrap made
pip install -e ".[test]"                # one-time: pytest + pytest-playwright
python -m playwright install chromium   # one-time: the browser the e2e test drives

pytest tests/test_scan_fallback.py      # fast, licence-free unit tests (the crawl fallback)
./run_e2e.sh                            # end-to-end: drives the real UI headed so you can watch
```

The end-to-end test boots the app and drives a real browser through the whole flow
(environment → scan → transcode → player → delete-all → teardown → respawn). It
needs a licensed Baselight and a folder of test media (`TCDEMO_TEST_MEDIA`); it
skips cleanly if the media isn't present.

## A demo, not production

Single-user, state lives in memory. It's a reference integration to show the API
surface, not a hardened service — but it demos cleanly.
