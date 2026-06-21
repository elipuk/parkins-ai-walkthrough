#!/usr/bin/env python3
"""walkthrough.py — feed the parkins-ai walkthrough system new panels.

Source of truth: `content/<id>/...` on disk (gitignored — these are
holiday photos). Serving copy: R2 bucket `parkins-ai-walkthroughs`.
This script writes both — local first, then syncs to R2.

The Worker (src/index.js) is read-only over R2, so no redeploy is
needed when content changes. Worker deploys are a separate concern.

Subcommands:
    create        Bootstrap a new walkthrough (manifest + optional cover)
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
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
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


# ── Subcommands ─────────────────────────────────────────────────────────────

def cmd_create(args: argparse.Namespace) -> None:
    wid = args.id
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]+", wid):
        raise SystemExit(f"Invalid id '{wid}' — use lowercase kebab-case.")

    wdir = walkthrough_dir(wid)
    if wdir.exists() and not args.force:
        raise SystemExit(f"{wdir} already exists. Use --force to overwrite.")
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


def cmd_add_panel(args: argparse.Namespace) -> None:
    wid = args.walkthrough or get_active()
    if not wid:
        raise SystemExit("No walkthrough specified and no active pointer. "
                         "Pass --walkthrough or run `create` / `set-active` first.")

    manifest = read_manifest(wid)

    # Build panel id: NN-slug.
    ordinal = _next_ordinal(manifest)
    slug = slugify(args.title)
    panel_id = f"{ordinal:02d}-{slug}"

    # Duplicate-detection — same slug already in the manifest?
    existing_ids = {p["id"] for p in manifest.get("panels", [])}
    same_slug = [pid for pid in existing_ids if pid.split("-", 1)[1:] == [slug]]
    if same_slug and not args.force:
        raise SystemExit(f"A panel with slug '{slug}' already exists "
                         f"({', '.join(same_slug)}). Use --force or change --title.")

    panel_dir = walkthrough_dir(wid) / "panels" / panel_id
    panel_dir.mkdir(parents=True, exist_ok=True)

    # Article body.
    if args.article_file:
        article_text = Path(args.article_file).expanduser().read_text()
    elif args.article:
        article_text = args.article
    else:
        # Bare minimum article so the long-form view always has something.
        article_text = f"# {args.title}\n\n{args.body}\n"

    if not article_text.endswith("\n"):
        article_text += "\n"

    # Normalise every image into the panel dir. Filenames are stable —
    # photo-01.jpg, photo-02.jpg, … — and ordered as passed on the CLI.
    image_paths: list[str] = list(args.image)
    image_filenames: list[str] = []
    for idx, src in enumerate(image_paths, start=1):
        src_path = Path(src).expanduser().resolve()
        fname = f"photo-{idx:02d}.jpg"
        normalise_image(src_path, panel_dir / fname)
        image_filenames.append(fname)

    # Optional single video — transcoded to a standard mp4 alongside the photos.
    video_filename: str | None = None
    if args.video:
        video_src = Path(args.video).expanduser().resolve()
        normalise_video(video_src, panel_dir / VIDEO_FILENAME)
        video_filename = VIDEO_FILENAME

    # panel.json + article.md. We always write the gallery field. `image`
    # mirrors images[0] so older code paths (cast hold-image, fallbacks)
    # still find a value — matches the Computing Heroes convention.
    panel_json = {
        "title": args.title,
        "subtitle": args.subtitle or "",
        "body": args.body,
        "image": image_filenames[0],
        "images": image_filenames,
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
        "title": args.title,
        "subtitle": args.subtitle or "",
    })

    section_title = args.section or DEFAULT_SECTION_TITLE
    sections = manifest.setdefault("sections", [])
    target = next((s for s in sections if s.get("title") == section_title), None)
    if target is None:
        target = {"title": section_title, "panels": []}
        sections.append(target)
    target.setdefault("panels", []).append(panel_id)

    write_manifest(wid, manifest)
    upload_manifest(wid)

    entry_code = manifest.get("entry_code")
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
    for offset, src in enumerate(args.image):
        idx = start + offset
        src_path = Path(src).expanduser().resolve()
        fname = f"photo-{idx:02d}.jpg"
        normalise_image(src_path, panel_dir / fname)
        new_filenames.append(fname)

    images.extend(new_filenames)
    panel["images"] = images
    panel["image"] = images[0]
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
    args.func(args)


if __name__ == "__main__":
    main()
