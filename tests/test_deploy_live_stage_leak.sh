#!/usr/bin/env bash
# Regression test for a root-owned leftover leak in deploy.sh's server.js
# swap: `sudo cp "$BUILD_SRC/tools/server.js" "$LIVE_SERVER.new"` writes a
# root-owned staged file outside $BUILD_SRC/$BUILD_DIR entirely, and before
# this fix nothing in cleanup() ever removed it -- only $BUILD_SRC and
# $BUILD_DIR were tracked. A TERM/INT/HUP/QUIT/OOM landing between that cp
# and the later `sudo mv "$LIVE_SERVER.new" "$LIVE_SERVER"` (the same causes
# already covered throughout this file for every other live-write step) left
# a root-owned, possibly-partial `.new` file behind forever: no later deploy
# run looks for or removes a stale `.new` unless it happens to write a fresh
# one over the same fixed name first.
#
# deploy.sh can't be `source`d directly to exercise cleanup() in isolation
# -- sourcing it runs the whole script. Instead this extracts cleanup()
# verbatim out of the real, unmodified tools/deploy.sh via its own unique
# start ("cleanup() {") and matching end ("^}"), plus the exact
# `LIVE_STAGE="$LIVE_SERVER.new"` / `sudo cp ...` lines by their literal
# text, and runs them for real in a scratch harness that mirrors deploy.sh's
# actual `trap cleanup EXIT` shape -- so this fails if the fix is ever
# reverted or the relevant lines are renamed/restructured without updating
# this test.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SH="$REPO_ROOT/tools/deploy.sh"

get_line() {
  local pattern="$1"
  local line
  line="$(grep -F -m1 -x "$pattern" "$DEPLOY_SH" || true)"
  if [ -z "$line" ]; then
    echo "FAIL: could not find expected line in $DEPLOY_SH: $pattern -- has it been renamed or removed?" >&2
    exit 1
  fi
  echo "$line"
}

CLEANUP_SRC="$(awk '/^cleanup\(\) \{/,/^}/' "$DEPLOY_SH")"
if [ -z "$CLEANUP_SRC" ]; then
  echo "FAIL: could not find cleanup() in $DEPLOY_SH -- has it been renamed or removed?" >&2
  exit 1
fi

SET_LIVE_STAGE="$(get_line '  LIVE_STAGE="$LIVE_SERVER.new"')"
CP_LINE="$(get_line '  sudo cp "$BUILD_SRC/tools/server.js" "$LIVE_STAGE"')"
CHMOD_LINE="$(get_line '  sudo chmod 644 "$LIVE_STAGE"')"
CHOWN_LINE="$(get_line '  sudo chown webapp:webapp "$LIVE_STAGE"')"
MV_LINE="$(get_line '  sudo mv "$LIVE_STAGE" "$LIVE_SERVER"')"
CLEAR_LIVE_STAGE="$(get_line '  LIVE_STAGE=""')"

WORK="$(mktemp -d)"
cleanup_work() { rm -rf "$WORK"; }
trap cleanup_work EXIT

mkdir -p "$WORK/bin" "$WORK/build_src/tools" "$WORK/live"

# Stand-in sudo: no real root needed, since this test only needs the real
# cleanup()/cp/mv behavior around ordinary scratch files it already owns --
# except `chown webapp:webapp`, which a non-root test invocation can't
# actually do, and doesn't need to: only the swap's presence/absence
# matters here, not which user ends up owning the file.
cat > "$WORK/bin/sudo" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "chown" ]; then
  exit 0
fi
exec "$@"
EOF
chmod +x "$WORK/bin/sudo"
export PATH="$WORK/bin:$PATH"

# cleanup()'s own `git worktree remove ... || rm -rf "$BUILD_SRC"` fallback
# (correctly) wipes $BUILD_SRC on every exit, matching real production
# deploy.sh where it's a throwaway per-run temp worktree -- so this test's
# own scratch source file has to be recreated before each invocation below,
# the same way a real deploy gets a fresh $BUILD_SRC every time it runs.
setup_build_src() {
  rm -rf "$WORK/build_src"
  mkdir -p "$WORK/build_src/tools"
  # A large-ish source file so the cp below takes long enough to interrupt
  # mid-transfer, the same shape as this file's own comment's SIGTERM repro.
  dd if=/dev/zero of="$WORK/build_src/tools/server.js" bs=1M count=200 status=none
}

SCRIPT_PRELUDE=$(cat <<EOF
set -uo pipefail
BUILD_SRC="$WORK/build_src"
BUILD_DIR="$WORK/build_dir_unused"
LIVE_SERVER="$WORK/live/server.js"
LIVE_STAGE=""
$CLEANUP_SRC
trap cleanup EXIT
EOF
)

# Scenario 1: killed between the cp and the later chmod/chown/mv -- models
# the actual bug, a TERM/OOM/etc. landing exactly in that window.
cat > "$WORK/run_interrupted.sh" <<EOF
$SCRIPT_PRELUDE
$SET_LIVE_STAGE
$CP_LINE
EOF

# Scenario 2: a full, uninterrupted swap -- must NOT have its staged file
# deleted out from under it, and must leave the final $LIVE_SERVER in place.
cat > "$WORK/run_full.sh" <<EOF
$SCRIPT_PRELUDE
$SET_LIVE_STAGE
$CP_LINE
$CHMOD_LINE
$CHOWN_LINE
$MV_LINE
$CLEAR_LIVE_STAGE
EOF

setup_build_src
bash "$WORK/run_interrupted.sh" &
RUN_PID=$!
sleep 0.1
kill -TERM "$RUN_PID" 2>/dev/null || true
wait "$RUN_PID" 2>/dev/null

fail=0

if [ -e "$WORK/live/server.js.new" ]; then
  echo "FAIL: a TERM landing mid-copy left a root-owned $WORK/live/server.js.new behind -- cleanup() did not remove the in-flight staged file." >&2
  fail=1
fi

# A normal, uninterrupted run must complete the swap for real: the final
# $LIVE_SERVER must exist, and cleanup() must not delete it or leave a
# stray .new behind now that LIVE_STAGE was cleared after the mv.
setup_build_src
bash "$WORK/run_full.sh"
if [ ! -e "$WORK/live/server.js" ]; then
  echo "FAIL: an uninterrupted run's final \$LIVE_SERVER was missing after cleanup() ran on normal exit." >&2
  fail=1
fi
if [ -e "$WORK/live/server.js.new" ]; then
  echo "FAIL: an uninterrupted run left a stray server.js.new behind after a completed swap." >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: a TERM landing between the server.js cp and its later mv is cleaned up by cleanup(), while an uninterrupted run's own staged file survives untouched."
