#!/usr/bin/env python3
"""Pre-bake per-panel cast.mp4 files for Google Cast (Default Media Receiver).

For each panel that has both an image and a narration MP3, downloads the assets
from the live Worker, bakes a 1280×720 landscape MP4 (still image + audio) with
ffmpeg, and uploads the result to R2 as cast.mp4.

The existing asset route GET /api/walkthroughs/{id}/asset/{panelId}/cast.mp4
serves the result — no Worker changes needed.

Usage:
    python3 prebake_cast.py                             # all four walkthroughs
    python3 prebake_cast.py --walkthrough computing-heroes
    python3 prebake_cast.py --walkthrough computing-heroes --panel lovelace
    python3 prebake_cast.py --jobs 6                    # parallel workers (default 4)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

FFMPEG = "/opt/homebrew/bin/ffmpeg"
R2_BUCKET = "parkins-ai-walkthroughs"
WORKER_BASE = "https://walkthrough.parkins.ai"

# The still image is held in the video well past the narration audio so that on
# Cast the receiver never switches from video-mode to photo-mode (that switch is
# what causes the one-off flicker at narration end). One continuous clip shows
# the panel picture for HOLD_SECONDS — effectively until the viewer advances —
# with the audio playing once at the start, then silence. A low frame rate keeps
# the long still cheap to encode and tiny on disk.
HOLD_SECONDS = 600
HOLD_FPS = 1
# Seconds each image is shown before rotating to the next — matches the web
# carousel's 5s auto-advance so cast and web stay in step.
PER_IMAGE_SECONDS = 5

ALL_WALKTHROUGHS = [
    "computing-heroes",
    "natural-history-museum-2026",
    "poem-anthology",
    "science-museum",
]

_print_lock = threading.Lock()


def log(*args) -> None:
    with _print_lock:
        print(*args, flush=True)


# ── Credentials ──────────────────────────────────────────────────────────────

def get_cf_token() -> str:
    token = subprocess.check_output(
        ["security", "find-generic-password", "-s", "eli/cloudflare-parkins-ai-token", "-w"],
        text=True,
    ).strip()
    if not token:
        raise RuntimeError("Cloudflare token not found in keychain")
    return token


# ── Network helpers ──────────────────────────────────────────────────────────

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; prebake-cast/1.0)"}


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def download_file(url: str, dest: Path) -> bool:
    """Download url to dest. Returns True on success."""
    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        return dest.stat().st_size > 0
    except Exception as exc:
        log(f"    download failed {url}: {exc}")
        return False


# ── R2 upload ────────────────────────────────────────────────────────────────

def r2_upload(local: Path, r2_key: str, cf_token: str) -> None:
    cmd = (
        f'npx wrangler r2 object put "{R2_BUCKET}/{r2_key}"'
        f' --file "{local}" --content-type video/mp4 --remote'
    )
    env = {**os.environ, "CLOUDFLARE_API_TOKEN": cf_token}
    subprocess.check_call(
        cmd, shell=True, cwd=Path(__file__).parent, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


# ── ffmpeg bake ───────────────────────────────────────────────────────────────

def _normalize(image: Path, out: Path) -> bool:
    """Scale+pad a single image to a 1280×720 frame so all slides share the same
    dimensions (required for the concat demuxer to stitch them)."""
    cmd = [
        FFMPEG, "-y", "-i", str(image),
        "-vf", (
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1"
        ),
        "-frames:v", "1", str(out),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log(f"    ffmpeg normalize error: {result.stderr.decode()[-400:]}")
    return result.returncode == 0 and out.stat().st_size > 0


def bake_cast(images: list[Path], audio: Path, out: Path, workdir: Path) -> bool:
    """Bake a 1280×720 landscape MP4 that rotates through the panel's images at
    PER_IMAGE_SECONDS each (matching the web carousel), looping to fill
    HOLD_SECONDS. Audio plays once at the start, then silence — so casting keeps
    the picture rotating with no video→photo flicker.

    A single image collapses to the previous behaviour: one still held for the
    whole duration."""
    # Normalize every image to identical 1280×720 frames.
    norms: list[Path] = []
    for idx, img in enumerate(images):
        norm = workdir / f"norm_{idx}.png"
        if not _normalize(img, norm):
            return False
        norms.append(norm)

    # Build a concat list that repeats the image cycle enough times to exceed
    # HOLD_SECONDS; the final -t truncates to the exact hold length.
    n = len(norms)
    cycle_len = n * PER_IMAGE_SECONDS
    cycles = max(1, (HOLD_SECONDS // cycle_len) + 1)
    list_path = workdir / "slides.txt"
    lines: list[str] = []
    for _ in range(cycles):
        for norm in norms:
            lines.append(f"file '{norm.as_posix()}'")
            lines.append(f"duration {PER_IMAGE_SECONDS}")
    # concat demuxer needs the last file repeated (without duration) to flush it.
    lines.append(f"file '{norms[-1].as_posix()}'")
    list_path.write_text("\n".join(lines) + "\n")

    # The cycle repeats the same handful of images for the full hold. Disabling
    # scene-cut and forcing a single GOP means x264 emits one keyframe for each
    # unique image's first appearance; every later repeat is a near-zero P-frame
    # that references the identical earlier frame. Without this each of the ~120
    # transitions became its own fat keyframe, bloating the file to ~40 MB.
    cmd = [
        FFMPEG, "-y",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-i", str(audio),
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", str(HOLD_FPS),
        "-crf", "28", "-preset", "veryfast",
        "-x264-params", "keyint=99999:scenecut=0:ref=16:bframes=0",
        "-c:a", "aac", "-b:a", "128k",
        "-af", "apad",
        "-t", str(HOLD_SECONDS),
        str(out),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log(f"    ffmpeg error: {result.stderr.decode()[-400:]}")
    return result.returncode == 0


# ── Per-panel processing ─────────────────────────────────────────────────────

def process_panel(walkthrough_id: str, panel_id: str, cf_token: str) -> str:
    """Process one panel. Returns a status string."""
    panel_url = f"{WORKER_BASE}/api/walkthroughs/{walkthrough_id}/panels/{panel_id}"
    try:
        panel = fetch_json(panel_url)
    except Exception as exc:
        return f"[{panel_id}] SKIP — panel.json unreachable: {exc}"

    # Resolve the full image list — prefer images[] array (rotates on cast just
    # like the web carousel), fall back to the single image field.
    image_files: list[str] = []
    if panel.get("images") and len(panel["images"]) > 0:
        image_files = list(panel["images"])
    elif panel.get("image"):
        image_files = [panel["image"]]

    narration_file: str | None = panel.get("narration")

    if not image_files or not narration_file:
        return f"[{panel_id}] SKIP — no image or narration (images={image_files}, nar={narration_file})"

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        nar_local   = tmpdir / "narration.mp3"
        cast_local  = tmpdir / "cast.mp4"

        base = f"{WORKER_BASE}/api/walkthroughs/{walkthrough_id}/asset/{panel_id}"

        image_locals: list[Path] = []
        for idx, image_file in enumerate(image_files):
            img_ext = Path(image_file).suffix or ".jpg"
            image_local = tmpdir / f"image_{idx}{img_ext}"
            if not download_file(f"{base}/{image_file}", image_local):
                return f"[{panel_id}] FAIL — image download ({image_file})"
            image_locals.append(image_local)

        if not download_file(f"{base}/{narration_file}", nar_local):
            return f"[{panel_id}] FAIL — narration download"

        if not bake_cast(image_locals, nar_local, cast_local, tmpdir):
            return f"[{panel_id}] FAIL — ffmpeg"

        size_kb = cast_local.stat().st_size // 1024
        r2_key = f"walkthroughs/{walkthrough_id}/panels/{panel_id}/cast.mp4"
        try:
            r2_upload(cast_local, r2_key, cf_token)
        except subprocess.CalledProcessError:
            return f"[{panel_id}] FAIL — R2 upload"

    title = panel.get("title", panel_id)
    return f"[{panel_id}] OK — {title} ({size_kb} KB)"


# ── Walkthrough runner ────────────────────────────────────────────────────────

def process_walkthrough(
    walkthrough_id: str,
    panel_filter: str | None,
    jobs: int,
    cf_token: str,
) -> tuple[int, int, int]:
    """Returns (ok, skipped, failed) counts."""
    log(f"\n{'='*60}")
    log(f"Walkthrough: {walkthrough_id}")
    log(f"{'='*60}")

    try:
        manifest = fetch_json(f"{WORKER_BASE}/api/walkthroughs/{walkthrough_id}")
    except Exception as exc:
        log(f"  ERROR: could not fetch manifest: {exc}")
        return 0, 0, 0

    panels = manifest.get("panels", [])
    if panel_filter:
        panels = [p for p in panels if p["id"] == panel_filter]
        if not panels:
            log(f"  Panel '{panel_filter}' not found in manifest.")
            return 0, 0, 0

    log(f"  {len(panels)} panels (jobs={jobs})")

    results: list[str] = []

    if jobs == 1:
        for p in panels:
            result = process_panel(walkthrough_id, p["id"], cf_token)
            log(f"  {result}")
            results.append(result)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futs = {
                pool.submit(process_panel, walkthrough_id, p["id"], cf_token): p["id"]
                for p in panels
            }
            for fut in as_completed(futs):
                result = fut.result()
                log(f"  {result}")
                results.append(result)

    ok    = sum(1 for r in results if " OK "   in r)
    skip  = sum(1 for r in results if " SKIP " in r)
    fail  = sum(1 for r in results if " FAIL " in r)
    log(f"\n  → {ok} baked, {skip} skipped (no nar/image), {fail} failed")
    return ok, skip, fail


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-bake cast.mp4 for Google Cast")
    parser.add_argument("--walkthrough", default=None,
                        help="Single walkthrough ID (default: all four)")
    parser.add_argument("--panel", default=None,
                        help="Single panel ID within the walkthrough")
    parser.add_argument("--jobs", type=int, default=4,
                        help="Parallel worker count (default: 4)")
    args = parser.parse_args()

    cf_token = get_cf_token()

    walkthroughs = [args.walkthrough] if args.walkthrough else ALL_WALKTHROUGHS

    total_ok = total_skip = total_fail = 0
    for wid in walkthroughs:
        ok, skip, fail = process_walkthrough(wid, args.panel, args.jobs, cf_token)
        total_ok   += ok
        total_skip += skip
        total_fail += fail

    log(f"\n{'='*60}")
    log(f"TOTAL: {total_ok} baked, {total_skip} skipped, {total_fail} failed")
    log(f"{'='*60}")

    if total_fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
