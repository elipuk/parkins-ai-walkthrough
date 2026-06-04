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
