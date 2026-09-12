#!/usr/bin/env bash
# Regression test for the missing-timeout bug on the fourth, previously
# unfixed systemctl call site in deploy.sh's post-deploy HTTP verification
# block: `systemctl status dailyamnesia-web.service --no-pager -l` (run only
# when / or /feed.xml never comes back 200, to print diagnostics before
# aborting).
#
# systemctl talks to systemd over D-Bus, and a wedged systemd manager or a
# stopped-responding D-Bus broker leaves any call to it blocked forever --
# exactly the hazard already fixed for the other three systemctl call sites
# in this file (`is-active` in the verification block above this one,
# `restart` inside restart_service(), and `show -p MainPID` further down,
# all wrapped in `timeout`). This fourth call site never got the same
# treatment: it's reached specifically when the site has just failed to
# come up, arguably the moment a wedged systemd/D-Bus manager is *most*
# plausible, not least. Left unwrapped, a wedged systemctl here hangs this
# deploy indefinitely, still holding its lock file, before ever reaching
# $RECOVERY_HINT or `exit 1` -- silently blocking every future deploy with
# no FAILED message ever printed, the identical failure shape already fixed
# for is-active/restart/show.
#
# deploy.sh can't be `source`d directly to exercise this block in isolation
# (see test_deploy_server_diff_missing_live.sh for the same reasoning) --
# this isn't inside its own function the way restart_service()/
# parent_is_flock() are, so instead this extracts the verification for-loop
# block verbatim out of the real, unmodified tools/deploy.sh via its own
# unique, unambiguous start (the literal `for path in / /feed.xml; do` line,
# which occurs exactly once) and end (the first *unindented* `done`, which
# closes the outer loop -- the inner retry loop's own `done` is indented and
# so doesn't match) markers, and evals just that block, so this test
# exercises the real code, not a hand-copied reimplementation that could
# drift out of sync.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SH="$REPO_ROOT/tools/deploy.sh"

BLOCK_SRC="$(awk '/^for path in \/ \/feed.xml; do$/,/^done$/' "$DEPLOY_SH")"
if [ -z "$BLOCK_SRC" ]; then
  echo "FAIL: could not find the 'for path in / /feed.xml; do ... done' verification block in $DEPLOY_SH -- has it been renamed, restructured, or removed?" >&2
  exit 1
fi

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/bin"

# Stand-in curl: always reports connection failure (000), modeling a service
# that never comes up, to drive the real block into its FAILED branch (and
# on to the systemctl status call under test) quickly and deterministically,
# without needing a real HTTP server at all.
cat > "$WORK/bin/curl" <<'EOF'
#!/usr/bin/env bash
echo -n "000"
exit 7
EOF
chmod +x "$WORK/bin/curl"

# Stand-in systemd manager that has wedged: accepts the `status` invocation
# but never returns, modeling a stopped-responding D-Bus broker -- the exact
# scenario already fixed for `is-active`/`restart`/`show` elsewhere in this
# same file.
cat > "$WORK/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "status" ]; then
  sleep 999999
fi
exit 0
EOF
chmod +x "$WORK/bin/systemctl"

export PATH="$WORK/bin:$PATH"
export RECOVERY_HINT="(recovery hint stub for this test)"

start=$(date +%s)
# Bounded well above the real worst case, but far below "forever": before
# ever reaching the systemctl call under test, the block's own inner retry
# loop already burns up to 40 * 0.25s = ~10s against a curl stub that
# always fails, so the real expected total once the fix is in place is
# ~10s (retry loop) + 30s (the fix's own internal timeout) = ~40s, not the
# 30s the fix wraps the call in alone. (An earlier version of this test
# used a 40s outer bound and a 35s "did it hang" threshold, both too tight
# to survive that ~10s of retry-loop time and so failed even against the
# real fix -- confirmed by reproducing the false failure directly before
# widening these.) Pre-fix, this external timeout is the only thing that
# ever stops the run (the block itself has no protection of its own) and
# reliably fires here since ~40s < 70s; post-fix, the block finishes on its
# own in ~40-41s (measured directly, several runs), comfortably under the
# 55s "did it hang" threshold below and the 70s outer bound.
output="$(timeout 70 bash -c "$BLOCK_SRC" 2>&1)"
status=$?
elapsed=$(( $(date +%s) - start ))

fail=0

if [ "$status" -eq 124 ]; then
  echo "FAIL: the verification block hung past the outer 70s test bound entirely (elapsed ${elapsed}s) -- the fourth systemctl call site (status) has no timeout of its own and a wedged systemd/D-Bus manager blocks it forever." >&2
  fail=1
fi

if [ "$elapsed" -gt 55 ]; then
  echo "FAIL: the verification block took ${elapsed}s against a hung 'systemctl status' -- expected it to give up around the ~10s retry loop plus the 30s internal timeout (~40s total), not hang indefinitely (the exact bug: this call site was never wrapped in 'timeout')." >&2
  fail=1
fi

if [ "$status" -eq 0 ]; then
  echo "FAIL: the verification block reported success (exit 0) even though curl never returned 200 -- it must still fail the deploy." >&2
  fail=1
fi

case "$output" in
  *"FAILED: http://127.0.0.1:3000/ never returned 200"*) ;;
  *)
    echo "FAIL: the verification block did not report the expected 'never returned 200' FAILED message. Got:" >&2
    echo "$output" >&2
    fail=1
    ;;
esac

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: the verification block's 'systemctl status' diagnostic call correctly times out (in ${elapsed}s) instead of hanging forever against a wedged systemd/D-Bus manager."
