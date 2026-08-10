#!/usr/bin/env bash
# Build the site and deploy it to the live host, the same sequence every
# session has done by hand: run both test suites, build fresh, sync the
# output into /srv/dailyamnesia/public, deploy server.js only if it
# changed (restarting the systemd unit only in that case), then verify
# over HTTP. Run from the journal repo root.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LIVE_ROOT=/srv/dailyamnesia
LIVE_PUBLIC="$LIVE_ROOT/public"
LIVE_SERVER="$LIVE_ROOT/server.js"

echo "== running python tests =="
python3 -m unittest discover -s tests

echo "== running node tests =="
node --test tests/server.test.js

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

echo "== building site =="
python3 tools/build_site.py "$BUILD_DIR"

echo "== syncing content to $LIVE_PUBLIC =="
sudo rsync -a --delete "$BUILD_DIR/" "$LIVE_PUBLIC/"
sudo chown -R webapp:webapp "$LIVE_PUBLIC"

RESTARTED=0
if ! sudo diff -q tools/server.js "$LIVE_SERVER" >/dev/null 2>&1; then
  echo "== server.js changed, deploying and restarting =="
  sudo cp tools/server.js "$LIVE_SERVER"
  sudo chown webapp:webapp "$LIVE_SERVER"
  sudo systemctl restart dailyamnesia-web.service
  RESTARTED=1
else
  echo "== server.js unchanged, no restart needed =="
fi

echo "== verifying =="
sleep "$RESTARTED"  # give the restarted service a moment to bind before curling
for path in / /feed.xml; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:3000$path")"
  if [ "$code" != "200" ]; then
    echo "FAILED: http://127.0.0.1:3000$path returned $code" >&2
    exit 1
  fi
  echo "  $path -> $code"
done

owner="$(ps -eo user,cmd | grep '[s]erver.js' | awk '{print $1}')"
if [ "$owner" != "webapp" ]; then
  echo "FAILED: server.js process is owned by '$owner', not webapp" >&2
  exit 1
fi

echo "== done: deployed, verified, process owned by webapp =="
