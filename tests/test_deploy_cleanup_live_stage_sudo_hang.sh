#!/usr/bin/env bash
# Regression test for the missing-timeout bug on cleanup()'s own
# `sudo rm -f "$LIVE_STAGE"` line: unlike every other sudo call in the
# sync/server.js-swap section (all wrapped via run_synced()'s `timeout`, or
# the sudo test -e/diff -q pair's own hand-rolled timeout -- see
# tests/test_deploy_sudo_hang.sh), this one had no timeout of its own.
#
# Reaching this line means the server.js swap was genuinely interrupted
# mid-flight (LIVE_STAGE is only ever set while that swap is in progress --
# see tests/test_deploy_live_stage_leak.sh), and the most plausible way for
# that to happen is exactly the scenario run_synced() exists to guard
# against elsewhere: sudo's cached credential expired mid-deploy with a
# controlling TTY attached, so `run_synced sudo cp ...` itself hit its own
# $SYNC_TIMEOUT_S timeout and aborted -- leaving sudo still just as broken
# by the moment cleanup() runs a moment later. A `sudo` that blocks on a
# password prompt right here used to hang forever, and worse than an
# ordinary hang elsewhere in this file: cleanup()'s own first line already
# runs `trap '' TERM INT HUP QUIT`, so once execution reaches this point, an
# operator's plain Ctrl-C, `kill`, or a dropped SSH session's HUP can't stop
# it either -- only a hard SIGKILL can. The process sits there still holding
# $LOCKFILE, silently blocking every future deploy ("another deploy.sh is
# already running") with no FAILED message and no other symptom, exactly
# like every other now-fixed hang in this file.
#
# deploy.sh can't be `source`d directly to exercise cleanup() in isolation
# -- sourcing it runs the whole script. Instead this extracts cleanup() and
# the real $SYNC_TIMEOUT_S default verbatim out of the real, unmodified
# tools/deploy.sh (the same file that actually gets run in production), the
# same technique tests/test_deploy_live_stage_leak.sh and
# tests/test_deploy_sudo_hang.sh already use, so this fails if the fix is
# ever reverted or the relevant lines are renamed/restructured without
# updating this test.
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
SYNC_TIMEOUT_LINE="$(get_line 'SYNC_TIMEOUT_S="${DEPLOY_SH_SYNC_TIMEOUT_S:-60}"')"

WORK="$(mktemp -d)"
cleanup_work() { rm -rf "$WORK"; }
trap cleanup_work EXIT
mkdir -p "$WORK/bin"

# Stand-in sudo: models an expired credential with a controlling TTY
# attached -- it blocks reading a "password" that will never come, instead
# of failing immediately the way a no-TTY sudo would (that already-healthy
# case is what run_synced()/sudo -n true elsewhere in this file guard
# against; this test targets the one remaining call that wasn't guarded).
cat > "$WORK/bin/sudo" <<'SUDOEOF'
#!/usr/bin/env bash
sleep 999999
SUDOEOF
chmod +x "$WORK/bin/sudo"
export PATH="$WORK/bin:$PATH"

# Bound the real 60s default down to something a test can actually wait
# out, same override tests/test_deploy_sudo_hang.sh uses.
export DEPLOY_SH_SYNC_TIMEOUT_S=2

# Build a scratch script matching deploy.sh's real EXIT-trap shape: a
# LIVE_STAGE path staged (as if server.js's cp/chmod/chown/mv sequence was
# interrupted mid-swap), then exit, triggering cleanup() via the real trap.
cat > "$WORK/run.sh" <<RUNEOF
set -uo pipefail
BUILD_SRC="$WORK/build_src_unused"
BUILD_DIR="$WORK/build_dir_unused"
LIVE_STAGE=""
$SYNC_TIMEOUT_LINE
$CLEANUP_SRC
trap cleanup EXIT
LIVE_STAGE="$WORK/live_server.js.new"
exit 1
RUNEOF

start=$(date +%s)
status=0
# -k forces a hard SIGKILL if the plain TERM `timeout` sends first has no
# effect -- and against the unfixed code it won't: cleanup()'s own first
# line is `trap '' TERM INT HUP QUIT`, so once execution is inside
# cleanup() (which it is here, via the EXIT trap), an ordinary TERM is
# silently ignored, same as it would be against the real deploy.sh. Without
# -k this test would itself hang forever instead of reporting the failure.
# 15s/3s gives ample headroom over the 2s DEPLOY_SH_SYNC_TIMEOUT_S override.
timeout -k 3 15 bash "$WORK/run.sh" >"$WORK/out.log" 2>&1 || status=$?
elapsed=$(( $(date +%s) - start ))

fail=0

if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
  echo "FAIL: cleanup() did not finish within the ${DEPLOY_SH_SYNC_TIMEOUT_S}s override (waited up to 15s) against a hung sudo, and needed a hard SIGKILL to stop at all (an ordinary TERM/INT/HUP/QUIT is ignored once inside cleanup()) -- it hangs the whole script (and holds the deploy lock) with no bound. Output:" >&2
  cat "$WORK/out.log" >&2
  fail=1
elif [ "$elapsed" -gt 10 ]; then
  echo "FAIL: cleanup() took ${elapsed}s against a hung sudo -- expected it to give up around the ${DEPLOY_SH_SYNC_TIMEOUT_S}s override, not hang." >&2
  cat "$WORK/out.log" >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: cleanup() correctly times out (in ${elapsed}s) instead of hanging forever (or needing SIGKILL) against a hung sudo removing \$LIVE_STAGE."
