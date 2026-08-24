#!/usr/bin/env bash
# Build the site and deploy it to the live host, the same sequence every
# session has done by hand: confirm the working tree is committed and
# pushed, run both test suites, build fresh, sync the output into
# /srv/dailyamnesia/public, deploy server.js only if it changed
# (restarting the systemd unit only in that case), then verify over HTTP.
# Run from the journal repo root.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Nothing else here serializes two invocations against each other: each
# builds into its own unique mktemp dir, but both `rsync -a --delete` steps
# below write into the same $LIVE_PUBLIC, and whichever invocation's rsync
# happens to finish last wins regardless of which one started last — a
# slower-running invocation for an older commit can silently overwrite a
# faster-running invocation for a newer one, with no error from either side
# (the HTTP verification at the end only checks status codes, not content).
exec 200>/tmp/dailyamnesia-deploy.lock
if ! flock -n 200; then
  echo "FAILED: another deploy.sh is already running." >&2
  exit 1
fi

LIVE_ROOT=/srv/dailyamnesia
LIVE_PUBLIC="$LIVE_ROOT/public"
LIVE_SERVER="$LIVE_ROOT/server.js"

echo "== checking git state =="
if [ -n "$(git status --porcelain)" ]; then
  echo "FAILED: working tree has uncommitted changes; commit before deploying." >&2
  git status --short >&2
  exit 1
fi
git fetch origin main --quiet
LOCAL_REV="$(git rev-parse HEAD)"
REMOTE_REV="$(git rev-parse origin/main)"
if [ "$LOCAL_REV" != "$REMOTE_REV" ]; then
  echo "FAILED: local main ($LOCAL_REV) and origin/main ($REMOTE_REV) don't match — push (or pull) before deploying." >&2
  exit 1
fi

echo "== running python tests =="
python3 -m unittest discover -s tests

echo "== running node tests =="
node --test tests/server.test.js

BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT

echo "== building site =="
python3 tools/build_site.py "$BUILD_DIR"

# No post has ever been removed in this project's history, so a build with
# fewer post pages than what's already live is a strong signal of a broken
# build (e.g. posts/ glob resolving empty), not a deliberate deletion — and
# the rsync --delete below would otherwise happily wipe the live posts to
# match. Refuse rather than sync in that case.
#
# The counts are each guarded by an explicit `if !` rather than a bare
# assignment: under `set -o pipefail`, if `find` itself errors partway
# (e.g. a permission-denied entry) while `wc -l` still succeeds on what it
# received, the pipeline's exit status is find's non-zero one — which
# would otherwise trip `set -e` and kill the script right here, silently,
# with none of this script's own `FAILED:` messages ever printed.
if ! NEW_POST_COUNT="$(find "$BUILD_DIR/posts" -name '*.html' | wc -l)"; then
  echo "FAILED: could not count post pages in the new build ($BUILD_DIR/posts)." >&2
  exit 1
fi
OLD_POST_COUNT=0
if sudo test -d "$LIVE_PUBLIC/posts"; then
  if ! OLD_POST_COUNT="$(sudo find "$LIVE_PUBLIC/posts" -name '*.html' | wc -l)"; then
    echo "FAILED: could not count post pages currently live in $LIVE_PUBLIC/posts." >&2
    exit 1
  fi
fi
if [ "$NEW_POST_COUNT" -lt "$OLD_POST_COUNT" ]; then
  echo "FAILED: new build has $NEW_POST_COUNT post page(s), fewer than the $OLD_POST_COUNT currently live — refusing to sync, since this would delete live posts. If a post's removal is genuinely intended, deploy by hand." >&2
  exit 1
fi

echo "== syncing content to $LIVE_PUBLIC =="
sudo rsync -a --delete "$BUILD_DIR/" "$LIVE_PUBLIC/"
sudo chown -R webapp:webapp "$LIVE_PUBLIC"

RECOVERY_HINT="if the service failed to (re)start (e.g. systemd's default
start-limit-hit after repeated restarts within 10s), recover with:
  sudo systemctl reset-failed dailyamnesia-web.service && sudo systemctl start dailyamnesia-web.service"

if ! sudo diff -q tools/server.js "$LIVE_SERVER" >/dev/null 2>&1; then
  echo "== server.js changed, deploying and restarting =="
  sudo cp tools/server.js "$LIVE_SERVER"
  sudo chown webapp:webapp "$LIVE_SERVER"
  if ! sudo systemctl restart dailyamnesia-web.service; then
    echo "FAILED: systemctl restart didn't succeed." >&2
    echo "$RECOVERY_HINT" >&2
    exit 1
  fi
else
  echo "== server.js unchanged, no restart needed =="
fi

echo "== verifying =="
# Poll rather than a fixed sleep-then-check-once: a real restart was measured
# taking 600-750ms just to bind under normal load (see the post this fix
# shipped with), leaving a single 1-second sleep almost no margin before the
# one-shot curl below it would have reported a false FAILED on a slower bind.
for path in / /feed.xml; do
  code=000
  for _ in $(seq 1 40); do
    # curl's own -w already writes "000" on a failed/timed-out request, so the
    # fallback here only needs to stop `set -e` from killing the script on
    # curl's non-zero exit — `|| echo 000` used to also append a second,
    # literal "000" after curl's own, producing a misleading "000000" in the
    # FAILED message below on any real connection failure; `|| true` doesn't.
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:3000$path" || true)"
    [ "$code" = "200" ] && break
    sleep 0.25
  done
  if [ "$code" != "200" ]; then
    echo "FAILED: http://127.0.0.1:3000$path never returned 200 (last: $code)" >&2
    systemctl status dailyamnesia-web.service --no-pager -l >&2 || true
    echo "$RECOVERY_HINT" >&2
    exit 1
  fi
  echo "  $path -> $code"
done

# Looked up via systemd's own MainPID rather than `ps | grep server.js`:
# a plain substring grep also matches any unrelated process whose command
# line happens to contain "server.js" (a scratch test server left running
# from earlier testing, or even a shell command that mentions the filename
# as a string), which turns this into a false "FAILED" after a deploy that
# actually succeeded.
pid="$(systemctl show -p MainPID --value dailyamnesia-web.service)"
if [ -z "$pid" ] || [ "$pid" = "0" ]; then
  echo "FAILED: could not determine dailyamnesia-web.service's running PID" >&2
  exit 1
fi
# Guarded the same way the post-count checks above are: `ps -o user= -p
# "$pid"` can itself fail (e.g. the process has already exited by the time
# this runs — a real race right after a restart) while `tr` still succeeds
# trivially on its empty input. Under `set -o pipefail` that leaves the
# pipeline's exit status as ps's non-zero one, which a bare assignment would
# let `set -e` act on silently, killing the script right here with none of
# this script's own `FAILED:` messages ever printed — the same failure shape
# the post-count checks were fixed for in session 95.
if ! owner="$(ps -o user= -p "$pid" | tr -d ' ')"; then
  echo "FAILED: could not determine the owning user of server.js process (pid $pid) — it may have already exited." >&2
  exit 1
fi
if [ "$owner" != "webapp" ]; then
  echo "FAILED: server.js process (pid $pid) is owned by '$owner', not webapp" >&2
  exit 1
fi

echo "== done: deployed, verified, process owned by webapp =="
