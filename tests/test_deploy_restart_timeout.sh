#!/usr/bin/env bash
# Regression test for the missing-timeout bug on deploy.sh's systemctl
# calls: `sudo systemctl restart dailyamnesia-web.service` (like the two
# already-guarded `timeout`-wrapped calls elsewhere in deploy.sh -- git
# fetch, both test suites) talks to systemd over D-Bus, and a wedged
# systemd manager or stopped-responding D-Bus broker leaves it blocked
# forever with nothing to time it out. That wedges deploy.sh right there,
# still holding its lock file, silently blocking every future deploy with
# no FAILED message. Before the fix, restart_service() (then just an
# inline `if ! sudo systemctl restart ...; then` guard) had no protection
# of its own against this.
#
# deploy.sh can't be `source`d directly to get at restart_service() in
# isolation -- sourcing it runs the whole script (git checks, the
# self-re-exec through flock, live rsync/sudo calls, etc.) rather than just
# defining functions. Instead this extracts the current restart_service()
# function body verbatim out of the real, unmodified tools/deploy.sh (the
# same file that actually gets run in production) and evals just that,
# so this test exercises the real fixed code, not a hand-copied
# reimplementation of it that could drift out of sync.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SH="$REPO_ROOT/tools/deploy.sh"

FUNC_SRC="$(awk '/^restart_service\(\) \{/,/^}/' "$DEPLOY_SH")"
if [ -z "$FUNC_SRC" ]; then
  echo "FAIL: could not find restart_service() in $DEPLOY_SH -- has it been renamed or removed?" >&2
  exit 1
fi
eval "$FUNC_SRC"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/bin"

# Stand-in systemd manager that has wedged: accepts the `restart`
# invocation but never returns, modeling a stopped-responding D-Bus broker.
cat > "$WORK/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "restart" ]; then
  sleep 999999
fi
exit 0
EOF
chmod +x "$WORK/bin/systemctl"

# Stand-in sudo that just execs its arguments -- no real root needed, since
# the hang under test lives entirely inside systemctl, not sudo.
cat > "$WORK/bin/sudo" <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF
chmod +x "$WORK/bin/sudo"

export PATH="$WORK/bin:$PATH"
export RECOVERY_HINT="(recovery hint stub for this test)"
# Bound the real 200s default down to something a test can actually wait
# out; restart_service() only honors this because the fix made the
# timeout an overridable local var for exactly this purpose.
export DEPLOY_SH_SYSTEMCTL_RESTART_TIMEOUT_S=2

start=$(date +%s)
status=0
output="$(restart_service 2>&1)" || status=$?
elapsed=$(( $(date +%s) - start ))

fail=0

if [ "$elapsed" -gt 10 ]; then
  echo "FAIL: restart_service() against a hung systemctl took ${elapsed}s -- expected it to give up around the 2s override, not hang indefinitely (the exact bug: no timeout of its own)." >&2
  fail=1
fi

if [ "$status" -eq 0 ]; then
  echo "FAIL: restart_service() reported success (exit 0) against a systemctl that never returned -- a hang must not be read as a clean restart." >&2
  fail=1
fi

case "$output" in
  *"did not finish within"*) ;;
  *)
    echo "FAIL: restart_service() did not report a clear timeout failure message. Got:" >&2
    echo "$output" >&2
    fail=1
    ;;
esac

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: restart_service() correctly times out (in ${elapsed}s) and fails loudly against a hung systemctl, instead of hanging forever."
