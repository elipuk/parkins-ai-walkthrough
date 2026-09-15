#!/usr/bin/env python3
"""walkthrough.py — feed the parkins-ai walkthrough system new panels.

Source of truth: `content/<id>/...` on disk (gitignored — these are
holiday photos). Serving copy: R2 bucket `parkins-ai-walkthroughs`.
This script writes both — local first, then syncs to R2.

The Worker (src/index.js) is read-only over R2, so no redeploy is
needed when content changes. Worker deploys are a separate concern.

Subcommands:
    add           Photos + commentary in, panel out — the auto-feed entry point
    create        Bootstrap a new walkthrough (idempotent; --force to overwrite)
    add-panel     Add a panel (image + panel.json + article.md) to an existing walkthrough
    remove-panel  Remove a panel from the manifest and R2
    set-active    Mark a walkthrough as the current default

Auth: Cloudflare API token in macOS keychain at
    eli/cloudflare-parkins-ai-token
fetched the same way prebake_cast.py does it. Exported into the
environment of every wrangler invocation.

Image normalisation uses ffmpeg (already at /opt/homebrew/bin/ffmpeg)
because Pillow isn't installed on the system Python. Long edge gets
capped at IMAGE_LONG_EDGE px, metadata stripped, re-encoded as jpeg.

Example — bootstrap Munich, then add a frame:

    bin/walkthrough.py create \\
        --id munich-2026 --title "Munich" \\
        --subtitle "Summer 2026" --description "A family trip — Eli's scrapbook." \\
        --entry-code MUNICH --preset wedding

    bin/walkthrough.py add-panel \\
        --image /tmp/newport_pagnell.jpg \\
        --title "Charlotte and Millie" \\
        --subtitle "Newport Pagnell services, 14:02" \\
        --body "Charlotte walking Millie on the verge..." \\
        --article-file /tmp/article.md
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_ROOT = REPO_ROOT / "content"
ACTIVE_POINTER = CONTENT_ROOT / ".active-walkthrough"

R2_BUCKET = "parkins-ai-walkthroughs"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

WORKER_PROD = "https://walkthrough.parkins.ai"

IMAGE_LONG_EDGE = 2000
IMAGE_QUALITY = 4  # ffmpeg -q:v scale 2 (best) .. 31 (worst); 4 ≈ JPEG quality ~88

VIDEO_LONG_EDGE = 1280
VIDEO_CRF = 23
VIDEO_FILENAME = "video.mp4"

THEME_PRESETS = {
    "wedding": {
        "primary": "#1a1714", "surface": "#221f1b", "accent": "#c9986a",
        "text": "#f5f0e8", "muted": "rgba(245, 240, 232, 0.48)",
        "border": "rgba(201, 152, 106, 0.2)",
        "font_heading": "Cormorant Garamond", "font_body": "Cormorant Garamond",
        "preset": "wedding",
    },
    "trip": {
        "primary": "#0f1419", "surface": "#1a2028", "accent": "#7fb8c7",
        "text": "#eaf2f6", "muted": "rgba(234, 242, 246, 0.48)",
        "border": "rgba(127, 184, 199, 0.22)",
        "font_heading": "Cormorant Garamond", "font_body": "Cormorant Garamond",
        "preset": "trip",
    },
    "city": {
        "primary": "#16140f", "surface": "#221e17", "accent": "#d6a85b",
        "text": "#f4ecdc", "muted": "rgba(244, 236, 220, 0.50)",
        "border": "rgba(214, 168, 91, 0.22)",
        "font_heading": "Cormorant Garamond", "font_body": "Cormorant Garamond",
        "preset": "city",
    },
}

DEFAULT_SECTION_TITLE = "The Journey"

# ── Auth & wrangler ─────────────────────────────────────────────────────────

def cf_token() -> str:
    token = subprocess.check_output(
        ["security", "find-generic-password",
         "-s", "eli/cloudflare-parkins-ai-token", "-w"],
        text=True,
    ).strip()
    if not token:
        raise SystemExit("Cloudflare token not found in keychain (eli/cloudflare-parkins-ai-token)")
    return token


def wrangler_env() -> dict:
    return {**os.environ, "CLOUDFLARE_API_TOKEN": cf_token()}


def r2_put(local: Path, key: str, content_type: str) -> None:
    """Upload one file to R2. Raises on failure."""
    cmd = [
        "npx", "wrangler", "r2", "object", "put",
        f"{R2_BUCKET}/{key}",
        "--file", str(local),
        "--content-type", content_type,
        "--remote",
    ]
    res = subprocess.run(cmd, cwd=REPO_ROOT, env=wrangler_env(),
                         capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"R2 put failed for {key}:\n{res.stderr[-800:]}")


_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; walkthrough-ingest/1.0)"}


def fetch_panel_json(wid: str, panel_id: str) -> dict | None:
    """Read a panel.json via the Worker (panel routes are not code-gated).
    Used by `add-photos` / `remove-panel` when the local content tree
    doesn't have the panel (e.g. it was authored from a different machine
    or via a different working copy). Returns None on 404."""
    url = f"{WORKER_PROD}/api/walkthroughs/{wid}/panels/{panel_id}"
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def fetch_manifest(wid: str, entry_code: str | None) -> dict | None:
    """Read the manifest via the Worker. If gated, supply the entry code."""
    url = f"{WORKER_PROD}/api/walkthroughs/{wid}"
    if entry_code:
        url += f"?code={entry_code}"
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def r2_delete(key: str) -> None:
    cmd = ["npx", "wrangler", "r2", "object", "delete",
           f"{R2_BUCKET}/{key}", "--remote"]
    res = subprocess.run(cmd, cwd=REPO_ROOT, env=wrangler_env(),
                         capture_output=True, text=True)
    if res.returncode != 0:
        # Best-effort: log but don't blow up — the object may already be gone.
        sys.stderr.write(f"warn: r2 delete {key} returned {res.returncode}: {res.stderr[-200:]}\n")


# ── Utility ─────────────────────────────────────────────────────────────────

_SLUG_RX = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _SLUG_RX.sub("-", text.lower()).strip("-")
    return s or "untitled"


def walkthrough_dir(wid: str) -> Path:
    return CONTENT_ROOT / wid


def manifest_path(wid: str) -> Path:
    return walkthrough_dir(wid) / "manifest.json"


def read_manifest(wid: str) -> dict:
    path = manifest_path(wid)
    if not path.exists():
        raise SystemExit(f"No local manifest at {path} — run `create` first, "
                         "or copy the manifest down from R2.")
    return json.loads(path.read_text())


def write_manifest(wid: str, manifest: dict) -> None:
    path = manifest_path(wid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


def upload_manifest(wid: str) -> None:
    r2_put(manifest_path(wid),
           f"walkthroughs/{wid}/manifest.json",
           "application/json")


def normalise_video(src: Path, dest: Path) -> None:
    """Transcode a video to H.264 + AAC, faststart, capped at VIDEO_LONG_EDGE.
    Metadata stripped. Source audio passed through to AAC at 128k; clips
    without audio just produce a video-only file. Full duration preserved.

    `-movflags +faststart` puts the moov atom at the head so HTML5 video
    can start playing while the rest is still downloading.
    """
    if not src.exists():
        raise SystemExit(f"Video not found: {src}")
    vf = (
        f"scale='if(gt(iw,ih),min({VIDEO_LONG_EDGE},iw),-2)':"
        f"'if(gt(ih,iw),min({VIDEO_LONG_EDGE},ih),-2)',"
        "format=yuv420p"
    )
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(VIDEO_CRF),
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-map_metadata", "-1",
        str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg video transcode failed:\n{res.stderr[-600:]}")


def normalise_image(src: Path, dest: Path) -> None:
    """Cap long edge, strip metadata, re-encode as jpg via ffmpeg."""
    if not src.exists():
        raise SystemExit(f"Image not found: {src}")
    vf = (f"scale='if(gt(iw,ih),min({IMAGE_LONG_EDGE},iw),-2)':"
          f"'if(gt(ih,iw),min({IMAGE_LONG_EDGE},ih),-2)',"
          "format=yuvj420p")
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(src),
        "-vf", vf,
        "-map_metadata", "-1",
        "-q:v", str(IMAGE_QUALITY),
        str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg normalise failed:\n{res.stderr[-400:]}")


def set_active(wid: str) -> None:
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    ACTIVE_POINTER.write_text(wid + "\n")


def get_active() -> str | None:
    if ACTIVE_POINTER.exists():
        val = ACTIVE_POINTER.read_text().strip()
        return val or None
    return None


# ── Content identity ────────────────────────────────────────────────────────
#
# Two hashes are recorded per image, and they answer different questions.
#
#   source_sha256      — the source bytes exactly as fed. Normalisation is
#                        lossy, so this is the only thing that survives a
#                        question like "is this the same file Graham sent?".
#   normalised_sha256  — sha256 of the normalised photo-NN.jpg we actually
#                        publish. ffmpeg with fixed flags is byte-
#                        deterministic (verified 2026-09-15: re-normalising
#                        the source of panel 01 reproduced photo.jpg to the
#                        byte), so this identifies a photo even for the 61
#                        Munich panels written before identity existed at
#                        all. Without it, dedup would be blind to history.
#
# captured_at is EXIF DateTimeOriginal. It is never synthesised from mtime —
# a photo with no capture date stays visibly undated.

EXIFTOOL = "/opt/homebrew/bin/exiftool"

# How far outside a walkthrough's observed capture range a photo may fall
# before `add` refuses to file it against the *active pointer*. A trip spans
# days to a few weeks, and legitimately accretes photos from the travel days
# either side and from a later leg of the same trip — so the margin has to be
# generous. The failure this actually guards against is a months-apart
# misroute (Scilly in May vs Munich in June), which 14 days catches easily.
CAPTURE_MARGIN_DAYS = 14


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def exif_captured_at(path: Path) -> str | None:
    """EXIF DateTimeOriginal (falling back to CreateDate) as ISO-8601 local
    time with no timezone, or None if the file carries no capture date."""
    if not Path(EXIFTOOL).exists():
        return None
    res = subprocess.run(
        [EXIFTOOL, "-s3", "-d", "%Y-%m-%dT%H:%M:%S",
         "-DateTimeOriginal", "-CreateDate", str(path)],
        capture_output=True, text=True)
    if res.returncode != 0:
        return None
    for line in res.stdout.splitlines():
        line = line.strip()
        # exiftool renders an unset date as 0000:00:00 00:00:00.
        if line and not line.startswith("0000"):
            return line
    return None


def normalised_sha256(src: Path) -> str:
    """Hash of what this source would become once normalised."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "probe.jpg"
        normalise_image(src, tmp)
        return sha256_file(tmp)


def source_identity(src: Path) -> dict:
    """The identity record for one source image, before it is published."""
    return {
        "source_sha256": sha256_file(src),
        "normalised_sha256": normalised_sha256(src),
        "captured_at": exif_captured_at(src),
    }


def read_panel_json(wid: str, panel_id: str) -> dict | None:
    path = walkthrough_dir(wid) / "panels" / panel_id / "panel.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def panel_image_files(panel: dict) -> list[str]:
    images = list(panel.get("images") or [])
    if not images and panel.get("image"):
        images = [panel["image"]]
    return images


def identity_index(wid: str) -> dict:
    """Map every known hash in a walkthrough to the panel that owns it.

    Panels written by `add` carry `image_meta` and are free to read. Panels
    from before that carry nothing, so their published photo-NN.jpg is
    hashed off disk — which lands in the same namespace as
    `normalised_sha256` and therefore matches a freshly-fed source.
    """
    index: dict[str, tuple[str, str]] = {}
    manifest = read_manifest(wid)
    for entry in manifest.get("panels", []):
        panel_id = entry.get("id")
        if not panel_id:
            continue
        panel = read_panel_json(wid, panel_id)
        if panel is None:
            continue
        meta_by_file = {m.get("file"): m for m in panel.get("image_meta") or []}
        panel_dir = walkthrough_dir(wid) / "panels" / panel_id
        for fname in panel_image_files(panel):
            meta = meta_by_file.get(fname)
            if meta:
                for key in ("source_sha256", "normalised_sha256"):
                    if meta.get(key):
                        index.setdefault(meta[key], (panel_id, fname))
                continue
            local = panel_dir / fname
            if local.exists():
                index.setdefault(sha256_file(local), (panel_id, fname))
    return index


def observed_capture_range(wid: str):
    """(earliest, latest) datetimes seen in a walkthrough, or None if it has
    no dated photos at all. A manifest-level `captured_range` (two ISO
    strings) widens the basis when one has been recorded explicitly."""
    stamps: list[datetime] = []
    manifest = read_manifest(wid)
    for iso in manifest.get("captured_range") or []:
        try:
            stamps.append(datetime.fromisoformat(iso))
        except ValueError:
            pass
    for entry in manifest.get("panels", []):
        panel = read_panel_json(wid, entry.get("id", ""))
        if not panel:
            continue
        for meta in panel.get("image_meta") or []:
            iso = meta.get("captured_at")
            if not iso:
                continue
            try:
                stamps.append(datetime.fromisoformat(iso))
            except ValueError:
                pass
    if not stamps:
        return None
    return min(stamps), max(stamps)


# ── Subcommands ─────────────────────────────────────────────────────────────

def cmd_create(args: argparse.Namespace) -> None:
    wid = args.id
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]+", wid):
        raise SystemExit(f"Invalid id '{wid}' — use lowercase kebab-case.")

    wdir = walkthrough_dir(wid)
    if wdir.exists() and not args.force:
        # Idempotent by default. An auto-feed has to be able to open with
        # `create` and not care whether this is day one or day nine; dying on
        # the normal case is what made the old recipe need a human. --force
        # remains the explicit destructive path and is unchanged.
        manifest = read_manifest(wid)
        set_active(wid)
        n = len(manifest.get("panels", []))
        print(f"walkthrough '{wid}' already exists — reusing it "
              f"({n} panel{'s' if n != 1 else ''}, now active)")
        print(f"  local:    {wdir}")
        print(f"  prod URL: {prod_view_url(wid, manifest.get('entry_code'))}")
        return
    (wdir / "panels").mkdir(parents=True, exist_ok=True)

    theme = THEME_PRESETS.get(args.preset, THEME_PRESETS["trip"]).copy()

    manifest: dict = {
        "id": wid,
        "title": args.title,
        "subtitle": args.subtitle or "",
        "entry_code": args.entry_code.upper() if args.entry_code else None,
        "cover_screen": True,
        "description": args.description or "",
        "theme": theme,
        "sections": [{"title": DEFAULT_SECTION_TITLE, "panels": []}],
        "panels": [],
    }
    if not args.entry_code:
        manifest.pop("entry_code")

    write_manifest(wid, manifest)
    upload_manifest(wid)

    if args.cover:
        cover_src = Path(args.cover).expanduser().resolve()
        cover_local = wdir / "cover.jpg"
        normalise_image(cover_src, cover_local)
        r2_put(cover_local, f"walkthroughs/{wid}/cover.jpg", "image/jpeg")

    set_active(wid)
    print(f"created walkthrough '{wid}' (active)")
    print(f"  local:    {wdir}")
    print(f"  beta URL: {beta_view_url(wid, args.entry_code)}")
    print(f"  prod URL: {prod_view_url(wid, args.entry_code)}")


def _next_ordinal(manifest: dict) -> int:
    ords = []
    for p in manifest.get("panels", []):
        m = re.match(r"^(\d+)-", p.get("id", ""))
        if m:
            ords.append(int(m.group(1)))
    return (max(ords) + 1) if ords else 1


def publish_panel(wid: str, *, title: str, subtitle: str = "", body: str,
                  image_paths: list[str], article_file: str | None = None,
                  article: str | None = None, section: str | None = None,
                  video: str | None = None, force: bool = False,
                  identities: list[dict] | None = None) -> str:
    """Write, upload and index one panel. Returns the panel id.

    Shared by `add-panel` (explicit) and `add` (the auto-feed entry point);
    `add` has already hashed the sources by the time it gets here, so it
    passes `identities` through rather than paying for a second pass.
    """
    manifest = read_manifest(wid)

    # Build panel id: NN-slug.
    ordinal = _next_ordinal(manifest)
    slug = slugify(title)
    panel_id = f"{ordinal:02d}-{slug}"

    # Duplicate-detection on the title slug. This catches a re-run of the
    # same authored panel; it does NOT catch the same photo under a new
    # title — that is what the content hashes in `add` are for.
    existing_ids = {p["id"] for p in manifest.get("panels", [])}
    same_slug = [pid for pid in existing_ids if pid.split("-", 1)[1:] == [slug]]
    if same_slug and not force:
        raise SystemExit(f"A panel with slug '{slug}' already exists "
                         f"({', '.join(same_slug)}). Use --force or change --title.")

    panel_dir = walkthrough_dir(wid) / "panels" / panel_id
    panel_dir.mkdir(parents=True, exist_ok=True)

    # Article body.
    if article_file:
        article_text = Path(article_file).expanduser().read_text()
    elif article:
        article_text = article
    else:
        # Bare minimum article so the long-form view always has something.
        article_text = f"# {title}\n\n{body}\n"

    if not article_text.endswith("\n"):
        article_text += "\n"

    # Normalise every image into the panel dir. Filenames are stable —
    # photo-01.jpg, photo-02.jpg, … — and ordered as passed on the CLI.
    image_filenames: list[str] = []
    image_meta: list[dict] = []
    for idx, src in enumerate(image_paths, start=1):
        src_path = Path(src).expanduser().resolve()
        ident = identities[idx - 1] if identities else source_identity(src_path)
        fname = f"photo-{idx:02d}.jpg"
        normalise_image(src_path, panel_dir / fname)
        image_filenames.append(fname)
        image_meta.append({"file": fname, **ident})

    # Optional single video — transcoded to a standard mp4 alongside the photos.
    video_filename: str | None = None
    if video:
        video_src = Path(video).expanduser().resolve()
        normalise_video(video_src, panel_dir / VIDEO_FILENAME)
        video_filename = VIDEO_FILENAME

    # panel.json + article.md. We always write the gallery field. `image`
    # mirrors images[0] so older code paths (cast hold-image, fallbacks)
    # still find a value — matches the Computing Heroes convention.
    # `image_meta` is additive and parallel to `images`; nothing in the
    # Worker or the SPA reads it, which is the point — identity travels in
    # the data files without changing what they serve.
    panel_json = {
        "title": title,
        "subtitle": subtitle or "",
        "body": body,
        "image": image_filenames[0],
        "images": image_filenames,
        "image_meta": image_meta,
        "article": "article.md",
    }
    if video_filename:
        panel_json["video"] = video_filename
    (panel_dir / "panel.json").write_text(
        json.dumps(panel_json, indent=2, ensure_ascii=False) + "\n")
    (panel_dir / "article.md").write_text(article_text)

    # Sync everything to R2. Photos and the (optional) video go up before
    # panel.json — so the manifest never references assets that aren't yet
    # in the bucket.
    base = f"walkthroughs/{wid}/panels/{panel_id}"
    for fname in image_filenames:
        r2_put(panel_dir / fname, f"{base}/{fname}", "image/jpeg")
    if video_filename:
        r2_put(panel_dir / video_filename, f"{base}/{video_filename}", "video/mp4")
    r2_put(panel_dir / "panel.json", f"{base}/panel.json", "application/json")
    r2_put(panel_dir / "article.md", f"{base}/article.md", "text/markdown")

    # Update manifest.
    manifest.setdefault("panels", []).append({
        "id": panel_id,
        "title": title,
        "subtitle": subtitle or "",
    })

    section_title = section or DEFAULT_SECTION_TITLE
    sections = manifest.setdefault("sections", [])
    target = next((s for s in sections if s.get("title") == section_title), None)
    if target is None:
        target = {"title": section_title, "panels": []}
        sections.append(target)
    target.setdefault("panels", []).append(panel_id)

    write_manifest(wid, manifest)
    upload_manifest(wid)
    return panel_id


def cmd_add_panel(args: argparse.Namespace) -> None:
    wid = args.walkthrough or get_active()
    if not wid:
        raise SystemExit("No walkthrough specified and no active pointer. "
                         "Pass --walkthrough or run `create` / `set-active` first.")

    panel_id = publish_panel(
        wid, title=args.title, subtitle=args.subtitle, body=args.body,
        image_paths=list(args.image), article_file=args.article_file,
        article=args.article, section=args.section, video=args.video,
        force=args.force)

    entry_code = read_manifest(wid).get("entry_code")
    print(f"added panel '{panel_id}' to {wid}")
    print(f"  beta URL: {beta_view_url(wid, entry_code)}")


def cmd_remove_panel(args: argparse.Namespace) -> None:
    wid = args.walkthrough or get_active()
    if not wid:
        raise SystemExit("No walkthrough specified.")
    manifest = read_manifest(wid)

    panel_id = args.panel_id
    before = len(manifest.get("panels", []))
    manifest["panels"] = [p for p in manifest.get("panels", []) if p["id"] != panel_id]
    for s in manifest.get("sections", []):
        s["panels"] = [pid for pid in s.get("panels", []) if pid != panel_id]
    if len(manifest["panels"]) == before:
        sys.stderr.write(f"warn: panel '{panel_id}' not in manifest\n")

    write_manifest(wid, manifest)
    upload_manifest(wid)

    # Look up the panel's image list so we delete the right photo-NN.jpg
    # files. Prefer the local source-of-truth panel.json; fall back to R2
    # via the Worker if the local tree doesn't have it.
    panel: dict = {}
    local_panel_json = walkthrough_dir(wid) / "panels" / panel_id / "panel.json"
    if local_panel_json.exists():
        panel = json.loads(local_panel_json.read_text())
    else:
        panel = fetch_panel_json(wid, panel_id) or {}
    images = list(panel.get("images") or [])
    if not images and panel.get("image"):
        images = [panel["image"]]
    if not images:
        images = ["photo.jpg"]

    # video.mp4 is the inline panel video (this feature). cast.mp4 is the
    # separate prebaked story-mode/narration video — different code path,
    # but if one exists we clean it up too rather than leaving R2 orphans.
    base = f"walkthroughs/{wid}/panels/{panel_id}"
    cleanup = images + [VIDEO_FILENAME, "panel.json", "article.md",
                        "cast.mp4", "narration.mp3"]
    for fname in cleanup:
        r2_delete(f"{base}/{fname}")

    local_dir = walkthrough_dir(wid) / "panels" / panel_id
    if local_dir.exists():
        shutil.rmtree(local_dir)
    print(f"removed panel '{panel_id}' from {wid}")


def cmd_move_panel(args: argparse.Namespace) -> None:
    """Reorder a panel without touching its assets.

    Panel display order follows the manifest `panels` array (and each
    section's `panels` list), NOT the numeric id prefix — so a move is a
    pure manifest edit + re-upload. R2 assets are keyed by panel id and
    stay put. Section order is re-derived from the new global order, so
    sections always mirror the top-level sequence.
    """
    wid = args.walkthrough or get_active()
    if not wid:
        raise SystemExit("No walkthrough specified.")
    manifest = read_manifest(wid)

    panels = manifest.get("panels", [])
    ids = [p["id"] for p in panels]
    pid = args.panel_id
    if pid not in ids:
        raise SystemExit(f"Panel '{pid}' not in {wid}.")

    remaining = [i for i in ids if i != pid]

    if args.to_index is not None:
        idx = max(0, min(args.to_index, len(remaining)))
        new_ids = remaining[:idx] + [pid] + remaining[idx:]
    else:
        ref = args.after if args.after is not None else args.before
        if ref not in ids:
            raise SystemExit(f"Reference panel '{ref}' not in {wid}.")
        if ref == pid:
            raise SystemExit("Cannot move a panel relative to itself.")
        pos = remaining.index(ref)
        insert_at = pos + 1 if args.after is not None else pos
        new_ids = remaining[:insert_at] + [pid] + remaining[insert_at:]

    by_id = {p["id"]: p for p in panels}
    manifest["panels"] = [by_id[i] for i in new_ids]
    order = {i: n for n, i in enumerate(new_ids)}
    for s in manifest.get("sections", []):
        s["panels"] = sorted(s.get("panels", []), key=lambda x: order.get(x, len(new_ids)))

    write_manifest(wid, manifest)
    upload_manifest(wid)

    where = new_ids.index(pid)
    nbr = new_ids[where - 1] if where > 0 else "(start)"
    print(f"moved '{pid}' → position {where} (after {nbr}) in {wid}")


def cmd_add_photos(args: argparse.Namespace) -> None:
    """Append images to an existing panel without recreating it.

    Used when Graham sends follow-up photos in a separate message that
    belong to the same scene as the panel just created.
    """
    wid = args.walkthrough or get_active()
    if not wid:
        raise SystemExit("No walkthrough specified.")
    panel_id = args.panel_id

    # Resolve panel.json — prefer local source-of-truth, fall back to R2.
    panel_dir = walkthrough_dir(wid) / "panels" / panel_id
    panel_json_path = panel_dir / "panel.json"
    if panel_json_path.exists():
        panel = json.loads(panel_json_path.read_text())
    else:
        panel = fetch_panel_json(wid, panel_id)
        if panel is None:
            raise SystemExit(f"Panel '{panel_id}' not found in {wid}.")
        # Drop the id the Worker injects on its outbound shape.
        panel.pop("id", None)
        panel_dir.mkdir(parents=True, exist_ok=True)

    images = list(panel.get("images") or [])
    if not images and panel.get("image"):
        images = [panel["image"]]

    # Continue ordinal numbering past whatever's already there.
    start = len(images) + 1
    new_filenames: list[str] = []
    new_meta: list[dict] = []
    identities = getattr(args, "identities", None)
    for offset, src in enumerate(args.image):
        idx = start + offset
        src_path = Path(src).expanduser().resolve()
        ident = identities[offset] if identities else source_identity(src_path)
        fname = f"photo-{idx:02d}.jpg"
        normalise_image(src_path, panel_dir / fname)
        new_filenames.append(fname)
        new_meta.append({"file": fname, **ident})

    images.extend(new_filenames)
    panel["images"] = images
    panel["image"] = images[0]
    # Identity accretes alongside the images. Photos appended to a panel
    # written before image_meta existed leave the older entries absent
    # rather than back-filling a hash we cannot honestly derive here.
    panel["image_meta"] = list(panel.get("image_meta") or []) + new_meta
    panel.setdefault("article", "article.md")

    panel_json_path.write_text(
        json.dumps(panel, indent=2, ensure_ascii=False) + "\n")

    base = f"walkthroughs/{wid}/panels/{panel_id}"
    for fname in new_filenames:
        r2_put(panel_dir / fname, f"{base}/{fname}", "image/jpeg")
    r2_put(panel_json_path, f"{base}/panel.json", "application/json")

    print(f"appended {len(new_filenames)} photo(s) to {panel_id}: {', '.join(new_filenames)}")
    print(f"  panel now has {len(images)} image(s)")


def cmd_add_video(args: argparse.Namespace) -> None:
    """Attach (or replace) the single video on an existing panel.

    The walkthrough viewer renders one video per panel as the first slide
    of the carousel. Re-running this with a different clip overwrites the
    existing video.mp4 — we surface that explicitly so the caller knows.
    """
    wid = args.walkthrough or get_active()
    if not wid:
        raise SystemExit("No walkthrough specified.")
    panel_id = args.panel_id

    # Resolve panel.json — prefer local source-of-truth, fall back to R2.
    panel_dir = walkthrough_dir(wid) / "panels" / panel_id
    panel_json_path = panel_dir / "panel.json"
    if panel_json_path.exists():
        panel = json.loads(panel_json_path.read_text())
    else:
        panel = fetch_panel_json(wid, panel_id)
        if panel is None:
            raise SystemExit(f"Panel '{panel_id}' not found in {wid}.")
        panel.pop("id", None)
        panel_dir.mkdir(parents=True, exist_ok=True)

    replacing = bool(panel.get("video"))

    src_video = Path(args.video).expanduser().resolve()
    normalise_video(src_video, panel_dir / VIDEO_FILENAME)

    panel["video"] = VIDEO_FILENAME
    panel_json_path.write_text(
        json.dumps(panel, indent=2, ensure_ascii=False) + "\n")

    base = f"walkthroughs/{wid}/panels/{panel_id}"
    r2_put(panel_dir / VIDEO_FILENAME, f"{base}/{VIDEO_FILENAME}", "video/mp4")
    r2_put(panel_json_path, f"{base}/panel.json", "application/json")

    verb = "replaced video on" if replacing else "added video to"
    print(f"{verb} {panel_id} (1 video max — this is the panel's only clip)")


def _title_from_id(wid: str) -> str:
    return " ".join(w.capitalize() for w in wid.split("-"))


def ensure_walkthrough(wid: str, args: argparse.Namespace) -> bool:
    """Create the walkthrough if it isn't there. Returns True if created."""
    if manifest_path(wid).exists():
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]+", wid):
        raise SystemExit(f"Invalid id '{wid}' — use lowercase kebab-case.")
    create_args = argparse.Namespace(
        id=wid,
        title=args.walkthrough_title or _title_from_id(wid),
        subtitle=args.walkthrough_subtitle or "",
        description=args.walkthrough_description or "",
        entry_code=args.entry_code,
        preset=args.preset,
        cover=args.cover,
        force=False,
    )
    cmd_create(create_args)
    return True


def cmd_add(args: argparse.Namespace) -> None:
    """The single call: photos in, panel published, nothing else to remember.

    Everything here exists to make an unattended feed safe. The prose is
    still authored by hand — that is the part that must not be automated.
    """
    explicit = args.walkthrough is not None
    wid = args.walkthrough or get_active()
    if not wid:
        raise SystemExit("No walkthrough specified and no active pointer. "
                         "Pass --walkthrough or run `create` / `set-active` first.")

    sources = [Path(s).expanduser().resolve() for s in args.image]
    for src in sources:
        if not src.exists():
            raise SystemExit(f"Image not found: {src}")

    created = ensure_walkthrough(wid, args)

    # Hash and date every source once, up front. Used for dedup, for the
    # pointer guard, and then handed to the publisher so nothing is
    # recomputed.
    identities = [source_identity(src) for src in sources]

    if not created:
        # ── Is this photo already filed? ────────────────────────────────
        index = identity_index(wid)
        matches: list[tuple[Path, str, str]] = []
        fresh: list[int] = []
        for i, (src, ident) in enumerate(zip(sources, identities)):
            hit = (index.get(ident["source_sha256"])
                   or index.get(ident["normalised_sha256"]))
            if hit:
                matches.append((src, hit[0], hit[1]))
            else:
                fresh.append(i)

        if matches and not fresh:
            print(f"already filed in {wid} — nothing written.")
            for src, panel_id, fname in matches:
                print(f"  {src.name} → panel '{panel_id}' ({fname})")
            print(f"  prod URL: "
                  f"{prod_view_url(wid, read_manifest(wid).get('entry_code'))}")
            return

        if matches and not (args.append_to or args.force):
            lines = "\n".join(f"  {s.name} → panel '{p}' ({f})"
                              for s, p, f in matches)
            raise SystemExit(
                f"{len(matches)} of {len(sources)} photo(s) are already in {wid}:\n"
                f"{lines}\n"
                f"The other {len(fresh)} are new. Either pass "
                f"--append-to <panel-id> to add just the new ones to an "
                f"existing panel, or --force to publish all of them as a new "
                f"panel anyway.")

        if matches and args.append_to:
            # Only the genuinely new images go onto the named panel.
            sources = [sources[i] for i in fresh]
            identities = [identities[i] for i in fresh]

        # ── Is this photo even from this trip? ──────────────────────────
        if not explicit:
            _guard_active_pointer(wid, identities)

    if args.append_to:
        if not sources:
            print(f"nothing new to append to '{args.append_to}'.")
            return
        sub = argparse.Namespace(walkthrough=wid, panel_id=args.append_to,
                                 image=[str(s) for s in sources],
                                 identities=identities)
        cmd_add_photos(sub)
        return

    panel_id = publish_panel(
        wid, title=args.title, subtitle=args.subtitle, body=args.body,
        image_paths=[str(s) for s in sources], article_file=args.article_file,
        article=args.article, section=args.section, video=args.video,
        force=args.force, identities=identities)

    entry_code = read_manifest(wid).get("entry_code")
    print(f"added panel '{panel_id}' to {wid}")
    print(f"  prod URL: {prod_view_url(wid, entry_code)}")


def _guard_active_pointer(wid: str, identities: list[dict]) -> None:
    """Refuse to file photos into the walkthrough the pointer happens to name
    when their capture dates say they belong somewhere else.

    Only ever consulted when the caller did NOT name a walkthrough — an
    explicit --walkthrough is a decision, and decisions are not second-
    guessed. Undated photos never trip it: absence of EXIF is not evidence.
    """
    dated = [datetime.fromisoformat(i["captured_at"])
             for i in identities if i.get("captured_at")]
    if not dated:
        return
    span = observed_capture_range(wid)
    if span is None:
        print(f"note: {wid} has no dated photos yet — capture-date guard "
              f"cannot be evaluated, proceeding on the active pointer.",
              file=sys.stderr)
        return
    earliest, latest = span
    lo = earliest - timedelta(days=CAPTURE_MARGIN_DAYS)
    hi = latest + timedelta(days=CAPTURE_MARGIN_DAYS)
    outside = [d for d in dated if d < lo or d > hi]
    if not outside:
        return
    shown = ", ".join(d.strftime("%Y-%m-%d") for d in sorted(outside)[:4])
    raise SystemExit(
        f"refusing to file into '{wid}' — the active walkthrough.\n"
        f"  {wid} holds photos from {earliest:%Y-%m-%d} to {latest:%Y-%m-%d}; "
        f"these were taken {shown}.\n"
        f"  That is more than {CAPTURE_MARGIN_DAYS} days outside its range, "
        f"which usually means the pointer is stale.\n"
        f"  Pass --walkthrough <id> to say where these belong (that overrides "
        f"this check), or run `set-active --id <id>` first.")


def cmd_set_active(args: argparse.Namespace) -> None:
    set_active(args.id)
    print(f"active walkthrough → {args.id}")


def cmd_show_active(_: argparse.Namespace) -> None:
    wid = get_active()
    if wid:
        print(wid)
    else:
        sys.exit(1)


# ── URL helpers ──────────────────────────────────────────────────────────────

def _beta_origin() -> str:
    # Resolved at runtime by listing the beta deployment if needed. The hostname
    # for `--env beta` is `walkthrough-beta.<account-subdomain>.workers.dev`;
    # we don't know the account subdomain at file-read time, so the helper just
    # returns the documented hostname. After `wrangler deploy --env beta`, the
    # real URL is printed by wrangler — update if it changes.
    return os.environ.get("WALKTHROUGH_BETA_ORIGIN",
                          "https://walkthrough-beta.parkins-d93jed72hfs91np.workers.dev")


def beta_view_url(wid: str, entry_code: str | None) -> str:
    base = f"{_beta_origin()}/{wid}"
    return f"{base}?code={entry_code}" if entry_code else base


def prod_view_url(wid: str, entry_code: str | None) -> str:
    base = f"{WORKER_PROD}/{wid}"
    return f"{base}?code={entry_code}" if entry_code else base


# ── Argument parsing ────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Walkthrough ingestion (create/add/remove).")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="Bootstrap a new walkthrough.")
    c.add_argument("--id", required=True, help="kebab-case identifier (e.g. munich-2026)")
    c.add_argument("--title", required=True, help="Display title (kept generic — appears in public listing)")
    c.add_argument("--subtitle", default="")
    c.add_argument("--description", default="", help="One-line description (public — keep clean)")
    c.add_argument("--entry-code", default=None, help="Uppercase entry code (e.g. MUNICH)")
    c.add_argument("--preset", default="trip", choices=sorted(THEME_PRESETS.keys()))
    c.add_argument("--cover", default=None, help="Path to a cover image (optional)")
    c.add_argument("--force", action="store_true", help="Overwrite an existing local dir")
    c.set_defaults(func=cmd_create)

    ad = sub.add_parser(
        "add",
        help="Photos + commentary in, panel out. The one call an auto-feed needs.",
        description="Creates the walkthrough if it is missing, refuses photos "
                    "that are already filed, refuses photos whose capture "
                    "dates say they belong to a different trip, and otherwise "
                    "publishes the panel.")
    ad.add_argument("--walkthrough", default=None,
                    help="Walkthrough id. Defaults to .active-walkthrough. "
                         "Passing it explicitly also overrides the "
                         "capture-date guard.")
    ad.add_argument("--image", required=True, action="append",
                    help="Path to a source image. Repeat for a gallery panel "
                         "(order preserved; first image is the thumbnail).")
    ad.add_argument("--title", default=None, help="Panel title")
    ad.add_argument("--subtitle", default="")
    ad.add_argument("--body", default=None, help="Short caption-length prose")
    ad.add_argument("--article-file", default=None, help="Path to a markdown file")
    ad.add_argument("--article", default=None,
                    help="Inline markdown (avoid — prefer --article-file)")
    ad.add_argument("--section", default=None,
                    help=f"Section to append to (default: '{DEFAULT_SECTION_TITLE}')")
    ad.add_argument("--video", default=None,
                    help="Optional single video clip for this panel")
    ad.add_argument("--append-to", default=None, metavar="PANEL_ID",
                    help="Append the new photos to this existing panel "
                         "instead of creating one. Also the answer when some "
                         "of the photos are already filed.")
    ad.add_argument("--force", action="store_true",
                    help="Publish anyway despite a duplicate photo or slug")
    # Only consulted when the walkthrough has to be created.
    ad.add_argument("--walkthrough-title", default=None,
                    help="Title to use if the walkthrough must be created "
                         "(default: derived from the id)")
    ad.add_argument("--walkthrough-subtitle", default=None)
    ad.add_argument("--walkthrough-description", default=None)
    ad.add_argument("--entry-code", default=None,
                    help="Entry code, if the walkthrough must be created")
    ad.add_argument("--preset", default="trip", choices=sorted(THEME_PRESETS.keys()))
    ad.add_argument("--cover", default=None,
                    help="Cover image, if the walkthrough must be created")
    ad.set_defaults(func=cmd_add)

    a = sub.add_parser("add-panel", help="Add a panel from one or more images + commentary.")
    a.add_argument("--walkthrough", default=None,
                   help="Walkthrough id (defaults to .active-walkthrough)")
    a.add_argument("--image", required=True, action="append",
                   help="Path to a source image. Repeat for a multi-image gallery panel "
                        "(order is preserved; first image is the panel thumbnail).")
    a.add_argument("--title", required=True)
    a.add_argument("--subtitle", default="")
    a.add_argument("--body", required=True, help="Short caption-length prose")
    a.add_argument("--article-file", default=None, help="Path to a markdown file")
    a.add_argument("--article", default=None, help="Inline markdown (avoid — prefer --article-file)")
    a.add_argument("--section", default=None,
                   help=f"Section to append to (default: '{DEFAULT_SECTION_TITLE}')")
    a.add_argument("--video", default=None,
                   help="Optional path to a single video clip. Renders as the first "
                        "slide of the panel's gallery. One video max per panel.")
    a.add_argument("--force", action="store_true", help="Allow a duplicate slug")
    a.set_defaults(func=cmd_add_panel)

    ap = sub.add_parser("add-photos",
                        help="Append one or more images to an existing panel (no prose changes).")
    ap.add_argument("--walkthrough", default=None,
                    help="Walkthrough id (defaults to .active-walkthrough)")
    ap.add_argument("--panel-id", required=True, help="Existing panel id, e.g. 06-birra-moretti")
    ap.add_argument("--image", required=True, action="append",
                    help="Path to a source image. Repeat to append several at once.")
    ap.set_defaults(func=cmd_add_photos)

    av = sub.add_parser("add-video",
                        help="Attach or replace the single video on an existing panel.")
    av.add_argument("--walkthrough", default=None,
                    help="Walkthrough id (defaults to .active-walkthrough)")
    av.add_argument("--panel-id", required=True, help="Existing panel id")
    av.add_argument("--video", required=True, help="Path to the source video clip")
    av.set_defaults(func=cmd_add_video)

    m = sub.add_parser("move-panel",
                       help="Reorder a panel (manifest-only; assets stay put).")
    m.add_argument("--walkthrough", default=None,
                   help="Walkthrough id (defaults to .active-walkthrough)")
    m.add_argument("--panel-id", required=True, help="Panel id to move")
    mg = m.add_mutually_exclusive_group(required=True)
    mg.add_argument("--after", default=None, metavar="PANEL_ID",
                    help="Place immediately after this panel")
    mg.add_argument("--before", default=None, metavar="PANEL_ID",
                    help="Place immediately before this panel")
    mg.add_argument("--to-index", type=int, default=None, metavar="N",
                    help="Place at 0-based index N in the panel order")
    m.set_defaults(func=cmd_move_panel)

    r = sub.add_parser("remove-panel", help="Remove a panel.")
    r.add_argument("--walkthrough", default=None)
    r.add_argument("--panel-id", required=True)
    r.set_defaults(func=cmd_remove_panel)

    s = sub.add_parser("set-active", help="Set the active walkthrough pointer.")
    s.add_argument("--id", required=True)
    s.set_defaults(func=cmd_set_active)

    sa = sub.add_parser("show-active", help="Print the active walkthrough id.")
    sa.set_defaults(func=cmd_show_active)

    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "add" and not args.append_to:
        missing = [f"--{n}" for n in ("title", "body") if not getattr(args, n)]
        if missing:
            raise SystemExit(f"add: {' and '.join(missing)} required "
                             f"(or pass --append-to to extend an existing panel)")
    args.func(args)


if __name__ == "__main__":
    main()
