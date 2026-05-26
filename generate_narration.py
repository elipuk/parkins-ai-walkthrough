#!/usr/bin/env python3
"""Generate ElevenLabs narration MP3s for walkthrough panels and upload to R2.

Usage:
    python3 generate_narration.py [--walkthrough <id>] [--panel <id>]

Defaults to the computing-heroes walkthrough. Reads panel.json body text,
synthesises via ElevenLabs (voice: Michelle C92s6vssSLlabgIln1iY), writes
MP3 to /tmp/walkthrough-narration/{panel_id}.mp3, gets duration via ffprobe,
uploads to R2, and patches narration_duration into the local panel.json.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────

VOICE_ID = "C92s6vssSLlabgIln1iY"
MODEL_ID = "eleven_turbo_v2_5"
API_KEY = subprocess.check_output(
    ["security", "find-generic-password", "-s", "eli/elevenlabs", "-w"],
    text=True,
).strip()

DEMO_ROOT = Path(__file__).parent / "demo"
OUT_DIR = Path("/tmp/walkthrough-narration")
R2_BUCKET = "parkins-ai-walkthroughs"

ELEVENLABS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"


def synthesise(text: str, out_path: Path) -> None:
    """Call ElevenLabs TTS and write MP3 to out_path."""
    payload = json.dumps({
        "text": text,
        "model_id": MODEL_ID,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": 1.0,
        },
    }).encode()

    req = urllib.request.Request(
        ELEVENLABS_URL,
        data=payload,
        headers={
            "xi-api-key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )

    print(f"  Calling ElevenLabs for {out_path.name}…", end=" ", flush=True)
    with urllib.request.urlopen(req, timeout=60) as resp:
        out_path.write_bytes(resp.read())
    print("done")


def get_duration(mp3: Path) -> float:
    """Return audio duration in seconds via ffprobe."""
    try:
        result = subprocess.check_output(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(mp3),
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        return round(float(result), 2)
    except Exception:
        return 0.0


def get_cf_token() -> str:
    token = subprocess.check_output(
        ["security", "find-generic-password", "-s", "eli/cloudflare-parkins-ai-token", "-w"],
        text=True,
    ).strip()
    if not token:
        raise RuntimeError("Cloudflare API token not found in keychain at eli/cloudflare-parkins-ai-token")
    return token


def r2_upload(local: Path, r2_key: str) -> None:
    """Upload a file to R2 via wrangler."""
    cmd = (
        f'npx wrangler r2 object put "{R2_BUCKET}/{r2_key}"'
        f" --file {local} --remote"
    )
    env = {**os.environ, "CLOUDFLARE_API_TOKEN": get_cf_token()}
    print(f"  Uploading to r2://{R2_BUCKET}/{r2_key}…", end=" ", flush=True)
    subprocess.check_call(cmd, shell=True, cwd=Path(__file__).parent, env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    print("done")


def process_panel(walkthrough_id: str, panel_id: str) -> None:
    panel_dir = DEMO_ROOT / walkthrough_id / "panels" / panel_id
    panel_json_path = panel_dir / "panel.json"

    with open(panel_json_path) as f:
        panel = json.load(f)

    body = panel.get("body", "").strip()
    if not body:
        print(f"  [{panel_id}] No body text — skipping.")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    mp3_path = OUT_DIR / f"{panel_id}.mp3"

    print(f"\n[{panel_id}] {panel['title']}")
    synthesise(body, mp3_path)

    duration = get_duration(mp3_path)
    print(f"  Duration: {duration}s")

    r2_key = f"walkthroughs/{walkthrough_id}/panels/{panel_id}/narration.mp3"
    r2_upload(mp3_path, r2_key)

    # Patch narration_duration into local panel.json
    panel["narration"] = "narration.mp3"
    panel["narration_duration"] = duration
    with open(panel_json_path, "w") as f:
        json.dump(panel, f, indent=2, ensure_ascii=False)
    print(f"  Updated {panel_json_path.name} with narration_duration={duration}")

    # Also upload updated panel.json to R2
    r2_json_key = f"walkthroughs/{walkthrough_id}/panels/{panel_id}/panel.json"
    r2_upload(panel_json_path, r2_json_key)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--walkthrough", default="computing-heroes")
    parser.add_argument("--panel", default=None, help="Single panel ID, or all if omitted")
    args = parser.parse_args()

    wid = args.walkthrough
    manifest_path = DEMO_ROOT / wid / "manifest.json"
    with open(manifest_path) as f:
        manifest = json.load(f)

    panels = manifest["panels"]
    if args.panel:
        panels = [p for p in panels if p["id"] == args.panel]
        if not panels:
            print(f"Panel '{args.panel}' not found in manifest.")
            sys.exit(1)

    print(f"Generating narration for {len(panels)} panels in '{wid}'…")
    for p in panels:
        process_panel(wid, p["id"])

    print("\nAll panels done.")


if __name__ == "__main__":
    main()
