#!/usr/bin/env python3
"""Pre-bake per-panel cast.mp4 files for Google Cast (Default Media Receiver).

For each panel that has both an image and a narration MP3, downloads the assets
from the live Worker, bakes a 1080p landscape MP4 (still image + audio) with
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
import urllib.parse
import urllib.request
import uuid
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

FFMPEG = "/opt/homebrew/bin/ffmpeg"
R2_BUCKET = "parkins-ai-walkthroughs"
WORKER_BASE = "https://walkthrough.parkins.ai"

# The Worker 403s urllib's default user-agent, so every request sets this.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; prebake-cast/1.0)"}

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

# Cast caps *image* media at 720p, but this bakes **video**, which is not capped
# there — H.264 1080p plays across the Chromecast line. Baking at 1080p is what
# lets a panel actually use the TV's landscape screen. Sources smaller than this
# are padded into the frame, never upscaled past their own resolution.
FRAME_W = 1920
FRAME_H = 1080
# Bumped when the bake changes in a way that makes existing files stale.
# Written into the MP4 comment; with +faststart the moov atom carrying it sits at
# the head of the file, so a small Range read can tell current from stale.
BAKE_STAMP = f"castbake-v3-{FRAME_W}x{FRAME_H}"


# Walkthroughs with an entry_code serve a *stub* manifest with no panels[] unless
# the right ?code= is supplied. That is why Munich and the wedding were never
# baked even once the hardcoded list was gone: the run saw zero panels and
# reported success. Codes are not in the stub (by design — the Worker strips
# entry_code before sending), so they come from the local authoring copy.
CODE_SEARCH_DIRS = ("content", "demo")


def local_entry_code(wid: str) -> str | None:
    """Entry code for a walkthrough, from whichever local manifest has one."""
    for d in CODE_SEARCH_DIRS:
        mf = Path(__file__).parent / d / wid / "manifest.json"
        if mf.is_file():
            try:
                code = json.loads(mf.read_text()).get("entry_code")
            except Exception:
                continue
            if code:
                return str(code)
    return None


def all_walkthroughs() -> list[str]:
    """Every walkthrough the live Worker knows about.

    Deliberately not a hardcoded list: one used to live here and went stale, so
    Munich and the wedding were silently never baked and cast 404'd on them."""
    req = urllib.request.Request(f"{WORKER_BASE}/api/walkthroughs", headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return [w["id"] for w in json.load(resp)]

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


def verify_uploaded(walkthrough_id: str, panel_id: str, expect_bytes: int) -> str | None:
    """Confirm the asset is really being served. Returns None on success, else why not.

    An upload that exits 0 proves nothing — wrangler can exit 0 unauthenticated,
    and a stale edge cache can answer for an object that was never written. So the
    check is made against the live route, cache-busted, and asserts the *size* the
    Worker reports rather than merely that something came back.

    Costs one KB, not one clip: the asset route honours Range, so a 0-1023 read
    returns 206 and a Content-Range carrying the true total length."""
    url = (f"{WORKER_BASE}/api/walkthroughs/{walkthrough_id}/asset/{panel_id}"
           f"/cast.mp4?v={uuid.uuid4().hex}")
    req = urllib.request.Request(url, headers={**_HEADERS, "Range": "bytes=0-1023"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            crange = resp.headers.get("Content-Range") or ""
            body_len = len(resp.read())
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}"
    except Exception as exc:
        return f"unreachable: {exc}"

    if status == 206 and "/" in crange:
        try:
            served = int(crange.rsplit("/", 1)[1])
        except ValueError:
            return f"unparseable Content-Range {crange!r}"
    elif status == 200:
        # Range ignored (older Worker): fall back to what actually arrived.
        served = body_len
    else:
        return f"unexpected status {status}"

    if served != expect_bytes:
        return f"served {served} B, expected {expect_bytes} B"
    # A JSON error body is ~21 bytes; a real clip is not.
    if served < 10240:
        return f"implausibly small ({served} B)"
    return None


def probe_served(walkthrough_id: str, panel_id: str) -> tuple[bool, str]:
    """Is this panel's cast.mp4 actually being served? Returns (ok, detail).

    Used by --verify-only to audit a walkthrough without re-baking it. The bar is
    the one the job was set: HTTP 200/206 and a plausible size, not a 21-byte
    JSON error. One KB on the wire per panel, via Range."""
    url = (f"{WORKER_BASE}/api/walkthroughs/{walkthrough_id}/asset/{panel_id}"
           f"/cast.mp4?v={uuid.uuid4().hex}")
    req = urllib.request.Request(url, headers={**_HEADERS, "Range": "bytes=0-1023"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            crange = resp.headers.get("Content-Range") or ""
            served = (int(crange.rsplit("/", 1)[1])
                      if resp.status == 206 and "/" in crange else len(resp.read()))
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"unreachable: {exc}"
    if served < 10240:
        return False, f"implausibly small ({served} B)"
    return True, f"{served // 1024} KB"


def verify_only(walkthrough_id: str, entry_code: str | None) -> tuple[int, int]:
    """Audit every panel of a walkthrough over HTTP. Returns (verified, missing)."""
    code = entry_code or local_entry_code(walkthrough_id)
    url = f"{WORKER_BASE}/api/walkthroughs/{walkthrough_id}"
    if code:
        url += f"?code={urllib.parse.quote(code)}"
    manifest = fetch_json(url)
    if manifest.get("code_required"):
        log(f"  ERROR: {walkthrough_id} is gated and no code was found.")
        return 0, 0
    panels = manifest.get("panels", [])
    good = bad = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(probe_served, walkthrough_id, p["id"]): p["id"] for p in panels}
        for fut in as_completed(futs):
            ok, detail = fut.result()
            if ok:
                good += 1
            else:
                bad += 1
                log(f"    MISSING [{futs[fut]}] — {detail}")
    log(f"  {walkthrough_id}: {good}/{len(panels)} verified over HTTP, {bad} missing")
    return good, bad


# ── ffmpeg bake ───────────────────────────────────────────────────────────────

def _normalize(image: Path, out: Path) -> bool:
    """Scale+pad a single image to a FRAME_W×FRAME_H frame so all slides share the same
    dimensions (required for the concat demuxer to stitch them)."""
    cmd = [
        FFMPEG, "-y", "-i", str(image),
        "-vf", (
            # min(FRAME,i*) gives the box a lower bound, so this only ever
            # scales *down*. Without it, force_original_aspect_ratio=decrease
            # happily enlarges a small source to fill the frame — a 640x480
            # photo came out at 1440x1080, i.e. 2.25x of invented detail.
            # Sources under 1080p now sit at native size inside the pad.
            f"scale=w='min({FRAME_W},iw)':h='min({FRAME_H},ih)'"
            f":force_original_aspect_ratio=decrease:force_divisible_by=2,"
            f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        ),
        "-frames:v", "1", str(out),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log(f"    ffmpeg normalize error: {result.stderr.decode()[-400:]}")
    return result.returncode == 0 and out.stat().st_size > 0


def bake_cast(images: list[Path], audio: Path | None, out: Path, workdir: Path) -> bool:
    """Bake a FRAME_W×FRAME_H landscape MP4 that rotates through the panel's images at
    PER_IMAGE_SECONDS each (matching the web carousel), looping to fill
    HOLD_SECONDS. Audio plays once at the start, then silence — so casting keeps
    the picture rotating with no video→photo flicker. When there is no narration
    (audio is None) the clip is baked silent — the whole point on Cast is a clean,
    flicker-free slideshow that holds each photo for a fixed interval and sits on
    the panel until the viewer advances.

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
    ]
    if audio is not None:
        cmd += ["-i", str(audio)]
    cmd += [
        # The concat demuxer's last frame has no successor, so the video stream
        # stopped one frame short: 599 frames for a 600 s clip. With narration,
        # -af apad padded the audio to the full 600 s, leaving a final second of
        # audio with no video behind it — a gap the receiver has to guess at.
        # Cloning the last frame past the end lets -t truncate all streams on the
        # same timestamp instead.
        # ...and fps= re-times the result onto a strict CFR grid. tpad alone was
        # not enough: the concat demuxer's timestamps meant -t still cut the
        # video early (measured: 599 frames / 599.000 s against 600.000 s of
        # audio). tpad,fps together give 600 frames and one end timestamp for
        # video, audio and container alike.
        "-vf", f"tpad=stop_mode=clone:stop_duration=2,fps={HOLD_FPS}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-r", str(HOLD_FPS),
        "-crf", "28", "-preset", "veryfast",
        "-x264-params", "keyint=99999:scenecut=0:ref=16:bframes=0",
    ]
    if audio is not None:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-af", "apad"]
    cmd += [
        "-t", str(HOLD_SECONDS),
        # Stamp what this bake is, and put the moov atom at the head so the
        # stamp can be read from the first few KB rather than by pulling the
        # whole file. Faststart also helps the receiver start playing sooner.
        "-metadata", f"comment={BAKE_STAMP}",
        "-movflags", "+faststart",
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

    if not image_files:
        return f"[{panel_id}] SKIP — no images (images={image_files})"

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

        # Narration is optional: with it, the audio plays once; without it, the
        # clip is a silent flicker-free slideshow.
        audio_arg: Path | None = None
        if narration_file:
            if not download_file(f"{base}/{narration_file}", nar_local):
                return f"[{panel_id}] FAIL — narration download"
            audio_arg = nar_local

        if not bake_cast(image_locals, audio_arg, cast_local, tmpdir):
            return f"[{panel_id}] FAIL — ffmpeg"

        local_bytes = cast_local.stat().st_size
        size_kb = local_bytes // 1024
        r2_key = f"walkthroughs/{walkthrough_id}/panels/{panel_id}/cast.mp4"
        try:
            r2_upload(cast_local, r2_key, cf_token)
        except subprocess.CalledProcessError:
            return f"[{panel_id}] FAIL — R2 upload"

        # Not "uploaded" — *serving*. This is the only line that earns the word.
        why = verify_uploaded(walkthrough_id, panel_id, local_bytes)
        if why:
            return f"[{panel_id}] FAIL — not served after upload ({why})"

    title = panel.get("title", panel_id)
    return f"[{panel_id}] OK — {title} ({size_kb} KB)"


# ── Walkthrough runner ────────────────────────────────────────────────────────

def process_walkthrough(
    walkthrough_id: str,
    panel_filter: str | None,
    jobs: int,
    cf_token: str,
    entry_code: str | None = None,
) -> tuple[int, int, int]:
    """Returns (ok, skipped, failed) counts."""
    log(f"\n{'='*60}")
    log(f"Walkthrough: {walkthrough_id}")
    log(f"{'='*60}")

    code = entry_code or local_entry_code(walkthrough_id)
    manifest_url = f"{WORKER_BASE}/api/walkthroughs/{walkthrough_id}"
    if code:
        manifest_url += f"?code={urllib.parse.quote(code)}"

    try:
        manifest = fetch_json(manifest_url)
    except Exception as exc:
        log(f"  ERROR: could not fetch manifest: {exc}")
        return 0, 0, 1

    # Loudly, not silently. A gated walkthrough answers with a stub and no
    # panels; treating that as "nothing to do" is precisely the silent gap this
    # whole exercise exists to close.
    if manifest.get("code_required"):
        log(f"  ERROR: {walkthrough_id} is entry-code gated and no code was found.")
        log(f"         Pass --code, or add entry_code to a local manifest under "
            f"{'/, '.join(CODE_SEARCH_DIRS)}/{walkthrough_id}/manifest.json")
        return 0, 0, 1

    panels = manifest.get("panels", [])
    if not panels:
        log(f"  ERROR: {walkthrough_id} returned a manifest with no panels.")
        return 0, 0, 1
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
    log(f"\n  → {ok} verified over HTTP, {skip} skipped (no nar/image), {fail} failed")

    # The counts must add up to the panels we set out to do. If they do not,
    # something was dropped silently and the run is not a success.
    if ok + skip + fail != len(panels):
        log(f"  ERROR: {len(panels)} panels in, but only {ok + skip + fail} accounted for.")
        fail += len(panels) - (ok + skip + fail)
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
    parser.add_argument("--verify-only", action="store_true",
                        help="Audit which panels are actually served; bake nothing")
    parser.add_argument("--code", default=None,
                        help="Entry code for a gated walkthrough (default: read "
                             "from the local authoring manifest)")
    args = parser.parse_args()

    walkthroughs = [args.walkthrough] if args.walkthrough else all_walkthroughs()

    if args.verify_only:
        log("Verifying served cast.mp4 assets (no baking)\n")
        tg = tb = 0
        for wid in walkthroughs:
            g, b = verify_only(wid, args.code)
            tg += g; tb += b
        log(f"\nTOTAL: {tg} verified, {tb} missing")
        sys.exit(1 if tb else 0)

    cf_token = get_cf_token()

    total_ok = total_skip = total_fail = 0
    for wid in walkthroughs:
        ok, skip, fail = process_walkthrough(
            wid, args.panel, args.jobs, cf_token, args.code)
        total_ok   += ok
        total_skip += skip
        total_fail += fail

    log(f"\n{'='*60}")
    log(f"TOTAL: {total_ok} baked, {total_skip} skipped, {total_fail} failed")
    log(f"{'='*60}")

    # A run that baked nothing at all is a failure, not a no-op. This is the
    # guard that would have caught Munich and the wedding on day one: the gated
    # manifest returned zero panels, so zero were baked, and the run exited 0.
    if total_ok == 0 and total_skip == 0:
        log("ERROR: nothing was baked and nothing was skipped — "
            "the run did no work. Treating as failure.")
        sys.exit(1)

    if total_fail > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
