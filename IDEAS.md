# Walkthrough viewer — ideas & requirements backlog

> Raw capture. **Not** specs, not committed implementation plans — ideas to weigh
> and shape later. Add freely; promote to a proper spec when one is ready to build.

## 2026-05-30 — Graham (chat)

1. **Additional photos per panel** — support more than one image on a single panel
   (gallery / multiple shots), rather than today's one-image-per-panel model.
2. **Pause / restart control** — playback controls for an auto-advancing walkthrough:
   pause where you are, restart from the beginning.
3. **Save position** — remember where the viewer left off so they can resume the
   walkthrough later rather than starting over.
4. **Cast to TV** — cast/stream a walkthrough to a TV for big-screen viewing.
   - **2026-05-30 decision:** scope is **Chrome → Google Cast only**. No iOS
     Safari / AirPlay support needed — sidesteps the web-Cast-API platform
     limitation. Use the Google Cast (CAF) sender SDK; needs a registered
     receiver app ID. R2 content is already public + CORS, so castable.
   - Status: deferred — 1–3 dispatched first (2026-05-30).

## 2026-09-13 — Graham (chat)

Same four raised again, verbatim: additional photos per panel; pause/restart
control; save position; cast to TV. Captured as asked — **not** a spec.

Status check against the shipped viewer (`public/index.html`) at the time of
capture — all four already exist in code:

| # | Idea | Where it lives now |
|---|------|--------------------|
| 1 | Additional photos per panel | `bin/walkthrough.py` — multi-image gallery panels (commit `fb8152d`), plus one-video-per-panel (`e9486a5`) |
| 2 | Pause / restart | `#nar-pause-btn` ⏸ and `#nar-restart-btn` ↺ narration controls |
| 3 | Save position | `localStorage['wt-pos-<wid>']`, with a resume prompt offering "Start over" |
| 4 | Cast to TV | Google Cast CAF sender wired to `CastContext`, deliberately on `DEFAULT_MEDIA_RECEIVER_APP_ID` — `prebake_cast.py` bakes a per-panel `cast.mp4` (1280x720 still + narration, held 600s at 1fps) into R2, so no custom receiver app is needed |

So the open question is not "build these" but **what's missing from what exists**.
Candidates to put to Graham:

- ~~**Cast receiver**~~ — *struck 2026-09-13.* I misread this on first capture.
  The default media receiver is the design, not a shortfall: `prebake_cast.py`
  bakes the panel into a video so the receiver has something real to play. The
  actual cast gaps are coverage and automation — see below.
- **Pause scope** — the controls pause *narration*. Whether panel auto-advance
  itself pauses/resumes as one unit is worth confirming on a real device.
- **Position scope** — position is per-walkthrough in browser localStorage, so it
  doesn't follow you across devices. Cross-device resume would need server state.
- **Gallery affordance** — multi-image panels exist in the authoring path; how
  they're navigated in the viewer (swipe? counter? thumbnails?) is unstated.

Next step when Graham wants it: pick which of the four is actually unsatisfying
in use, and spec *that* — rather than re-building what's there.

### 2026-09-13 — verification pass (Graham: "can we do 1 2 3 now — easy?")

Checked against the **live** site, not just the tree. `GET https://walkthrough.parkins.ai/`
(200, 87,781 bytes) contains `nar-pause-btn`, `nar-restart-btn`, `resume-yes`,
`wt-pos-` and the carousel scaffolding. So 1, 2 and 3 are not "easy" — they are
**already shipped and deployed**. No work to do.

Cast (4) is also largely built, and the feasibility question is really a
coverage question:

- `GET /api/walkthroughs/computing-heroes/asset/hinton/cast.mp4` → **200, 7.0 MB**.
- `GET /api/walkthroughs/isles-of-scilly-2026/asset/01-here-we-go/cast.mp4` → **200**.
- `GET /api/walkthroughs/munich-2026/asset/01-charlotte-and-millie/cast.mp4` → **404**.

Remaining work for cast, in order:

1. **`ALL_WALKTHROUGHS` is a hardcoded stale list** of four ids in
   `prebake_cast.py`. Munich (61 panels) and Maddy & Henry (37) were never baked.
2. **Baking is not wired into authoring.** Every panel added via `walkthrough-add`
   lands with no `cast.mp4` until someone re-runs the prebake by hand. This is the
   real fix — a hook at panel-add time, not a bigger list.
3. **Mid-flight uncommitted work** in `prebake_cast.py` (optional narration, silent
   slideshow, multi-image rotation at `PER_IMAGE_SECONDS`), `src/index.js`
   (entry-code manifest gating) and `bin/walkthrough.py`. Note the entry-code
   gating is **live but uncommitted** — deployed tree is ahead of git. Commit it.
4. **Never verified on an actual TV**, as far as the record shows. Chrome → Google
   Cast only, per the 2026-05-30 scope decision; no AirPlay.

## 2026-09-15 — Graham (chat): stream-on-the-fly instead of per-panel media

Graham's idea: rather than loading a discrete `cast.mp4` per panel, have a
Cloudflare Worker produce a *continuous* stream — hold the current image as the
video, play the queued audio over it, and push new images/audio into the stream
at chosen points. "Video on the fly."

**Why it's the right instinct.** The per-panel media model makes the TV a
passive player: each advance is a fresh media load, with a receiver
state-change and a visible seam. A continuous stream makes the TV a *display*
and the phone a *remote* — which is what casting a walkthrough should feel like.

**What doesn't work.** A Worker cannot encode video. No native binaries, JS/WASM
only, 128 MB per isolate — real-time transcoding is off the table. And
Cloudflare Stream Live is the wrong shape too: it accepts RTMPS/SRT from an
**external encoder** pushing a feed, and a Worker cannot be that encoder.

**What does work.** A live HLS stream is, on the wire, a text playlist plus
segment files. The Worker never has to *make* video — it has to *author the
playlist*. So:

1. Pre-encode each panel into HLS segments (a re-cut of the existing prebake,
   same ffmpeg step, different output container).
2. The Worker serves a rolling `EXT-X-PLAYLIST-TYPE:EVENT` playlist.
3. Advancing a panel appends that panel's segments to the playlist. The TV keeps
   pulling and simply continues.

This fits the platform properly: Workers have no enforced response-body limit
and no request-duration cap while a client stays connected, and CPU time is only
counted for actual compute — playlist authoring is string work.

Per-session state (current panel, what's queued, who's driving) wants a
**Durable Object**, one per cast session.

**Costs to weigh before building:**

- **Latency.** Live HLS lags by roughly segment-duration x buffer depth — expect
  2-6 s between "next" on the phone and the TV moving. Shorter segments cut it
  at the price of more requests.
- **Join discipline.** Every segment must share codec, resolution, timebase and
  audio parameters or the TV glitches at the joins. One encoder config, applied
  everywhere, non-negotiable.
- **It is a real build**, materially larger than the prebake. The prebake
  already delivers "play a walkthrough on the telly". This buys *control* —
  drive, pause, jump, live-advance — not basic playback.

Not scheduled. Captured for weighing against the prebake work now in flight.
