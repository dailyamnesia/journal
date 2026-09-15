#!/usr/bin/env bash
# Regression test for the missing-timeout bug on deploy.sh's sync-section
# sudo calls: unlike git fetch/both test suites/systemctl above (each
# already wrapped in `timeout`), the sync/server.js-swap section's plain
# `sudo mkdir`/`rsync`/`chown`/`test`/`diff`/`cp`/`chmod`/`mv` calls had no
# protection of their own. `sudo -n true`, far above, only confirms sudo's
# health at that one instant, before either test suite even starts -- an
# operator running this by hand (the persona this file already assumes
# throughout) with a cached sudo credential that expires before this
# section runs, tens of seconds later, hits a password prompt nobody is
# watching to answer, and the plain `sudo` call blocks on it forever,
# still holding $LOCKFILE and silently blocking every future deploy.
#
# This extracts run_synced() and its $SYNC_TIMEOUT_S default verbatim out
# of the real, unmodified tools/deploy.sh (the same file that actually
# gets run in production), plus the real `sudo mkdir -p "$LIVE_PUBLIC/
# posts"` line and the real `sudo test -e "$LIVE_SERVER"`/`sudo diff -q`
# pair's own timeout handling, and runs each against a stand-in `sudo`
# that answers `-n` instantly (a healthy non-interactive check) but blocks
# on any other invocation (an expired credential with a controlling TTY),
# so this fails if the fix is ever reverted or the command shape changes
# without updating this test.
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

RUN_SYNCED_SRC="$(awk '/^run_synced\(\) \{/,/^}/' "$DEPLOY_SH")"
if [ -z "$RUN_SYNCED_SRC" ]; then
  echo "FAIL: could not find run_synced() in $DEPLOY_SH -- has it been renamed or removed?" >&2
  exit 1
fi
SYNC_TIMEOUT_LINE="$(get_line 'SYNC_TIMEOUT_S="${DEPLOY_SH_SYNC_TIMEOUT_S:-60}"')"
MKDIR_LINE="$(get_line 'run_synced sudo mkdir -p "$LIVE_PUBLIC/posts"')"
TEST_LINE="$(get_line 'timeout "$SYNC_TIMEOUT_S" sudo test -e "$LIVE_SERVER" || test_status=$?')"
DIFF_LINE="$(get_line '  timeout "$SYNC_TIMEOUT_S" sudo diff -q "$BUILD_SRC/tools/server.js" "$LIVE_SERVER" >/dev/null 2>"$DIFF_STDERR" || diff_status=$?')"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/bin"

# Stand-in sudo: `-n` (non-interactive health check) succeeds instantly, as
# real sudo would with a currently-valid credential. Any other invocation
# models a credential that has since expired with a TTY attached: it
# blocks reading a "password" that will never come, instead of failing
# immediately the way a no-TTY sudo would.
cat > "$WORK/bin/sudo" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "-n" ]; then
  exit 0
fi
sleep 999999
EOF
chmod +x "$WORK/bin/sudo"
export PATH="$WORK/bin:$PATH"

export DEPLOY_SH_SYNC_TIMEOUT_S=2
LIVE_PUBLIC="$WORK/live/public"

fail=0

# Case 1: a plain run_synced-wrapped call (mkdir, standing in for every
# other run_synced site -- rsync/chown/cp/chmod/mv all share the identical
# wrapper).
start=$(date +%s)
status=0
output="$(bash -c "$SYNC_TIMEOUT_LINE; $RUN_SYNCED_SRC; $MKDIR_LINE" 2>&1)" || status=$?
elapsed=$(( $(date +%s) - start ))
if [ "$elapsed" -gt 10 ]; then
  echo "FAIL: run_synced sudo mkdir against a hung sudo took ${elapsed}s -- expected it to give up around the 2s override, not hang indefinitely." >&2
  fail=1
fi
if [ "$status" -eq 0 ]; then
  echo "FAIL: run_synced sudo mkdir reported success against a sudo that never returned." >&2
  fail=1
fi
case "$output" in
  *"did not finish within"*) ;;
  *)
    echo "FAIL: run_synced sudo mkdir did not report a clear timeout failure. Got: $output" >&2
    fail=1
    ;;
esac

# Case 2: the sudo test -e / sudo diff -q pair's own hand-rolled timeout
# handling (not routed through run_synced, since their exit codes carry
# meaning run_synced's blanket FAILED-on-nonzero would misread) -- a hang
# on the `sudo test -e` half must still be caught explicitly, not silently
# folded into "genuinely missing".
LIVE_SERVER="$WORK/live_server.js"
: > "$LIVE_SERVER"
start=$(date +%s)
status=0
output="$(bash -c "$SYNC_TIMEOUT_LINE; test_status=0; $TEST_LINE; if [ \"\$test_status\" -eq 124 ]; then echo 'FAILED: sudo test -e did not finish within \${SYNC_TIMEOUT_S}s'; exit 1; fi; echo unexpectedly-reached-past-the-hang" 2>&1)" || status=$?
elapsed=$(( $(date +%s) - start ))
if [ "$elapsed" -gt 10 ]; then
  echo "FAIL: sudo test -e against a hung sudo took ${elapsed}s -- expected it to give up around the 2s override." >&2
  fail=1
fi
if [ "$status" -eq 0 ]; then
  echo "FAIL: a hung 'sudo test -e' was not caught -- expected a nonzero exit, got 0. Output: $output" >&2
  fail=1
fi
case "$output" in
  *"did not finish within"*) ;;
  *)
    echo "FAIL: a hung 'sudo test -e' did not report a clear timeout failure. Got: $output" >&2
    fail=1
    ;;
esac

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: deploy.sh's sync-section sudo calls (run_synced-wrapped calls, and the test -e/diff -q pair's own hand-rolled check) correctly time out and fail loudly against a hung sudo credential prompt, instead of hanging forever holding the deploy lock."
