#!/usr/bin/env bash
# Regression test for deploy.sh's SERVER_CHANGED diff logic wrongly
# aborting a genuine first deploy of server.js.
#
# Session 196 fixed `sudo diff -q A B` conflating a real divergence (exit 1,
# empty stderr) with sudo refusing to run diff at all (exit 1, non-empty
# stderr) by treating any non-empty stderr as "ambiguous, refuse" -- but
# that rule itself conflates a third, perfectly legitimate case: $LIVE_SERVER
# not existing yet at all (a real first deploy, the same scenario the
# post-count guard (session 141) and the `mkdir -p "$LIVE_PUBLIC/posts"` fix
# (session 153) elsewhere in this same file already went out of their way to
# keep working). `diff -q A B` against a missing B exits 2 and writes
# "diff: B: No such file or directory" to stderr -- nonzero exit, nonempty
# stderr, exactly what the post-196 check reads as "ambiguous, refuse" --
# so a genuine first deploy of server.js hit the "could not reliably
# compare" FAILED branch and aborted, even though nothing about sudo or the
# comparison was ever actually in question.
#
# deploy.sh can't be `source`d directly to exercise this logic in isolation
# -- sourcing it runs the whole script (git checks, the self-re-exec
# through flock, live rsync/sudo calls, etc.). This isn't inside its own
# function the way restart_service()/parent_is_flock() are, so instead this
# extracts the diff/SERVER_CHANGED block verbatim out of the real,
# unmodified tools/deploy.sh (the same file that actually gets run in
# production) via its own unique, unambiguous start ("SERVER_CHANGED=false",
# which occurs exactly once in the file, immediately before this whole
# section's comments) and end (the first *unindented* "fi", which closes
# the outer if/else -- the nested if/fi above it is indented and so doesn't
# match) markers, and evals just that block, so this test exercises the
# real code, not a hand-copied reimplementation that could drift out of
# sync. Anchored on SERVER_CHANGED=false rather than a line inside the
# fixed logic itself so the same extraction also works unmodified against
# the pre-fix shape of this block (used below to confirm this test actually
# fails against it).
# shellcheck disable=SC2154 # diff_status is assigned by the eval'd $BLOCK_SRC below, not visible to static analysis
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SH="$REPO_ROOT/tools/deploy.sh"

BLOCK_SRC="$(awk '/^SERVER_CHANGED=false$/,/^fi$/' "$DEPLOY_SH")"
if [ -z "$BLOCK_SRC" ]; then
  echo "FAIL: could not find the SERVER_CHANGED=false ... fi block in $DEPLOY_SH -- has it been renamed, restructured, or removed?" >&2
  exit 1
fi

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

mkdir -p "$WORK/bin"

# Stand-in sudo that just execs its arguments -- no real root needed, since
# this test only needs real sudo's *behavior* around a missing destination
# file, which plain diff/test already reproduce faithfully without root.
cat > "$WORK/bin/sudo" <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF
chmod +x "$WORK/bin/sudo"
export PATH="$WORK/bin:$PATH"

BUILD_SRC="$WORK/build_src"
mkdir -p "$BUILD_SRC/tools"
echo "console.log('new server');" > "$BUILD_SRC/tools/server.js"

fail=0

# Case 1: the bug -- LIVE_SERVER doesn't exist yet at all (a genuine first
# deploy). Must be read as "changed", not aborted.
LIVE_SERVER="$WORK/live_server_missing.js"
rm -f "$LIVE_SERVER"
status1_out="$(eval "$BLOCK_SRC"; echo "RESULT:diff_status=$diff_status")"
status1_exit=$?
case "$status1_out" in
  *"RESULT:diff_status=1"*) ;;
  *)
    echo "FAIL: a missing \$LIVE_SERVER (genuine first deploy) was not read as diff_status=1 (\"changed\"). Got:" >&2
    echo "$status1_out" >&2
    fail=1
    ;;
esac
if [ "$status1_exit" -ne 0 ]; then
  echo "FAIL: the diff block exited non-zero ($status1_exit) against a missing \$LIVE_SERVER -- a genuine first deploy must not abort the whole script. Output:" >&2
  echo "$status1_out" >&2
  fail=1
fi

# Case 2: LIVE_SERVER exists and genuinely differs -- must still be "changed".
LIVE_SERVER="$WORK/live_server_diff.js"
echo "console.log('old server');" > "$LIVE_SERVER"
status2_out="$(eval "$BLOCK_SRC"; echo "RESULT:diff_status=$diff_status")"
status2_exit=$?
case "$status2_out" in
  *"RESULT:diff_status=1"*) ;;
  *)
    echo "FAIL: a genuinely different \$LIVE_SERVER was not read as diff_status=1 (\"changed\"). Got:" >&2
    echo "$status2_out" >&2
    fail=1
    ;;
esac
if [ "$status2_exit" -ne 0 ]; then
  echo "FAIL: the diff block exited non-zero ($status2_exit) against a genuinely different \$LIVE_SERVER. Output:" >&2
  echo "$status2_out" >&2
  fail=1
fi

# Case 3: LIVE_SERVER exists and is identical -- must be "unchanged".
LIVE_SERVER="$WORK/live_server_same.js"
cp "$BUILD_SRC/tools/server.js" "$LIVE_SERVER"
status3_out="$(eval "$BLOCK_SRC"; echo "RESULT:diff_status=$diff_status")"
status3_exit=$?
case "$status3_out" in
  *"RESULT:diff_status=0"*) ;;
  *)
    echo "FAIL: an identical \$LIVE_SERVER was not read as diff_status=0 (\"unchanged\"). Got:" >&2
    echo "$status3_out" >&2
    fail=1
    ;;
esac
if [ "$status3_exit" -ne 0 ]; then
  echo "FAIL: the diff block exited non-zero ($status3_exit) against an identical \$LIVE_SERVER. Output:" >&2
  echo "$status3_out" >&2
  fail=1
fi

# Case 4: session 196's own case -- sudo refuses to run diff specifically
# (a sudoers gap), while LIVE_SERVER genuinely exists. Must still abort with
# the "could not reliably compare" FAILED message, not be silently folded
# into "changed" the way this exact case used to be, pre-session-196.
cat > "$WORK/bin/sudo" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = "diff" ]; then
  echo "sudo: sorry, user deploy is not allowed to execute '/usr/bin/diff' on this host" >&2
  exit 1
fi
exec "$@"
EOF
chmod +x "$WORK/bin/sudo"
LIVE_SERVER="$WORK/live_server_diff.js"
status4_out="$(eval "$BLOCK_SRC" 2>&1)"
status4_exit=$?
if [ "$status4_exit" -eq 0 ]; then
  echo "FAIL: sudo refusing to run diff specifically (a sudoers gap) was not caught -- the diff block exited 0 instead of aborting with a FAILED message. Output:" >&2
  echo "$status4_out" >&2
  fail=1
fi
case "$status4_out" in
  *"could not reliably compare"*) ;;
  *)
    echo "FAIL: sudo refusing to run diff specifically did not produce the expected \"could not reliably compare\" FAILED message. Got:" >&2
    echo "$status4_out" >&2
    fail=1
    ;;
esac

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: the SERVER_CHANGED diff logic correctly treats a missing \$LIVE_SERVER (first deploy) as \"changed\" instead of aborting, while still correctly handling a genuine difference, a genuine match, and sudo refusing diff specifically."
