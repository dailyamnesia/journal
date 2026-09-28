#!/usr/bin/env bash
# Regression test for a gap in deploy.sh's own `timeout` protection: GNU
# `timeout N cmd`, used without `--kill-after` (every `timeout` call in this
# file, before this fix), does not actually bound wall-clock time against a
# child that survives the single SIGTERM `timeout` sends at the deadline.
# `timeout` sends that one SIGTERM and then blocks in its own wait() for the
# child to actually exit -- if the child doesn't die from it (traps or masks
# the signal, or is stuck in the uninterruptible D-state I/O this file's own
# comments already cite as the realistic cause of a "wedged NFS/FUSE mount"
# or a "PAM module blocked on an unresponsive directory service"), `timeout`
# keeps waiting past its configured deadline for exactly as long as the raw,
# unwrapped call would have.
#
# Every hang reproduced elsewhere in this file's own comments used a
# stand-in that dies immediately on a plain, untrapped SIGTERM (a bare
# `sleep 999999` or `while true`) -- exactly the one case bare `timeout`
# already handles correctly -- so none of those reproductions ever actually
# exercised this gap. This test uses a stand-in that traps and ignores TERM
# specifically (but is still an ordinary, killable process -- not simulating
# genuine D-state, which nothing can free short of the kernel's own I/O
# completing) to exercise it directly.
#
# run_synced() is used as the representative call site: it alone backs
# roughly a third of this file's `timeout`-wrapped calls (every sudo
# mkdir/rsync/chown/cp/chmod/mv in the sync section), so fixing it protects
# all of them at once, the same way this test checks all of them at once.
#
# deploy.sh can't be `source`d directly to exercise run_synced() in
# isolation -- sourcing it runs the whole script. Instead this extracts
# run_synced() verbatim: once from the pre-fix code (`git show HEAD` --
# HEAD is still the unmodified commit this fix is about to be applied on
# top of) to prove the bug is real against deploy.sh's own actual code, not
# just `timeout` in the abstract, and once from the real, unmodified,
# already-fixed tools/deploy.sh in the working tree to prove the fix closes
# it -- so this fails if the fix is ever reverted or the relevant lines are
# renamed/restructured without updating this test.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SH="$REPO_ROOT/tools/deploy.sh"

PRE_FIX_SRC="$(cd "$REPO_ROOT" && git show HEAD:tools/deploy.sh 2>/dev/null | awk '/^run_synced\(\) \{/,/^}/')"
if [ -z "$PRE_FIX_SRC" ]; then
  echo "FAIL: could not find run_synced() in HEAD's tools/deploy.sh -- has it been renamed, removed, or is HEAD no longer the pre-fix commit?" >&2
  exit 1
fi
case "$PRE_FIX_SRC" in
  *'--kill-after'*)
    echo "FAIL: HEAD's own run_synced() already contains --kill-after -- this test needs HEAD to be the commit *before* this fix, to prove the bug against deploy.sh's real pre-fix code. Has the fix already been committed?" >&2
    exit 1
    ;;
esac

POST_FIX_SRC="$(awk '/^run_synced\(\) \{/,/^}/' "$DEPLOY_SH")"
if [ -z "$POST_FIX_SRC" ]; then
  echo "FAIL: could not find run_synced() in $DEPLOY_SH -- has it been renamed or removed?" >&2
  exit 1
fi
case "$POST_FIX_SRC" in
  *'--kill-after="$TIMEOUT_KILL_AFTER_S"'*) ;;
  *)
    echo "FAIL: run_synced() in $DEPLOY_SH no longer wraps its call in timeout --kill-after=\"\$TIMEOUT_KILL_AFTER_S\" -- has the fix been reverted or the line restructured?" >&2
    exit 1
    ;;
esac

WORK="$(mktemp -d)"
cleanup_work() { rm -rf "$WORK"; }
trap cleanup_work EXIT

mkdir -p "$WORK/bin"

# Stand-in sudo: traps and ignores TERM, but is still an ordinary process a
# plain SIGKILL kills instantly -- not simulating genuine D-state I/O
# (nothing, including this fix, can bound that; see the file-level comment
# above TIMEOUT_KILL_AFTER_S in deploy.sh). Models a PAM module or a sudo
# build that masks the signal mid-operation instead of dying on it, the
# more ordinary real-world case `--kill-after` actually closes.
cat > "$WORK/bin/sudo" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-n" ]; then
  exit 0
fi
trap '' TERM
sleep 999999
EOF
chmod +x "$WORK/bin/sudo"
export PATH="$WORK/bin:$PATH"

fail=0

# Case 1: the bug, against deploy.sh's own real pre-fix code (HEAD). A bare
# `timeout "$SYNC_TIMEOUT_S" "$@"` must NOT bound execution against the
# TERM-ignoring stand-in above -- it should keep blocking well past the 2s
# configured bound, exactly like the raw, unwrapped call would. Guarded by
# an external `timeout -k 5 12` purely so this test itself doesn't hang
# forever if the bug is (as expected here) present; 12s is comfortably
# above the 2s bound this call is supposed to respect and comfortably below
# the 999999s the stand-in would otherwise sleep for.
start=$(date +%s)
status=0
timeout -k 5 12 bash -c "
set -uo pipefail
SYNC_TIMEOUT_S=2
$PRE_FIX_SRC
run_synced sudo mkdir -p /nonexistent
" >"$WORK/pre_fix.log" 2>&1
status=$?
elapsed=$(( $(date +%s) - start ))

if [ "$elapsed" -lt 8 ]; then
  echo "FAIL: pre-fix run_synced() returned in ${elapsed}s against a TERM-ignoring sudo -- expected it to still be blocked well past its own 2s bound (that's the bug this fix closes: bare 'timeout N cmd' does not actually bound a SIGTERM-surviving child). Either the repro stand-in isn't modeling the bug correctly, or HEAD is no longer the pre-fix commit this test assumes. Output:" >&2
  cat "$WORK/pre_fix.log" >&2
  fail=1
fi

# Case 2: the fix, against the real, unmodified, already-fixed
# tools/deploy.sh in the working tree. The same TERM-ignoring stand-in must
# now be bounded: timeout's own SIGTERM at the 2s deadline is ignored (same
# as case 1), but --kill-after's SIGKILL 1s later is not, so this must
# return within a few seconds, not linger anywhere near case 1's runaway
# duration.
start=$(date +%s)
status=0
bash -c "
set -uo pipefail
SYNC_TIMEOUT_S=2
TIMEOUT_KILL_AFTER_S=1
$POST_FIX_SRC
run_synced sudo mkdir -p /nonexistent
" >"$WORK/post_fix.log" 2>&1
status=$?
elapsed2=$(( $(date +%s) - start ))

if [ "$elapsed2" -gt 10 ]; then
  echo "FAIL: post-fix run_synced() took ${elapsed2}s against the same TERM-ignoring sudo -- expected it to give up within a few seconds of its 2s bound plus the 1s --kill-after grace, not hang indefinitely (the exact bug this fix is supposed to close)." >&2
  cat "$WORK/post_fix.log" >&2
  fail=1
fi
if [ "$status" -eq 0 ]; then
  echo "FAIL: post-fix run_synced() reported success against a sudo that never completed -- a bounded-but-still-failed call must not be read as success. Output:" >&2
  cat "$WORK/post_fix.log" >&2
  fail=1
fi
case "$(cat "$WORK/post_fix.log")" in
  *"did not finish within"*) ;;
  *)
    echo "FAIL: post-fix run_synced() did not report a clear timeout failure message. Output:" >&2
    cat "$WORK/post_fix.log" >&2
    fail=1
    ;;
esac

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: HEAD's pre-fix run_synced() left genuinely unbounded (${elapsed}s+) against a TERM-ignoring sudo, confirming the bug; the current, --kill-after-fixed run_synced() correctly bounded the identical call to ${elapsed2}s instead."
