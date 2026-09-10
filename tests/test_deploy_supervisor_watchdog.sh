#!/usr/bin/env bash
# Regression test for the gap in deploy.sh's lock-holding-supervisor
# protection: SUPERVISOR_PPID and lock_file_was_replaced (just above
# watch_supervisor() in tools/deploy.sh) are only checked once, in the
# razor-thin instant right before the sync/deploy section starts. That
# section itself -- four rsync passes plus the server.js diff/cp/chmod/
# chown/mv sequence, all under sudo -- is not one instantaneous statement;
# on a real deploy it can run for a genuine stretch of wall-clock time, and
# the flock-holding supervisor (this script's own parent process once
# re-exec'd through `flock --close`) can die during that window exactly as
# easily as in the gap before it. Before this fix, nothing rechecked after
# that single point-in-time check, so a supervisor dying mid-sync silently
# released the lock with nothing in the script ever noticing -- a second,
# fully independent deploy.sh invocation could then acquire the same lock
# and run its own sync fully concurrently with the first one's
# still-in-progress sync, the exact two-racing-syncs scenario the lock
# exists to prevent in the first place.
#
# deploy.sh can't be `source`d directly to get at watch_supervisor() in
# isolation (see test_deploy_restart_timeout.sh for the same reasoning) --
# instead this extracts the current watch_supervisor() function body
# verbatim out of the real, unmodified tools/deploy.sh and evals just that,
# so this test exercises the real fixed code, not a hand-copied
# reimplementation that could drift out of sync.
#
# The test reproduces deploy.sh's own self-reexec flock shape (a real
# `flock --close` "supervisor" process holding the lock, with a child
# process -- standing in for the rest of deploy.sh's sync/deploy section --
# running underneath it), starts watch_supervisor() as a background job
# inside that child exactly as deploy.sh itself now does, then kills only
# the supervisor (modeling an OOM-killer or an operator targeting the
# most descriptive-looking `ps` line) while the child is still "mid-sync".
# Before this fix existed, nothing in deploy.sh would ever have noticed;
# with it, the child must receive SIGTERM (proven by hitting a trap that
# writes a marker file) within a couple of seconds of the supervisor dying,
# not merely at the next explicit check (there is no next check) or never.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SH="$REPO_ROOT/tools/deploy.sh"

FUNC_SRC="$(awk '/^watch_supervisor\(\) \{/,/^}/' "$DEPLOY_SH")"
if [ -z "$FUNC_SRC" ]; then
  echo "FAIL: could not find watch_supervisor() in $DEPLOY_SH -- has it been renamed or removed?" >&2
  exit 1
fi

WORK="$(mktemp -d)"
cleanup() {
  pkill -TERM -P $$ 2>/dev/null || true
  wait 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

FUNC_FILE="$WORK/func.sh"
printf '%s\n' "$FUNC_SRC" > "$FUNC_FILE"

LOCKFILE="$WORK/lock"
MARKER="$WORK/term_received"
CHILD_READY="$WORK/child_ready"

# The child: sources the real, extracted watch_supervisor(), starts it in
# the background exactly as deploy.sh itself does immediately after its
# lock_file_was_replaced check, then just sits -- standing in for the long
# sync/deploy section -- until either SIGTERM arrives (the fix working) or
# a generous bound elapses (the fix not working, i.e. the pre-fix
# behavior). A trap records that TERM actually arrived, since the default
# disposition would just kill the child silently.
cat > "$WORK/child.sh" <<EOF
#!/usr/bin/env bash
source "$FUNC_FILE"
LOCKFILE="$LOCKFILE"
# Derived the same way deploy.sh itself derives it (ps -o ppid=, not \$PPID
# -- see the SUPERVISOR_PPID comment in deploy.sh for why), not hardcoded:
# this script's own real parent, once launched via flock --close below, is
# the flock supervisor process, exactly mirroring production. Without this
# line SUPERVISOR_PPID stays empty and "kill -0 \"\\\$SUPERVISOR_PPID\""
# fails on its very first iteration regardless of whether the supervisor
# below is ever actually killed -- a false-positive PASS that doesn't
# exercise the fix at all. Confirmed by neutering the real kill -TERM
# "\$SUP_PID" below to a no-op: without this line the test still reported
# PASS; with it, it correctly reports FAIL (never receives TERM).
SUPERVISOR_PPID="\$(ps -o ppid= -p \$\$ | tr -d ' ')"
trap 'echo TERM > "$MARKER"; exit 0' TERM
watch_supervisor &
touch "$CHILD_READY"
for _ in \$(seq 1 200); do
  sleep 0.1
done
EOF
chmod +x "$WORK/child.sh"

DAILYAMNESIA_DEPLOY_LOCKED=1 flock -n --close -E 99 "$LOCKFILE" "$WORK/child.sh" &
CHILD_JOB=$!

for _ in $(seq 1 100); do
  [ -e "$CHILD_READY" ] && break
  sleep 0.1
done
if [ ! -e "$CHILD_READY" ]; then
  echo "FAIL: child never started (setup problem, not the fix under test)." >&2
  exit 1
fi

# Find the supervisor (the real `flock` process itself -- located by its
# comm and the fact that its cmdline mentions our own lockfile, since
# `pgrep -f "$WORK/child.sh"` alone also matches flock's own argv, which
# includes child.sh's path as the command it was told to run), then find
# the real child (running watch_supervisor) as *its* direct child.
SUP_PID=""
for _ in $(seq 1 50); do
  for cand in $(pgrep -x flock); do
    if tr '\0' ' ' < "/proc/$cand/cmdline" 2>/dev/null | grep -q -- "$LOCKFILE"; then
      SUP_PID="$cand"
      break
    fi
  done
  [ -n "$SUP_PID" ] && break
  sleep 0.1
done
if [ -z "$SUP_PID" ]; then
  echo "FAIL: could not find the running flock supervisor process (setup problem, not the fix under test)." >&2
  exit 1
fi
CHILD_PID=""
for _ in $(seq 1 50); do
  CHILD_PID="$(ps --ppid "$SUP_PID" -o pid= | tr -d ' ')"
  [ -n "$CHILD_PID" ] && break
  sleep 0.1
done
if [ -z "$CHILD_PID" ]; then
  echo "FAIL: could not find flock's child process (setup problem, not the fix under test)." >&2
  exit 1
fi

# Kill ONLY the supervisor, modeling it dying independently mid-deploy
# (OOM-killer, an operator's targeted kill) while the child -- standing in
# for the rest of deploy.sh's sync/deploy section -- is still running.
kill -TERM "$SUP_PID"

start=$(date +%s)
found=0
for _ in $(seq 1 50); do
  if [ -e "$MARKER" ]; then
    found=1
    break
  fi
  sleep 0.1
done
elapsed=$(( $(date +%s) - start ))

wait "$CHILD_JOB" 2>/dev/null || true

fail=0
if [ "$found" -ne 1 ]; then
  echo "FAIL: watch_supervisor() did not terminate the child within ${elapsed}s of its supervisor dying -- a supervisor death mid-sync would go completely undetected (the exact bug: SUPERVISOR_PPID/lock_file_was_replaced are only ever checked once, before the sync section starts)." >&2
  fail=1
else
  echo "watch_supervisor() delivered SIGTERM to the child ${elapsed}s after its supervisor died."
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: watch_supervisor() detects the lock-holding supervisor dying mid-section and aborts the deploy instead of silently letting the lock be released underneath it."
