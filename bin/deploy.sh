#!/bin/zsh
# Deploy the walkthrough Worker and refuse to call it deployed until the live
# site actually serves what is on disk.
#
# Why this exists: on 2026-09-15 `wrangler deploy --env beta` printed
# "Uploaded walkthrough-beta" and succeeded, and the site went on serving the
# previous index.html. An identical second deploy picked the change up. A clean
# wrangler exit is not evidence — see docs/verification-traps.md.
#
# Usage: bin/deploy.sh beta | production
set -eu

ENVIRONMENT="${1:-}"
if [[ "$ENVIRONMENT" != "beta" && "$ENVIRONMENT" != "production" ]]; then
  echo "usage: bin/deploy.sh beta|production" >&2
  exit 2
fi

REPO="${0:A:h:h}"
cd "$REPO"

if [[ "$ENVIRONMENT" == "production" ]]; then
  HOST="https://walkthrough.parkins.ai"
else
  HOST="https://walkthrough-beta.parkins-d93jed72hfs91np.workers.dev"
fi

export CLOUDFLARE_API_TOKEN="$(security find-generic-password -a eli -s 'eli/cloudflare-parkins-ai-token' -w)"
if [[ -z "$CLOUDFLARE_API_TOKEN" ]]; then
  echo "FAIL: no Cloudflare token in keychain" >&2
  exit 1
fi

echo "→ deploying to $ENVIRONMENT ($HOST)"
npx wrangler deploy --env "$ENVIRONMENT"

# Propagation is not instant, and mid-rollout a probe can hit either version.
# Poll, cache-busted, until the served HTML matches disk — or give up loudly.
LOCAL="public/index.html"
SERVED="$(mktemp)"
trap 'rm -f "$SERVED"' EXIT

for attempt in {1..12}; do
  sleep 5
  if curl -fsS "$HOST/?deploycheck=$RANDOM$RANDOM" -o "$SERVED"; then
    if cmp -s "$SERVED" "$LOCAL"; then
      echo "✓ $ENVIRONMENT serves $(wc -c < "$LOCAL" | tr -d ' ') bytes, byte-identical to $LOCAL (attempt $attempt)"
      exit 0
    fi
  fi
  echo "  attempt $attempt: served $(wc -c < "$SERVED" | tr -d ' ') B vs local $(wc -c < "$LOCAL" | tr -d ' ') B — waiting"
done

echo "FAIL: $HOST is not serving $LOCAL after 12 attempts." >&2
echo "      wrangler exited 0 but the deploy did not take. Re-run before believing it." >&2
exit 1
