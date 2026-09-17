#!/usr/bin/env bash
# Regression test for an unprivileged temp-file leak in deploy.sh's
# server.js diff/ambiguity check: `DIFF_STDERR="$(mktemp)"` captures
# `sudo diff`'s stderr, then is removed itself a couple of statements
# later. Before this fix, cleanup() never tracked $DIFF_STDERR at all --
# only $BUILD_SRC/$BUILD_DIR and (since a later fix) $LIVE_STAGE were.
# A TERM/INT/HUP/QUIT (an operator's Ctrl-C, a dropped SSH session, the
# lock-watchdog's own `kill -TERM "$$"`) landing while the `timeout
# "$SYNC_TIMEOUT_S" sudo diff ...` call between creation and removal is
# still running -- exactly the same hang window this file already treats
# as real for every other blocking sudo call -- left the mktemp file
# behind in /tmp forever, with nothing in this script ever noticing or
# cleaning it up on a later run.
#
# deploy.sh can't be `source`d directly to exercise cleanup() in isolation
# -- sourcing it runs the whole script. Instead this extracts cleanup()
# and the exact mktemp/diff/rm block verbatim out of the real, unmodified
# tools/deploy.sh by their own literal text, and runs them for real in a
# scratch harness that mirrors deploy.sh's actual `trap cleanup EXIT`
# shape -- so this fails if the fix is ever reverted or the relevant
# lines are renamed/restructured without updating this test.
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

MKTEMP_LINE="$(get_line '  DIFF_STDERR="$(mktemp)"')"
DIFF_LINE="$(get_line '  timeout "$SYNC_TIMEOUT_S" sudo diff -q "$BUILD_SRC/tools/server.js" "$LIVE_SERVER" >/dev/null 2>"$DIFF_STDERR" || diff_status=$?')"
CONTENT_LINE="$(get_line '  DIFF_STDERR_CONTENT="$(cat "$DIFF_STDERR")"')"
RM_LINE="$(get_line '  rm -f "$DIFF_STDERR"')"
SYNC_TIMEOUT_LINE="$(get_line 'SYNC_TIMEOUT_S="${DEPLOY_SH_SYNC_TIMEOUT_S:-60}"')"

WORK="$(mktemp -d)"
cleanup_work() { rm -rf "$WORK"; }
trap cleanup_work EXIT

mkdir -p "$WORK/bin" "$WORK/build_src/tools" "$WORK/live"
touch "$WORK/build_src/tools/server.js" "$WORK/live/server.js"

# Stand-in sudo: any other subcommand execs through instantly, but `diff`
# blocks forever -- modeling the call as still in flight when a signal
# lands, the same shape as this file's own $LIVE_STAGE leak repro.
cat > "$WORK/bin/sudo" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "diff" ]; then
  sleep 999
fi
exec "$@"
EOF
chmod +x "$WORK/bin/sudo"
export PATH="$WORK/bin:$PATH"

SCRIPT_PRELUDE=$(cat <<EOF
set -uo pipefail
BUILD_SRC="$WORK/build_src"
BUILD_DIR="$WORK/build_dir_unused"
LIVE_SERVER="$WORK/live/server.js"
LIVE_STAGE=""
DIFF_STDERR=""
diff_status=0
$SYNC_TIMEOUT_LINE
$CLEANUP_SRC
trap cleanup EXIT
EOF
)

# Scenario 1: killed while `sudo diff` is still running -- models the
# actual bug, a TERM/OOM/etc. landing exactly in that window.
cat > "$WORK/run_interrupted.sh" <<EOF
$SCRIPT_PRELUDE
$MKTEMP_LINE
echo "\$DIFF_STDERR" > "$WORK/diff_stderr_path"
$DIFF_LINE
$CONTENT_LINE
$RM_LINE
EOF

# Scenario 2: a full, uninterrupted comparison -- must remove its own
# scratch file on normal exit too (via the script's own explicit `rm -f`,
# not cleanup() -- this just confirms the happy path still leaves nothing
# behind).
cat > "$WORK/run_full.sh" <<EOF
$SCRIPT_PRELUDE
$MKTEMP_LINE
echo "\$DIFF_STDERR" > "$WORK/diff_stderr_path_full"
$RM_LINE
EOF

bash "$WORK/run_interrupted.sh" &
RUN_PID=$!
sleep 0.2
kill -TERM "$RUN_PID" 2>/dev/null || true
wait "$RUN_PID" 2>/dev/null

fail=0

DIFF_STDERR_PATH="$(cat "$WORK/diff_stderr_path" 2>/dev/null || true)"
if [ -z "$DIFF_STDERR_PATH" ]; then
  echo "FAIL: the interrupted run never recorded its own \$DIFF_STDERR path -- test harness itself is broken." >&2
  fail=1
elif [ -e "$DIFF_STDERR_PATH" ]; then
  echo "FAIL: a TERM landing mid-'sudo diff' left $DIFF_STDERR_PATH behind -- cleanup() did not remove the in-flight \$DIFF_STDERR temp file." >&2
  fail=1
fi

bash "$WORK/run_full.sh"
DIFF_STDERR_PATH_FULL="$(cat "$WORK/diff_stderr_path_full" 2>/dev/null || true)"
if [ -z "$DIFF_STDERR_PATH_FULL" ]; then
  echo "FAIL: the uninterrupted run never recorded its own \$DIFF_STDERR path -- test harness itself is broken." >&2
  fail=1
elif [ -e "$DIFF_STDERR_PATH_FULL" ]; then
  echo "FAIL: an uninterrupted run left its own \$DIFF_STDERR temp file behind after its explicit rm -f." >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: a TERM landing mid-'sudo diff' is cleaned up by cleanup(), and an uninterrupted run's own temp file is gone too."
