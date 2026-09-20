#!/usr/bin/env bash
# Regression test for the missing-timeout bug on cleanup()'s own
# `git worktree remove --force --force "$BUILD_SRC"` line: the one remaining
# external/blocking call in the whole file that was left unwrapped by
# `timeout` (session 250) -- every sibling of this shape (git fetch, git
# worktree add, both test suites, all systemctl calls, every sudo call in
# the sync section, and even cleanup()'s own `sudo rm -f "$LIVE_STAGE"` a few
# lines below this one) already had one.
#
# The comment above `trap cleanup EXIT` already ruled out one hang risk here
# -- `git worktree remove` invokes no post-checkout-style hook, unlike
# `git worktree add` -- but that doesn't cover a wedged filesystem underneath
# $BUILD_SRC or this repo's own .git/worktrees/<id>/ metadata, the same
# threat model this file already treats as real for the OLD_POST_COUNT
# guard's `sudo find` against $LIVE_PUBLIC/posts. Because this line runs
# inside cleanup(), the EXIT trap that fires on every exit path, a hang here
# means the script never actually exits -- the flock supervisor keeps
# holding $LOCKFILE forever, silently blocking every future deploy with no
# FAILED message. Worse than most of this file's other hangs: cleanup()'s
# own first line already runs `trap '' TERM INT HUP QUIT`, so once execution
# reaches this point, an operator's plain Ctrl-C/kill/dropped-SSH-HUP can't
# stop it either -- only SIGKILL can.
#
# deploy.sh can't be `source`d directly to exercise cleanup() in isolation --
# sourcing it runs the whole script. Instead this extracts cleanup() and the
# real $SYNC_TIMEOUT_S default verbatim out of the real, unmodified
# tools/deploy.sh (the same file that actually gets run in production), the
# same technique tests/test_deploy_cleanup_live_stage_sudo_hang.sh and
# tests/test_deploy_live_stage_leak.sh already use, so this fails if the fix
# is ever reverted or the relevant lines are renamed/restructured without
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
case "$CLEANUP_SRC" in
  *'timeout "$SYNC_TIMEOUT_S" git worktree remove'*) ;;
  *)
    echo "FAIL: cleanup() no longer wraps 'git worktree remove' in timeout \"\$SYNC_TIMEOUT_S\" -- has the fix been reverted or the line restructured?" >&2
    exit 1
    ;;
esac
SYNC_TIMEOUT_LINE="$(get_line 'SYNC_TIMEOUT_S="${DEPLOY_SH_SYNC_TIMEOUT_S:-60}"')"

WORK="$(mktemp -d)"
cleanup_work() { rm -rf "$WORK"; }
trap cleanup_work EXIT
mkdir -p "$WORK/bin"

# Stand-in git: hangs only for 'worktree remove', passes every other
# subcommand through to the real binary -- models a wedged filesystem
# underneath $BUILD_SRC or .git/worktrees/<id>/, not real git's own
# behavior (which a fake binary couldn't faithfully model anyway).
cat > "$WORK/bin/git" <<'GITEOF'
#!/usr/bin/env bash
if [ "$1" = "worktree" ] && [ "$2" = "remove" ]; then
  sleep 999999
fi
exec /usr/bin/git "$@"
GITEOF
chmod +x "$WORK/bin/git"

# Bound the real 60s default down to something a test can actually wait out.
export DEPLOY_SH_SYNC_TIMEOUT_S=2

BUILD_SRC="$WORK/build_src"
mkdir -p "$BUILD_SRC"

# Build a scratch script matching deploy.sh's real EXIT-trap shape.
cat > "$WORK/run.sh" <<RUNEOF
set -uo pipefail
export PATH="$WORK/bin:\$PATH"
BUILD_SRC="$BUILD_SRC"
BUILD_DIR="$WORK/build_dir_unused"
LIVE_STAGE=""
DIFF_STDERR=""
$SYNC_TIMEOUT_LINE
$CLEANUP_SRC
trap cleanup EXIT
exit 1
RUNEOF

start=$(date +%s)
status=0
# -k forces a hard SIGKILL if the plain TERM `timeout` sends first has no
# effect -- and against the unfixed code it won't: cleanup()'s own first
# line is `trap '' TERM INT HUP QUIT`, so once execution is inside cleanup()
# (which it is here, via the EXIT trap), an ordinary TERM is silently
# ignored, same as it would be against the real deploy.sh. Without -k this
# test would itself hang forever instead of reporting the failure. 15s/3s
# gives ample headroom over the 2s DEPLOY_SH_SYNC_TIMEOUT_S override.
timeout -k 3 15 bash "$WORK/run.sh" >"$WORK/out.log" 2>&1 || status=$?
elapsed=$(( $(date +%s) - start ))

fail=0

if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
  echo "FAIL: cleanup() did not finish within the ${DEPLOY_SH_SYNC_TIMEOUT_S}s override (waited up to 15s) against a hung 'git worktree remove', and needed a hard SIGKILL to stop at all (an ordinary TERM/INT/HUP/QUIT is ignored once inside cleanup()) -- it hangs the whole script (and holds the deploy lock) with no bound. Output:" >&2
  cat "$WORK/out.log" >&2
  fail=1
elif [ "$elapsed" -gt 10 ]; then
  echo "FAIL: cleanup() took ${elapsed}s against a hung 'git worktree remove' -- expected it to give up around the ${DEPLOY_SH_SYNC_TIMEOUT_S}s override, not hang." >&2
  cat "$WORK/out.log" >&2
  fail=1
fi

if [ -d "$BUILD_SRC" ]; then
  echo "FAIL: \$BUILD_SRC ($BUILD_SRC) still exists after cleanup() gave up on the hung 'git worktree remove' -- the '|| rm -rf \"\$BUILD_SRC\"' fallback should still have removed it." >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: cleanup() correctly times out (in ${elapsed}s) and falls back to removing \$BUILD_SRC directly, instead of hanging forever (or needing SIGKILL), against a hung 'git worktree remove'."
