#!/usr/bin/env bash
# Regression test for the missing-timeout bug on deploy.sh's
# `git worktree add --quiet --detach "$BUILD_SRC" "$LOCAL_REV"` call.
#
# Every other external call in deploy.sh that can block on something other
# than raw CPU -- git fetch, both test suites, all four systemctl calls,
# every sudo call in the sync section -- is wrapped in `timeout`. This one
# call site was missed: `git worktree add` performs a real checkout, which
# by default runs the repo's `post-checkout` hook (a clone-local
# .git/hooks/post-checkout, or a hooksPath template applied at clone time --
# e.g. husky/lefthook/direnv-style setups sometimes install one), and
# nothing about that hook is under deploy.sh's control or guaranteed to
# terminate (a hook that shells out to something that can itself hang on a
# slow/dead network endpoint is an entirely ordinary way for this to
# happen). Left unwrapped, a hanging hook wedges deploy.sh right there,
# still holding its lock file, with no FAILED message and no other symptom
# -- identical in shape to the already-fixed git-fetch/test-suite/systemctl
# hangs.
#
# deploy.sh can't be `source`d directly to exercise this logic in isolation
# -- sourcing it runs the whole script (git checks, the self-re-exec through
# flock, live rsync/sudo calls, etc.). This isn't inside its own function
# the way restart_service()/parent_is_flock() are, so instead this extracts
# the `git worktree add` block verbatim out of the real, unmodified
# tools/deploy.sh (the same file that actually gets run in production) via
# its own unique start ("WORKTREE_ADD_TIMEOUT_S=", which occurs exactly once
# in the file) and end (the first "fi" after it) markers, and evals just
# that block against a real scratch git repo with a real hanging
# post-checkout hook, so this test exercises the real fixed code, not a
# hand-copied reimplementation that could drift out of sync.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SH="$REPO_ROOT/tools/deploy.sh"

BLOCK_SRC="$(awk '/^WORKTREE_ADD_TIMEOUT_S=/,/^fi$/' "$DEPLOY_SH")"
if [ -z "$BLOCK_SRC" ]; then
  echo "FAIL: could not find the WORKTREE_ADD_TIMEOUT_S=... fi block in $DEPLOY_SH -- has it been renamed, restructured, or removed?" >&2
  exit 1
fi

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

# A real scratch git repo (not a stand-in), since the bug is in real git's
# own hook-invocation behavior during `worktree add`, not something a fake
# `git` binary could faithfully model.
SCRATCH_REPO="$WORK/scratch_repo"
mkdir -p "$SCRATCH_REPO"
git init --quiet "$SCRATCH_REPO"
git -C "$SCRATCH_REPO" config user.email "test@example.com"
git -C "$SCRATCH_REPO" config user.name "test"
echo "hello" > "$SCRATCH_REPO/file.txt"
git -C "$SCRATCH_REPO" add file.txt
git -C "$SCRATCH_REPO" commit --quiet -m "init"
LOCAL_REV="$(git -C "$SCRATCH_REPO" rev-parse HEAD)"

mkdir -p "$SCRATCH_REPO/.git/hooks"
cat > "$SCRATCH_REPO/.git/hooks/post-checkout" <<'EOF'
#!/usr/bin/env bash
sleep 999999
EOF
chmod +x "$SCRATCH_REPO/.git/hooks/post-checkout"

fail=0

# Case 1: the bug -- a hanging post-checkout hook must not hang the block
# forever; it must fail loudly within the (overridden, short) timeout.
BUILD_SRC="$WORK/build_src_1"
export DEPLOY_SH_WORKTREE_ADD_TIMEOUT_S=2
start=$(date +%s)
output="$(cd "$SCRATCH_REPO" && eval "$BLOCK_SRC" 2>&1)"
status=$?
elapsed=$(( $(date +%s) - start ))

if [ "$elapsed" -gt 15 ]; then
  echo "FAIL: the git-worktree-add block against a hanging post-checkout hook took ${elapsed}s -- expected it to give up around the 2s override, not hang indefinitely (the exact bug: no timeout of its own). Output:" >&2
  echo "$output" >&2
  fail=1
fi

if [ "$status" -eq 0 ]; then
  echo "FAIL: the git-worktree-add block reported success (exit 0) against a post-checkout hook that never returned -- a hang must not be read as a clean checkout. Output:" >&2
  echo "$output" >&2
  fail=1
fi

case "$output" in
  *"did not finish within"*) ;;
  *)
    echo "FAIL: the git-worktree-add block did not report a clear timeout failure message. Got:" >&2
    echo "$output" >&2
    fail=1
    ;;
esac

# Case 2: the legitimate case -- a normal repo with no slow hook must still
# check out cleanly and quickly, unaffected by the fix.
rm -f "$SCRATCH_REPO/.git/hooks/post-checkout"
BUILD_SRC="$WORK/build_src_2"
export DEPLOY_SH_WORKTREE_ADD_TIMEOUT_S=60
start=$(date +%s)
output2="$(cd "$SCRATCH_REPO" && eval "$BLOCK_SRC" 2>&1)"
status2=$?
elapsed2=$(( $(date +%s) - start ))

if [ "$status2" -ne 0 ]; then
  echo "FAIL: the git-worktree-add block failed (exit $status2) against a genuinely healthy repo with no slow hook. Output:" >&2
  echo "$output2" >&2
  fail=1
fi
if [ ! -e "$BUILD_SRC/file.txt" ]; then
  echo "FAIL: the git-worktree-add block did not actually check out $BUILD_SRC/file.txt against a genuinely healthy repo." >&2
  fail=1
fi
if [ "$elapsed2" -gt 10 ]; then
  echo "FAIL: the git-worktree-add block against a genuinely healthy repo took ${elapsed2}s -- expected it to complete almost instantly." >&2
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: deploy.sh's git worktree add call correctly times out and fails loudly against a hanging post-checkout hook (in ${elapsed}s), instead of hanging forever holding the deploy lock, while still checking out cleanly and quickly (in ${elapsed2}s) against a genuinely healthy repo."
