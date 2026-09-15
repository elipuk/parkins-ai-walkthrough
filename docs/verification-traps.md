# Verification traps in this repo

Things that reported success, or reported a number, and were wrong. Each one cost
real time. They are written down so the next person does not re-earn them.

## A range request can be answered by the edge, not by your Worker

Cloudflare caches the asset route (`public, max-age=3600` on media). Once a full
object is in the edge cache, Cloudflare will satisfy a `Range` request **from that
cache entry itself** and return a perfectly correct `206` — whether or not the
Worker supports Range at all.

On 2026-09-15 this produced a `206 / 65536 bytes` from production against a Worker
that was still ignoring `Range` entirely. The evidence looked like a pass. It was
the cache.

**How to test Range for real:** add a cache-busting query string (`?z=$RANDOM`) so
the request misses cache and reaches the Worker, and probe more than once —
deployments propagate, and a single probe mid-rollout can hit either version. In
the same session the *same* URL gave `206` (edge cache), then `200` full (stale
version), then the true `206`/`416`. Only repeated cache-busted probes after
propagation settled told the truth.

Corollary: `%{size_download}` is the useful column. A `206` that hands back the
whole object is not a `206` that works.

## R2 throws on an out-of-range start — it does not return null

`env.BUCKET.get(key, { range })` returns `null` for a missing key, so the obvious
shape is:

```js
const obj = await env.BUCKET.get(key, { range });
if (!obj) return notFound();
```

That is wrong. If the range **starts past the end of the object**, R2 raises
instead of returning null, and the uncaught error surfaces as a `500`. Measured on
beta: `Range: bytes=99999999999-` returned `500` until the call was wrapped.

A ranged get therefore needs a `try`/`catch`, and the catch needs to distinguish
"object missing" (`404`) from "object exists, range unsatisfiable" (`416`, with
`Content-Range: bytes */<size>`). See `r2Get` in `src/index.js`.

This is why beta-first is not ceremony: the failure only appears against real R2.

## `wrangler deploy` can exit 0 and serve the previous file

On 2026-09-15 a beta deploy printed `Uploaded walkthrough-beta` and succeeded, and
the site kept serving the previous `index.html`. The next identical deploy reported
`Found 1 new or modified static asset to upload` and the change went live.

**Never treat a clean wrangler exit as proof.** Diff what is actually served
against what is on disk:

```bash
curl -s "https://<host>/?cb=$RANDOM" | diff -q - public/index.html
```

## A prebake run that bakes nothing can report success

Entry-code-gated walkthroughs answer `GET /api/walkthroughs/{id}` with a stub
manifest: no `panels[]`, just `code_required: true`. `prebake_cast.py` asked for
the panel list, got zero panels, baked zero, and exited 0.

That is how `munich-2026` and `maddy-wedding-2026` went unbaked while every run
looked clean. Removing the stale hardcoded walkthrough list did not fix it — it
only moved the silence.

The script now passes `?code=` and treats a still-gated response as a hard
failure. The general rule: **an empty work list is a claim that needs checking,
not a result.** Compare against the panel count you expect.

## ffmpeg's `-t` cuts video early on a concat input

`-t 600` against the concat demuxer gave **599** frames and a 599.000 s video
stream against 600.000 s of audio and container — the last frame has no successor,
so it was dropped. `-af apad` then padded the audio into a final second with no
video behind it.

`tpad` alone did not fix it (measured: still 599). It takes `tpad` to clone past
the end **and** `fps=` to re-time onto a strict CFR grid before `-t` truncates.

Always `ffprobe` all three durations after a bake, not just the container:

```bash
ffprobe -v error -select_streams v -show_entries stream=nb_frames,duration -of csv=p=0 f.mp4
ffprobe -v error -select_streams a -show_entries stream=duration -of csv=p=0 f.mp4
ffprobe -v error -show_entries format=duration -of csv=p=0 f.mp4
```

## `force_original_aspect_ratio=decrease` still upscales

It constrains the output to the target box; it does not stop a *smaller* source
being enlarged into it. `scale=1920:1080:force_original_aspect_ratio=decrease`
turned a 640x480 original into 1440x1080 — 2.25x of detail that was never in the
photograph — while a comment two lines up claimed sources were "never upscaled
past their own resolution".

Give the box a lower bound:

```
scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2
```

Measured after the fix: 640x480 stays 640x480; 4000x3000 still caps to 1440x1080.

A comment asserting a property the code does not have is worse than the bug, because
it stops the next reader from looking.
