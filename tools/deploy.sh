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

LOCKFILE=/tmp/dailyamnesia-deploy.lock

# Nothing else here serializes two invocations against each other: each
# builds into its own unique mktemp dir, but both `rsync -a --delete` steps
# below write into the same $LIVE_PUBLIC, and whichever invocation's rsync
# happens to finish last wins regardless of which one started last — a
# slower-running invocation for an older commit can silently overwrite a
# faster-running invocation for a newer one, with no error from either side
# (the HTTP verification at the end only checks status codes, not content).
#
# Locking via a self-re-exec through `flock --close`, not a plain
# `exec 200>lock; flock -n 200` in this same shell: a plain `exec`-opened fd
# has no close-on-exec bit, so it's inherited by every subprocess this script
# spawns (the test suites, rsync, sudo, and whatever any of those in turn
# fork) — and by extension, by any grandchild that outlives its own immediate
# parent (gets reparented instead of reaped), which then holds the flock
# indefinitely with no connection to this script even still existing. The
# `cleanup()` trap below only reaches direct children of this script's own
# PID, so it can't reach a reparented grandchild either. Reproduced directly:
# a scratch script matching the old exec+flock+trap-cleanup shape exactly,
# made to spawn one detached grandchild the way a test runner's
# `child_process.spawn` (fired-and-forgotten, never awaited) can, exited
# clean — but the very next invocation, seconds later, failed with "another
# deploy.sh is already running" against zero real deploy-related processes,
# because the orphaned grandchild alone still held the lock fd. `flock
# --close` closes the lock fd inside the process it launches (and everything
# that process forks) before ever running it, so nothing downstream can ever
# inherit it — verified against the same reproduction: the fixed version's
# lock fd is held by nobody once the script exits, orphaned grandchild or
# not, and a concurrent second invocation is still correctly rejected while a
# first is genuinely still running.
if [ "${DAILYAMNESIA_DEPLOY_LOCKED:-}" != 1 ]; then
  status=0
  DAILYAMNESIA_DEPLOY_LOCKED=1 flock -n --close -E 99 "$LOCKFILE" "$REPO_ROOT/tools/deploy.sh" "$@" || status=$?
  if [ "$status" -eq 99 ]; then
    echo "FAILED: another deploy.sh is already running." >&2
    exit 1
  fi
  exit "$status"
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

# Export the exact verified commit right now, before either test suite runs:
# the two suites together take upwards of 55 seconds, and until this export
# existed, nothing re-checked git state between this point and the "== building
# site ==" step below, which read source files straight from this live
# checkout. build_site.py derives every source path (POSTS_DIR, CHARTER_PATH,
# STATIC_DIR) from its own `__file__`, so building the exported copy instead
# makes the deployed content immune to anything that happens to this live
# checkout for the rest of the script's run, rather than merely narrowing the
# window with a re-check that would itself still race the build's own file
# reads. Reproduced directly: a scratch harness matching this
# check-then-sleep-then-build shape exactly shipped a real uncommitted edit
# made during the simulated test-suite window when building straight from the
# live tree; building from a `git archive` export of the just-verified commit
# instead left the edit out, even though `git status` still showed it
# afterward. BUILD_SRC is set up alongside BUILD_DIR's own cleanup below,
# since both are temp dirs this script owns for the rest of its run.
BUILD_SRC="$(mktemp -d)"
BUILD_DIR=""
# A bare `trap 'rm -rf "$BUILD_DIR"' EXIT` doesn't wait for a still-running
# foreground child (the `sudo rsync` below) before running: a TERM/INT
# delivered directly to this script's own PID (not its whole process group —
# e.g. `timeout`, a targeted `kill`, an OOM-killer reaping just the shell)
# kills bash immediately, the EXIT trap fires right away, and rsync is left
# orphaned mid-transfer, reading from a $BUILD_DIR that's already being
# deleted out from under it. Reproduced directly: killing the script mid-sync
# left `sudo rsync` running detached, throwing "file has vanished" for every
# not-yet-opened file, silently landing a partial deploy with no FAILED
# message. `pkill -P $$` plus `wait` ensures any child (sudo, and whatever it
# spawns) is actually dead before cleanup runs.
cleanup() {
  pkill -TERM -P $$ 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$BUILD_SRC" "$BUILD_DIR"
}
trap cleanup EXIT
git archive "$LOCAL_REV" | tar -x -C "$BUILD_SRC"

echo "== running python tests =="
python3 -m unittest discover -s tests

echo "== running node tests =="
node --test tests/server.test.js

BUILD_DIR="$(mktemp -d)"

echo "== building site =="
python3 "$BUILD_SRC/tools/build_site.py" "$BUILD_DIR"

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

# The lock acquired via `flock --close` above is held by the flock
# supervisor process (this script's own parent in the process tree), not by
# this process itself — that fork is unavoidable once --close is in play,
# since closing this process's own copy of the fd right before it started
# running means *something else* has to be left holding it. That supervisor
# can die independently of this actual deploy still running — killed
# directly (e.g. an operator debugging a "stuck" deploy targets the most
# descriptive-looking `ps` line, which is the supervisor's) or by an
# OOM-killer preferring an idle process blocked in wait() over an actively-
# running rsync/test child, the same threat model the cleanup() comment
# above already treats as real. If that happens, this lock is released the
# instant the supervisor dies, and a second invocation can already be
# running concurrently against the same $LIVE_PUBLIC by the time this
# process reaches the rsync below — exactly the two-racing-rsyncs scenario
# the whole lock exists to prevent. Bash's own $PPID is cached at shell
# startup and won't reflect the supervisor's death (it keeps reporting the
# supervisor's original, now-dead PID), so the live value has to be looked
# up fresh via `ps`, not read from $PPID. Reproduced directly with a scratch
# copy of this exact lock shape: killing only the supervisor mid-run let the
# simulated sync step run to completion, unsupervised, every time; this
# check, added right before the one genuinely irreversible step, catches it
# and aborts instead.
if [ "$(ps -o ppid= -p $$ | tr -d ' ')" = "1" ]; then
  echo "FAILED: this deploy's lock-holding process is gone (reparented to init) — a concurrent deploy may already be running; refusing to sync." >&2
  exit 1
fi

echo "== syncing content to $LIVE_PUBLIC =="
# --delete-delay, not plain --delete: plain --delete defaults to
# delete-during, which removes each now-extraneous destination file as
# rsync gets to it, interleaved with (and not necessarily after) copying
# in its replacement. If this rsync is interrupted partway (operator
# Ctrl-C, OOM-kill, a full disk) during a post rename or content update,
# a file can be deleted before its replacement finishes copying in,
# leaving that page live-404 on the site until the next successful
# deploy. Verified with a scratch rsync interrupted mid-transfer while
# renaming a file: plain --delete lost the file in every trial across a
# range of interrupt timings; --delete-delay queues all deletions and
# only applies them after every file has finished copying, so the same
# interruption left the old (still-valid) file in place every time.
# Two ordered passes, not one: rsync transfers files in the order it walks
# the source tree (top-level entries alphabetically, before recursing into
# subdirectories), so a single rsync across the whole tree can write
# index.html/feed.xml — already updated to link a brand-new post — before
# that post's own page has been copied into posts/, leaving a live 404
# behind a link the homepage/feed itself just started advertising, for
# however long the transfer takes. --delete-delay above only defers
# *deletions* to the end of the transfer; it's a different race and doesn't
# help here. Reproduced directly: a scratch rsync of a large new post file
# throttled with --bwlimit showed the new index.html (linking the new post)
# already live while the post file itself didn't exist yet, for the whole
# multi-second transfer. Posts go out first and finish completely; only
# then does the second pass publish the top-level pages that link to them.
sudo rsync -a --delete-delay "$BUILD_DIR/posts/" "$LIVE_PUBLIC/posts/"
sudo rsync -a --delete-delay --exclude='/posts/' "$BUILD_DIR/" "$LIVE_PUBLIC/"
sudo chown -R webapp:webapp "$LIVE_PUBLIC"

RECOVERY_HINT="if the service failed to (re)start (e.g. systemd's default
start-limit-hit after repeated restarts within 10s), recover with:
  sudo systemctl reset-failed dailyamnesia-web.service && sudo systemctl start dailyamnesia-web.service"

# The restart decision can't be gated on the server.js diff alone: if the
# service is down for a reason unrelated to a code change (a prior restart
# that itself failed, an OOM-kill, a manual stop) and this deploy has no
# server.js changes to push, diff-only gating takes the "unchanged, no
# restart needed" branch and leaves the service down — and since the diff
# keeps reporting "unchanged" on every later run too, re-running deploy.sh
# can never bring it back on its own; only a manual `systemctl start` can.
SERVER_CHANGED=false
if ! sudo diff -q tools/server.js "$LIVE_SERVER" >/dev/null 2>&1; then
  echo "== server.js changed, deploying =="
  sudo cp tools/server.js "$LIVE_SERVER"
  sudo chown webapp:webapp "$LIVE_SERVER"
  SERVER_CHANGED=true
fi

if [ "$SERVER_CHANGED" = true ] || ! sudo systemctl is-active --quiet dailyamnesia-web.service; then
  echo "== (re)starting service =="
  if ! sudo systemctl restart dailyamnesia-web.service; then
    echo "FAILED: systemctl restart didn't succeed." >&2
    echo "$RECOVERY_HINT" >&2
    exit 1
  fi
else
  echo "== server.js unchanged and service already running, no restart needed =="
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
