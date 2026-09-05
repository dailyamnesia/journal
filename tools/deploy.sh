#!/usr/bin/env bash
# Build the site and deploy it to the live host, the same sequence every
# session has done by hand: confirm the working tree is committed and
# pushed, run both test suites, build fresh, sync the output into
# /srv/dailyamnesia/public, deploy server.js only if it changed
# (restarting the systemd unit only in that case), then verify over HTTP.
# Run from the journal repo root.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LOCKFILE=/tmp/dailyamnesia-deploy.lock

# Nothing else here serializes two invocations against each other: each
# builds into its own unique mktemp dir, but both `rsync -a --delete` steps
# below write into the same $LIVE_PUBLIC, and whichever invocation's rsync
# happens to finish last wins regardless of which one started last — a
# slower-running invocation for an older commit can silently overwrite a
# faster-running invocation for a newer one, with no error from either side
# (the HTTP verification at the end only checks status codes, not content).
#
# Locking via a self-re-exec through `flock --close`, not a plain
# `exec 200>lock; flock -n 200` in this same shell: a plain `exec`-opened fd
# has no close-on-exec bit, so it's inherited by every subprocess this script
# spawns (the test suites, rsync, sudo, and whatever any of those in turn
# fork) — and by extension, by any grandchild that outlives its own immediate
# parent (gets reparented instead of reaped), which then holds the flock
# indefinitely with no connection to this script even still existing. The
# `cleanup()` trap below only reaches direct children of this script's own
# PID, so it can't reach a reparented grandchild either. Reproduced directly:
# a scratch script matching the old exec+flock+trap-cleanup shape exactly,
# made to spawn one detached grandchild the way a test runner's
# `child_process.spawn` (fired-and-forgotten, never awaited) can, exited
# clean — but the very next invocation, seconds later, failed with "another
# deploy.sh is already running" against zero real deploy-related processes,
# because the orphaned grandchild alone still held the lock fd. `flock
# --close` closes the lock fd inside the process it launches (and everything
# that process forks) before ever running it, so nothing downstream can ever
# inherit it — verified against the same reproduction: the fixed version's
# lock fd is held by nobody once the script exits, orphaned grandchild or
# not, and a concurrent second invocation is still correctly rejected while a
# first is genuinely still running.
#
# The boolean sentinel above isn't trusted on its own: it's an ordinary
# environment variable, inherited from whatever process invokes this script,
# not something only this script's own re-exec can set. If it's already `1`
# in the caller's environment for any unrelated reason (most plausibly: an
# operator who was poking at the guarded body by hand while debugging a
# stuck deploy — exactly the persona already assumed elsewhere in this
# script — and still has it exported in that same shell afterward), a
# perfectly ordinary direct invocation skips the `flock` line entirely,
# never creates $LOCKFILE, and runs the rest of the script completely
# unlocked, defeating fix #1 above outright. Reproduced directly: two
# scratch invocations run concurrently with `DAILYAMNESIA_DEPLOY_LOCKED=1`
# pre-exported both entered the guarded body at once, with no lock file ever
# created; without the var pre-set, the same two concurrent invocations
# correctly serialized, one rejected with "already running". Requiring the
# immediate parent to actually be `flock` (checked fresh via `ps`, same
# freshness discipline as the reparented-supervisor check further down, not
# a cached value) closes it: re-running the identical pre-exported-var
# reproduction with this check in place made both invocations fall through
# to the real `flock` line, and the second was correctly rejected.
parent_is_flock() {
  [ "$(ps -o comm= -p "$PPID" 2>/dev/null)" = "flock" ]
}
# $LOCKFILE lives in /tmp: world-writable (the sticky bit only stops other
# users from deleting or renaming an entry that's already there, not from
# creating a new name that doesn't exist yet), at a fully predictable path.
# `flock` opens whatever path it's given by following symlinks like any
# other open(2) caller — it has no equivalent of O_NOFOLLOW — so any local
# user who plants a symlink at this exact path *before* this script has ever
# created a real lock file there (a genuine, recurring window: any host with
# /tmp on tmpfs starts every reboot with the path missing) silently
# redirects `flock`'s open() onto whatever path they chose. `flock -n
# --close` never writes content, so it can't corrupt an existing file at the
# target, but it does create-if-missing — letting an unrelated local user
# get an empty file silently created, owned by whoever next runs this
# perfectly ordinary deploy, at any path that user can write to but the
# attacker couldn't. Reproduced directly: a scratch harness matching this
# exact self-re-exec `flock` shape, run completely normally after a symlink
# was pre-planted at its lock path pointing at a not-yet-existing file
# elsewhere, silently created that file — nothing printed, exit 0,
# indistinguishable from an ordinary successful run. Refusing outright when
# $LOCKFILE already exists as a symlink (a plain, real lock file this script
# itself creates never is one) closes the window: once a real lock file
# exists here, the sticky bit stops any other user from ever replacing it
# with a symlink again, so this only needs to hold once, right before the
# path is trusted.
if [ -L "$LOCKFILE" ]; then
  echo "FAILED: $LOCKFILE is a symlink, not a real lock file -- refusing to use it (this script never creates it as one; something else planted it, and flock would silently follow it wherever it points)." >&2
  exit 1
fi
if [ "${DAILYAMNESIA_DEPLOY_LOCKED:-}" != 1 ] || ! parent_is_flock; then
  status=0
  DAILYAMNESIA_DEPLOY_LOCKED=1 flock -n --close -E 99 "$LOCKFILE" "$REPO_ROOT/tools/deploy.sh" "$@" || status=$?
  if [ "$status" -eq 99 ]; then
    echo "FAILED: another deploy.sh is already running." >&2
    exit 1
  fi
  exit "$status"
fi

LIVE_ROOT=/srv/dailyamnesia
LIVE_PUBLIC="$LIVE_ROOT/public"
LIVE_SERVER="$LIVE_ROOT/server.js"

echo "== checking git state =="
# `git status --porcelain`'s own exit status was discarded here: embedded
# directly inside `$( ... )` as an argument to `[ -n ... ]`, its failure
# can't reach `set -e` at all, the same masking already fixed elsewhere in
# this script (NEW_POST_COUNT/OLD_POST_COUNT/the process-owner lookup) for
# a pipeline under `pipefail` -- this is the same failure shape one layer
# simpler, no pipe needed, just a command substitution used as an argument.
# `git status` can fail outright with nothing on stdout (a corrupted
# `.git/index` -- disk-full mid-write, an interrupted `git add`, plain bit
# rot -- makes it exit 128 with "fatal: .git/index: index file smaller
# than expected" and zero stdout), and neither `git rev-parse HEAD` nor
# `git fetch` need the index at all, so both keep succeeding right after
# it. `[ -n "" ]` is false, so this guard -- meant to hard-stop the deploy
# on uncommitted work -- silently reports the tree "clean" and the script
# sails on to build and ship $LOCAL_REV with no FAILED message and no
# indication the check never actually ran, hiding both a real
# uncommitted change and a corrupted index that deserves its own
# attention. Reproduced directly: a scratch repo with an uncommitted edit
# and a truncated `.git/index` had this exact line pass straight through
# while `git status --porcelain` itself exited 128 with empty stdout.
# Capturing the command's own exit status first, the same guarded-
# assignment shape already used for the post-count and owner checks below,
# closes it.
if ! GIT_STATUS_OUTPUT="$(git status --porcelain)"; then
  echo "FAILED: could not determine git working tree status (git status --porcelain failed) -- refusing to guess whether the tree is clean." >&2
  exit 1
fi
if [ -n "$GIT_STATUS_OUTPUT" ]; then
  echo "FAILED: working tree has uncommitted changes; commit before deploying." >&2
  git status --short >&2
  exit 1
fi
# Same hang risk already fixed for the python/node test suites below (session
# 176), just at an earlier call site: `git fetch` talks to a real network
# endpoint with no timeout of its own, and TCP accepting a connection is no
# guarantee anything on the other end ever answers -- a stuck/overloaded git
# host behind a load balancer or reverse proxy that completes the handshake
# itself, or any other middlebox black-holing the response, leaves `git
# fetch` blocked forever with nothing to time it out. That wedges this
# script right here, still holding $LOCKFILE, silently blocking every future
# deploy ("another deploy.sh is already running") with no FAILED message and
# no other symptom, exactly like the two already-fixed test-suite hangs.
# Reproduced directly: a scratch repo with `origin` pointed at a bare TCP
# listener that accepts the connection and never responds hung `git fetch
# origin main --quiet` indefinitely (confirmed via an external `timeout`,
# since the command had no protection of its own). 60s is generous headroom
# over a real fetch against a healthy remote, which completes in well under
# a second.
if ! timeout 60 git fetch origin main --quiet; then
  echo "FAILED: git fetch origin main did not finish within 60s (or failed) -- a hung or unresponsive remote would otherwise hold this deploy's lock forever, silently blocking every future deploy until killed by hand." >&2
  exit 1
fi
LOCAL_REV="$(git rev-parse HEAD)"
REMOTE_REV="$(git rev-parse origin/main)"
if [ "$LOCAL_REV" != "$REMOTE_REV" ]; then
  echo "FAILED: local main ($LOCAL_REV) and origin/main ($REMOTE_REV) don't match — push (or pull) before deploying." >&2
  exit 1
fi

# Check out the exact verified commit right now, before either test suite
# runs: the two suites together take upwards of 55 seconds, and until this
# checkout existed, nothing re-checked git state between this point and the
# "== building site ==" step below, which read source files straight from
# this live checkout. build_site.py derives every source path (POSTS_DIR,
# CHARTER_PATH, STATIC_DIR) from its own `__file__`, so building the checked-
# out copy instead makes the deployed content immune to anything that happens
# to this live checkout for the rest of the script's run, rather than merely
# narrowing the window with a re-check that would itself still race the
# build's own file reads. Reproduced directly: a scratch harness matching
# this check-then-sleep-then-build shape exactly shipped a real uncommitted
# edit made during the simulated test-suite window when building straight
# from the live tree; building from a pinned copy of the just-verified commit
# instead left the edit out, even though `git status` still showed it
# afterward.
#
# This has to be a real `git worktree add`, not a `git archive | tar -x`
# export (the first version of this fix) — an archive has no `.git` at all,
# and build_site.py's `_first_commit_time()` shells out to `git log --follow`
# with `cwd=REPO_ROOT` to break ties between same-date posts. Against an
# archive, every single post silently hits the `CalledProcessError` fallback
# (`UNCOMMITTED_SENTINEL`, meant only for a genuinely new, not-yet-committed
# post) with no error surfaced anywhere, so *every* same-date group's real
# chronological order collapsed into the initial glob's plain alphabetical
# order instead, on every full rebuild — silently reordering the entire
# site's history, not just new posts. A linked worktree keeps a `.git` file
# pointing back at this repo's real object database, so `git log` still
# works correctly, while still giving its own independent working directory
# and index — editing a file in this live checkout does not touch a linked
# worktree's copy of it. Confirmed directly: `_first_commit_time()` returned
# the sentinel for every post when pointed at a `git archive` export, and the
# real, correct timestamp when pointed at a `git worktree add --detach`
# checkout of the identical commit; a live edit made to this checkout after
# creating the worktree left the worktree's own file untouched. BUILD_SRC is
# set up alongside BUILD_DIR's own cleanup below, since both are temp
# directories/worktrees this script owns for the rest of its run.
BUILD_SRC="$(mktemp -d)"
BUILD_DIR=""
# A bare `trap 'rm -rf "$BUILD_DIR"' EXIT` doesn't wait for a still-running
# foreground child (the `sudo rsync` below) before running: a TERM/INT
# delivered directly to this script's own PID (not its whole process group —
# e.g. `timeout`, a targeted `kill`, an OOM-killer reaping just the shell)
# kills bash immediately, the EXIT trap fires right away, and rsync is left
# orphaned mid-transfer, reading from a $BUILD_DIR that's already being
# deleted out from under it. Reproduced directly: killing the script mid-sync
# left `sudo rsync` running detached, throwing "file has vanished" for every
# not-yet-opened file, silently landing a partial deploy with no FAILED
# message. `pkill -P $$` plus `wait` ensures any child (sudo, and whatever it
# spawns) is actually dead before cleanup runs. `git worktree remove` (not a
# plain `rm -rf`) is needed for $BUILD_SRC specifically, since a linked
# worktree is also registered under this repo's own `.git/worktrees/` —
# deleting just the directory leaves that registration behind as a
# perpetually "prunable" entry instead of actually cleaning up. Needs
# `--force` twice, not once: a single `--force` still refuses to remove a
# *locked* worktree ("cannot remove a locked working tree") — nothing in
# this script locks $BUILD_SRC itself, but an external actor can (an
# operator inspecting a stuck deploy, backup/AV software scanning /tmp), and
# that failure was silently swallowed by the `2>/dev/null`, falling through
# to the `rm -rf` fallback — which deletes the directory but never the
# `.git/worktrees/` registration, leaking it exactly the way a plain
# `rm -rf` alone always did. Reproduced directly: locking a real linked
# worktree, then running this exact fallback line, left the directory gone
# but the registration listed forever after by `git worktree list`;
# `--force --force` removes both.
cleanup() {
  # Ignore further TERM/INT for the rest of cleanup, once it's started: only
  # EXIT is trapped above, not TERM/INT themselves, so bash's default,
  # untrapped disposition for those (immediate termination) still applies
  # while this function is already running as the EXIT trap. A *second*
  # TERM/INT arriving while `wait` below is still blocked on the pkill'd
  # child (e.g. an operator who sent one signal, saw no immediate effect
  # since rsync/git take a moment to actually exit, and sent another) kills
  # this process outright, mid-cleanup — before `git worktree remove` or
  # `rm -rf "$BUILD_DIR"` ever run, leaking both the temp build dir and the
  # worktree's registration under this repo's own `.git/worktrees/` forever,
  # even though the child itself (already signaled by the first TERM's
  # pkill) went on to exit cleanly on its own. Reproduced directly: a scratch
  # harness matching this exact trap-cleanup shape, sent TERM twice in
  # quick succession (second one ~0.8s after the first, well before the
  # pkill'd child's own ~3s graceful-shutdown delay), never reached its
  # `rm -rf` line at all — the marker directory that line removes was still
  # sitting there afterward, untouched, with the child's own cleanup file
  # inside it. Adding this ignore-trap as cleanup's first line, then rerunning
  # the identical double-TERM timing, let `wait` block until the child
  # actually finished and cleanup run to completion every time — the marker
  # directory was gone afterward, as intended.
  #
  # HUP and QUIT need the same ignore, not just a repeat of whichever signal
  # started cleanup: the operator persona this whole fix is built around
  # doesn't necessarily send the same signal twice -- someone who sent one
  # TERM, saw no immediate effect, and then closed their terminal (SIGHUP to
  # this script's whole session) or hit Ctrl-\ out of frustration (SIGQUIT)
  # is at least as plausible as sending TERM again, and a dropped SSH
  # session mid-deploy delivering a single HUP is an even more ordinary way
  # for this to happen with no impatience involved at all. Neither was
  # covered by `trap '' TERM INT` alone. Reproduced directly: a scratch
  # harness matching this exact trap-cleanup shape, sent TERM (entering
  # cleanup, matching the pkill+wait window already described above) and
  # then a single HUP about half a second later, died immediately -- the
  # marker file cleanup writes last was never created, leaking
  # $BUILD_SRC's worktree registration and $BUILD_DIR the same way an
  # unprotected double-TERM did before that fix. Adding HUP and QUIT to
  # this same ignore-trap and rerunning the identical TERM-then-HUP timing
  # let cleanup run to completion every time.
  trap '' TERM INT HUP QUIT
  pkill -TERM -P $$ 2>/dev/null || true
  wait 2>/dev/null || true
  git worktree remove --force --force "$BUILD_SRC" 2>/dev/null || rm -rf "$BUILD_SRC"
  rm -rf "$BUILD_DIR"
}
trap cleanup EXIT
git worktree add --quiet --detach "$BUILD_SRC" "$LOCAL_REV"

# Both suites run against $BUILD_SRC, not this live checkout, for the same
# reason the build step below does: tests/test_build_site.py and
# tests/server.test.js each resolve the module under test relative to their
# own file location (Path(__file__).resolve() / require('../tools/server.js')),
# so discovering them from the live tree exercises the live tree's code, not
# the $BUILD_SRC snapshot of $LOCAL_REV that actually gets built and shipped a
# few lines down — the identical live-tree race the git-archive export above
# was introduced to close, just one step earlier. Reproduced directly: a
# scratch commit with a real, test-catchable bug, archived via `git archive`
# the same way, then live-tree-patched to look correct before running
# `python3 -m unittest discover -s tests` from the repo root — the suite
# reported OK while the archived commit (the one that gets built and shipped)
# still had the bug.
echo "== running python tests =="
# A single test that never returns (a blocking call with no timeout of its
# own, a real deadlock) would otherwise wedge this script forever right here,
# still holding $LOCKFILE -- silently blocking every future deploy attempt
# ("another deploy.sh is already running") until a human notices and kills it
# by hand, with no FAILED message and no other symptom. Reproduced directly:
# a scratch `threading.Event().wait()` test run through this exact command
# never returns on its own (confirmed via an external `timeout`, not this
# script's own protection, since it had none). 300s is generous headroom
# over the real suite's ~65s.
if ! timeout 300 python3 -m unittest discover -s "$BUILD_SRC/tests"; then
  echo "FAILED: python test suite did not finish within 300s (or failed) -- a hung test would otherwise hold this deploy's lock forever, silently blocking every future deploy until killed by hand." >&2
  exit 1
fi

echo "== running node tests =="
# node --test treats a file that merely *executes* successfully as one
# passing test when it defines zero real test(...) cases -- unlike
# python3 -m unittest discover above, which exits 5 ("NO TESTS RAN") the
# moment it collects zero test methods. A commit that ships this file with
# its test(...) bodies accidentally stripped (a bad merge, a block
# commented out mid-edit and never restored) would print "ok"/exit 0
# having verified nothing. Guard by statically counting top-level test(
# call sites in the exact pinned file about to run before trusting node's
# own exit code.
NODE_TEST_FILE="$BUILD_SRC/tests/server.test.js"
if [ "$(grep -c '^test(' "$NODE_TEST_FILE")" -lt 1 ]; then
  echo "FAILED: $NODE_TEST_FILE defines zero top-level test(...) cases -- node's own test runner reports a false 'ok' for an empty or gutted test file, so this can't be trusted as a real test run." >&2
  exit 1
fi
# Same hang risk as the python suite above -- node's own test runner cancels
# an async test that merely never resolves (an idle event loop lets it detect
# that), but a synchronous infinite loop blocks the single process outright,
# with no idle moment for that self-healing to ever run. Reproduced directly:
# a scratch `while (true) {}` test run through this exact command never
# returns on its own. 300s is generous headroom over the real suite's ~20s.
if ! timeout 300 node --test "$NODE_TEST_FILE"; then
  echo "FAILED: node test suite did not finish within 300s (or failed) -- a hung test would otherwise hold this deploy's lock forever, silently blocking every future deploy until killed by hand." >&2
  exit 1
fi

BUILD_DIR="$(mktemp -d)"
# mktemp -d always creates its directory mode 0700 (rwx------), regardless of
# this shell's own umask -- build_site.py never chmods $BUILD_DIR itself (it
# only ever creates the "posts" subdirectory under it and writes files
# directly into it, both of which do pick up this shell's ordinary umask), so
# left alone, $BUILD_DIR's own top-level mode stays 0700 all the way through
# the sync below. The third rsync pass a few dozen lines down
# (`rsync -a --delete-delay --exclude='/posts/' "$BUILD_DIR/" "$LIVE_PUBLIC/"`)
# syncs $BUILD_DIR's *contents* into the already-existing $LIVE_PUBLIC, but -a
# (which includes -p, preserve permissions) also copies the source root's own
# directory mode onto the destination root, even though $LIVE_PUBLIC already
# existed beforehand with a correct, world-readable mode -- so every single
# deploy silently clobbered $LIVE_PUBLIC's own top-level mode down to 0700, no
# matter what it was before. The subsequent `chown -R webapp:webapp` never
# touches mode bits, only ownership, so nothing downstream ever corrected it.
# This went unnoticed because it doesn't break the live site: server.js runs
# as webapp, which chown just made the owner, and an owner can always read
# its own files/traverse its own directories regardless of the mode's
# group/other bits -- but it silently locks out anyone else (a different
# admin account, a future backup/monitoring process) from even listing
# $LIVE_PUBLIC, let alone reading the individually-644/755 files inside it,
# with no error or warning anywhere. Confirmed directly: a scratch $BUILD_DIR
# from a bare `mktemp -d`, built via the real build_site.py and synced with
# deploy.sh's real three rsync passes onto a fresh, correctly-755
# $LIVE_PUBLIC, left it at 0700 afterward -- reproducing the exact drift
# (0700 top-level dir, 0755 posts/, 0644 files) found live on
# /srv/dailyamnesia/public on this very host. chmod-ing $BUILD_DIR to 0755
# right here, before anything is written into it, gives the top-level rsync
# pass a correctly-permissioned source to copy from instead, and -- since
# rsync -a syncs the destination root's mode to match the source root's on
# every run -- also self-heals $LIVE_PUBLIC back to 0755 on the very next
# deploy, with no separate one-off fix needed on the live host. Re-running
# the identical repro with this chmod in place left $LIVE_PUBLIC at 0755
# throughout, whether the destination started already-correct or already
# drifted to 0700.
chmod 755 "$BUILD_DIR"

echo "== building site =="
python3 "$BUILD_SRC/tools/build_site.py" "$BUILD_DIR"

# No post has ever been removed in this project's history, so a build with
# fewer post pages than what's already live is a strong signal of a broken
# build (e.g. posts/ glob resolving empty), not a deliberate deletion — and
# the rsync --delete below would otherwise happily wipe the live posts to
# match. Refuse rather than sync in that case.
#
# The counts are each guarded by an explicit `if !` rather than a bare
# assignment: under `set -o pipefail`, if `find` itself errors partway
# (e.g. a permission-denied entry) while `wc -l` still succeeds on what it
# received, the pipeline's exit status is find's non-zero one — which
# would otherwise trip `set -e` and kill the script right here, silently,
# with none of this script's own `FAILED:` messages ever printed.
if ! NEW_POST_COUNT="$(find "$BUILD_DIR/posts" -name '*.html' | wc -l)"; then
  echo "FAILED: could not count post pages in the new build ($BUILD_DIR/posts)." >&2
  exit 1
fi

# `sudo test -d "$LIVE_PUBLIC/posts"` returning false means one of two very
# different things: the directory genuinely doesn't exist yet (a real first
# deploy, where OLD_POST_COUNT=0 below is correct and the guard above this
# comment shouldn't apply), or `sudo` itself failed to even run `test` — e.g.
# a cached credential timestamp expired and this script is running with no
# controlling TTY and no askpass helper (a cron/systemd-timer invocation, or
# this script's own `flock`-wrapped self-re-exec with redirected stdio). Both
# cases look identical to a bare `if sudo test -d ...`: both make it return
# false, and OLD_POST_COUNT silently defaults to 0 either way — which defeats
# the whole point of the guard below when it's the second case, since a truly
# broken build (glob failure, an upstream crash this script didn't catch)
# would then compare against a wrongly-0 "old" count instead of the real live
# one, pass the guard, and let `rsync --delete-delay` wipe every live post.
# This is also the very first `sudo` call anywhere in this script (confirmed
# by grepping every other one below), so a broken-sudo condition would first
# surface right here. Reproduced directly: a scratch copy of this guard,
# pointed at a real posts/ dir with 3 live posts and a broken build with 0,
# correctly refused under a working sudo; under a fake `sudo` that always
# fails immediately (modeling the no-TTY/expired-credential case), it
# silently computed OLD_POST_COUNT=0 and would have proceeded straight into
# the rsync below instead of refusing.
#
# The fix has to check sudo's own health directly (`sudo -n true`), not
# infer it from some other directory's existence: an earlier version of this
# fix instead required `sudo test -d "$LIVE_PUBLIC"` (the parent of
# .../posts) to succeed first, reasoning that the parent always exists once
# the site has been deployed once — but that reintroduces the identical
# ambiguity one level up, since `sudo test -d` on a directory that genuinely
# doesn't exist yet (a real first deploy) is indistinguishable from `sudo`
# itself failing, and would wrongly abort a genuine first deploy under
# perfectly healthy sudo. `sudo -n true` sidesteps this entirely: `-n`
# (non-interactive) makes it fail immediately rather than prompt when
# credentials are missing/expired, and its result depends only on whether
# sudo can run *something* right now, never on any path's existence.
# Reproduced directly, three ways: (1) genuine first deploy (target
# directory doesn't exist, real working sudo) passes through to
# OLD_POST_COUNT=0 as intended; (2) real sudo with a broken build against
# real live posts still correctly refuses; (3) broken sudo (fake `sudo -n`
# failing) against real live posts now correctly refuses instead of
# silently defaulting to 0 the way the parent-directory check still would
# have let through.
if ! sudo -n true 2>/dev/null; then
  echo "FAILED: sudo is not usable non-interactively right now (expired/missing credentials, no controlling TTY, or similar) — refusing to guess whether posts are currently live rather than risk silently treating a real deploy as a first deploy." >&2
  exit 1
fi
OLD_POST_COUNT=0
if sudo test -d "$LIVE_PUBLIC/posts"; then
  if ! OLD_POST_COUNT="$(sudo find "$LIVE_PUBLIC/posts" -name '*.html' | wc -l)"; then
    echo "FAILED: could not count post pages currently live in $LIVE_PUBLIC/posts." >&2
    exit 1
  fi
fi
if [ "$NEW_POST_COUNT" -lt "$OLD_POST_COUNT" ]; then
  echo "FAILED: new build has $NEW_POST_COUNT post page(s), fewer than the $OLD_POST_COUNT currently live — refusing to sync, since this would delete live posts. If a post's removal is genuinely intended, deploy by hand." >&2
  exit 1
fi

# The lock acquired via `flock --close` above is held by the flock
# supervisor process (this script's own parent in the process tree), not by
# this process itself — that fork is unavoidable once --close is in play,
# since closing this process's own copy of the fd right before it started
# running means *something else* has to be left holding it. That supervisor
# can die independently of this actual deploy still running — killed
# directly (e.g. an operator debugging a "stuck" deploy targets the most
# descriptive-looking `ps` line, which is the supervisor's) or by an
# OOM-killer preferring an idle process blocked in wait() over an actively-
# running rsync/test child, the same threat model the cleanup() comment
# above already treats as real. If that happens, this lock is released the
# instant the supervisor dies, and a second invocation can already be
# running concurrently against the same $LIVE_PUBLIC by the time this
# process reaches the rsync below — exactly the two-racing-rsyncs scenario
# the whole lock exists to prevent. Bash's own $PPID is cached at shell
# startup and won't reflect the supervisor's death (it keeps reporting the
# supervisor's original, now-dead PID), so the live value has to be looked
# up fresh via `ps`, not read from $PPID. Reproduced directly with a scratch
# copy of this exact lock shape: killing only the supervisor mid-run let the
# simulated sync step run to completion, unsupervised, every time; this
# check, added right before the one genuinely irreversible step, catches it
# and aborts instead.
if [ "$(ps -o ppid= -p $$ | tr -d ' ')" = "1" ]; then
  echo "FAILED: this deploy's lock-holding process is gone (reparented to init) — a concurrent deploy may already be running; refusing to sync." >&2
  exit 1
fi

# The reparented-to-init check just above only catches the supervisor process
# itself dying. It says nothing about whether the lock it's still holding
# actually means anything, because flock(2) locks an inode, not a path:
# `rm -f "$LOCKFILE"` followed by anything at all recreating that same path —
# most plausibly an operator who sees "another deploy.sh is already running",
# assumes it's a stale lock left over from a crash, and clears it "by hand"
# (the identical persona this script already assumes for the reparented-
# supervisor case above and the locked-$BUILD_SRC-worktree case in cleanup())
# — leaves the still-very-much-alive supervisor holding a flock on an
# unlinked, orphaned inode that nothing can ever contend against again, while
# a brand new `flock -n --close -E 99 "$LOCKFILE" ...` opens a *different*
# inode at that same path and succeeds immediately. A second deploy.sh then
# runs fully concurrently with this one — the exact two-racing-rsyncs
# scenario fix #1 (top of this file) exists to prevent, just reached by
# swapping out the lock file itself instead of by fd inheritance. Reproduced
# directly: held a real `flock --close` lock on a scratch file, then `rm -f`
# + `touch`'d that same path while the holder was still alive and sleeping
# mid-lock — a second, independent `flock -n` on that path acquired
# instantly, confirmed racing against the first. `readlink` on the original
# holder's own fd for that file (found via /proc/<holder-pid>/fd/) reported
# the target as "$LOCKFILE (deleted)" the exact moment the path was replaced
# — the standard Linux signal that an fd's inode was unlinked out from under
# it, even though an unrelated new file now sits at the same name. Since
# parent_is_flock already confirmed $PPID is genuinely the flock supervisor,
# and it still holds the lock fd open, scanning its /proc/$PPID/fd for the
# one pointing at $LOCKFILE and checking for that "(deleted)" suffix catches
# exactly this. Re-running the identical reproduction with this check in
# place correctly refused the second invocation instead of letting it
# proceed.
lock_file_was_replaced() {
  local target fd
  for fd in "/proc/$PPID/fd/"*; do
    target="$(readlink "$fd" 2>/dev/null || true)"
    if [ "$target" = "$LOCKFILE (deleted)" ]; then
      return 0
    fi
  done
  return 1
}
if lock_file_was_replaced; then
  echo "FAILED: this deploy's lock file ($LOCKFILE) was deleted and replaced while this deploy was running — it no longer protects against a concurrent deploy; refusing to sync." >&2
  exit 1
fi

echo "== syncing content to $LIVE_PUBLIC =="
# The post-count guard above deliberately treats "$LIVE_PUBLIC/posts doesn't
# exist" as a legitimate, expected state on a genuine first-ever deploy
# (OLD_POST_COUNT=0, guard passes through) -- but that guard only decides
# whether to proceed, it never actually prepares $LIVE_PUBLIC for the rsync
# passes below to write into, and rsync itself only ever creates one missing
# leaf directory component, not a whole missing chain. On a truly fresh host
# where $LIVE_ROOT (/srv/dailyamnesia) already exists from initial
# provisioning (server.js and the systemd unit placed there by hand) but
# nothing has ever created $LIVE_PUBLIC itself (no deploy has run yet), both
# "$LIVE_PUBLIC" and "$LIVE_PUBLIC/posts" are missing at once, and the very
# first rsync pass below fails outright with "mkdir ... failed: No such file
# or directory", killed by `set -e` with a bare rsync error instead of this
# script's own "FAILED:" messaging or $RECOVERY_HINT -- the exact "real first
# deploy" case the guard above claims to support never actually reaches a
# working sync. Reproduced directly: a scratch $LIVE_ROOT with no public/
# subdir, run through deploy.sh's real three rsync lines unmodified (sudo
# stripped, since the scratch dirs need no privilege), failed with rsync
# exit code 11 on the very first pass, matching this exact scenario; adding
# `mkdir -p "$LIVE_PUBLIC/posts"` first (harmless and a no-op on every
# non-first deploy, since the directories already exist by then) let the
# identical repro complete all three passes successfully.
sudo mkdir -p "$LIVE_PUBLIC/posts"

# --delete-delay, not plain --delete: plain --delete defaults to
# delete-during, which removes each now-extraneous destination file as
# rsync gets to it, interleaved with (and not necessarily after) copying
# in its replacement. If this rsync is interrupted partway (operator
# Ctrl-C, OOM-kill, a full disk) during a post rename or content update,
# a file can be deleted before its replacement finishes copying in,
# leaving that page live-404 on the site until the next successful
# deploy. Verified with a scratch rsync interrupted mid-transfer while
# renaming a file: plain --delete lost the file in every trial across a
# range of interrupt timings; --delete-delay queues all deletions and
# only applies them after every file has finished copying, so the same
# interruption left the old (still-valid) file in place every time.
# Two ordered passes, not one: rsync transfers files in the order it walks
# the source tree (top-level entries alphabetically, before recursing into
# subdirectories), so a single rsync across the whole tree can write
# index.html/feed.xml — already updated to link a brand-new post — before
# that post's own page has been copied into posts/, leaving a live 404
# behind a link the homepage/feed itself just started advertising, for
# however long the transfer takes. --delete-delay above only defers
# *deletions* to the end of the transfer; it's a different race and doesn't
# help here. Reproduced directly: a scratch rsync of a large new post file
# throttled with --bwlimit showed the new index.html (linking the new post)
# already live while the post file itself didn't exist yet, for the whole
# multi-second transfer. Posts go out first and finish completely; only
# then does the second pass publish the top-level pages that link to them.
#
# But the posts pass itself must not delete anything yet, and a third pass
# is needed after the top-level one: build_site.py derives each post's
# output filename from its source markdown's own filename, so renaming a
# post's source file makes posts/<old-slug>.html extraneous in the same
# build that adds posts/<new-slug>.html, and index.html/feed.xml are
# exactly what stops linking to the old slug and starts linking to the new
# one. If the posts pass were still --delete-delay as originally written,
# it would fully complete — new slug published *and* old slug deleted —
# before the top-level pass ever ran; a process death in between (the same
# TERM/OOM/disk-full causes already documented above and in cleanup(), just
# landing between these calls instead of mid-transfer inside one of them)
# leaves index.html/feed.xml still advertising the old slug while the file
# they link to is already gone — a live dead link, and a strictly worse
# outcome than doing nothing, from a fix whose whole point is preventing
# exactly that. Reproduced directly: a scratch old-live/new-build pair with
# a post renamed between them, run through the original two-pass sequence
# with only the first pass executed (simulating the process dying right
# after it): index.html still pointed at the old slug, and that file was
# already gone from posts/ — a 404 behind a live link. Splitting into three
# passes closes it: (1) posts, without --delete, so the new slug's page
# (and any updated prev/next links on its neighbors) exist before anything
# can advertise them, while the old slug's page is left alone; (2)
# top-level, --delete-delay as before, which is what actually flips which
# slug is linked — safe now, since pass 1 already guarantees both the old
# and new pages exist through this pass; (3) posts again, now with
# --delete-delay, to remove the old slug's page, safe because pass 2
# already guarantees nothing live links to it anymore. A death between any
# two of these three passes leaves either an extra live post nothing points
# at (harmless, swept up by the next deploy) or the original, unchanged
# state — never a link to something missing. Re-running the identical
# repro with all three passes, stopping after each one in turn, found no
# point where a live link resolved to a missing file, unlike the two-pass
# version.
#
# Pass 1 itself isn't internally atomic across the many files one `rsync`
# invocation can touch, though: rsync transfers files in sorted order (the
# same fact the top-level/posts split above relies on), and build_site.py
# regenerates every post's prev/next nav on every build, not just the ones
# that changed. Adding a new latest post rewrites its immediate,
# alphabetically-earlier neighbor's page to link forward to the new slug —
# and since that neighbor (earlier date, earlier filename) sorts before
# the new post (later date, later filename), the neighbor's page, already
# advertising a "next" link to the brand-new slug, can go live before the
# new slug's own file has finished transferring, for however long that
# transfer takes. A process death in that exact window (the same
# TERM/OOM/disk-full causes already covered above) leaves a live link to a
# live 404 indefinitely — entirely within pass 1, never reaching the
# pass-2/pass-3 ordering the rest of this comment block protects.
# Reproduced directly: a scratch posts/ dir with an old post B and a large
# new post C, where B's rebuilt page links to C — running a single
# `rsync -a` of the whole tree throttled with --bwlimit showed B's updated
# content (linking to C) already live while C's own file didn't exist yet,
# for the whole multi-second transfer. Splitting pass 1 into two closes
# it: first `--ignore-existing` (copies only files not yet present live —
# genuinely new posts, which nothing can be linking to yet, since nothing
# from this deploy has gone live before this sub-pass runs), then the
# unrestricted sync (which updates existing pages, including neighbor nav
# links, now safe since anything they might newly reference already went
# live in the sub-pass before it). Re-running the identical repro through
# both sub-passes in order found no point where a live page linked to a
# not-yet-existing one.
sudo rsync -a --ignore-existing "$BUILD_DIR/posts/" "$LIVE_PUBLIC/posts/"
sudo rsync -a "$BUILD_DIR/posts/" "$LIVE_PUBLIC/posts/"
sudo rsync -a --delete-delay --exclude='/posts/' "$BUILD_DIR/" "$LIVE_PUBLIC/"
sudo rsync -a --delete-delay "$BUILD_DIR/posts/" "$LIVE_PUBLIC/posts/"
sudo chown -R webapp:webapp "$LIVE_PUBLIC"

# This hint is shown for two structurally different failures: the restart
# command itself failing (e.g. systemd's default start-limit-hit after
# repeated restarts within 10s -- the unit ends up "failed"), and the HTTP
# verification loop below failing after the "unchanged, no restart needed"
# branch -- i.e. systemd still reports the unit "active" but it isn't
# actually answering (a hung event loop, a closed listener, an internal
# crash that didn't kill the process). `systemctl start` is a no-op on a
# unit systemd already considers active, so it does nothing for the second
# case -- reset-failed and start both "succeed" and the service is still not
# responding. Reproduced directly with a scratch model of systemd's own
# state semantics: `start` only changes anything when the unit isn't already
# active, `restart` unconditionally stops-then-starts either way. Using
# `restart` here recovers both cases; re-running the same reproduction
# confirmed it against both the failed-unit case and the active-but-hung one.
RECOVERY_HINT="if the service failed to (re)start (e.g. systemd's default
start-limit-hit after repeated restarts within 10s), or is reported active
but isn't actually responding (a hung process, a closed listener), recover
with:
  sudo systemctl reset-failed dailyamnesia-web.service && sudo systemctl restart dailyamnesia-web.service"

# The restart decision can't be gated on the server.js diff alone: if the
# service is down for a reason unrelated to a code change (a prior restart
# that itself failed, an OOM-kill, a manual stop) and this deploy has no
# server.js changes to push, diff-only gating takes the "unchanged, no
# restart needed" branch and leaves the service down — and since the diff
# keeps reporting "unchanged" on every later run too, re-running deploy.sh
# can never bring it back on its own; only a manual `systemctl start` can.
#
# Diffed and copied from $BUILD_SRC, not the live checkout's own
# tools/server.js: everything else in this script (the test suites, the
# site build) deliberately reads from the pinned $BUILD_SRC worktree of
# $LOCAL_REV specifically so a live-tree edit made during the ~55+ seconds
# the test suites and build take can't reach production — this step used a
# bare relative path resolved against $REPO_ROOT instead, silently
# bypassing that entire protection for the one file whose correctness
# actually restarts a live service. Reproduced directly: a scratch repo
# with a committed, worktree-checked-out "VERSION_A" and a live-tree-only,
# uncommitted "VERSION_B" — the old `tools/server.js` (relative, cwd
# $REPO_ROOT) resolved to VERSION_B, the untested edit, not VERSION_A, the
# one both test suites just verified.
SERVER_CHANGED=false
if ! sudo diff -q "$BUILD_SRC/tools/server.js" "$LIVE_SERVER" >/dev/null 2>&1; then
  echo "== server.js changed, deploying =="
  # Both `rsync` calls above overwrite live content atomically (temp file
  # in the destination dir, renamed into place on success) -- this was the
  # one write to live content that didn't, a plain `cp` straight onto
  # $LIVE_SERVER. `cp` opens the destination, truncates it, and streams
  # bytes in; if it's interrupted partway (disk full, a `sudo` hiccup, or
  # this script itself receiving TERM -- `cleanup()`'s `pkill -TERM -P $$`
  # delivers TERM straight to this `cp` child), the live file is left
  # truncated: neither the old version nor the new one, just garbage that
  # `node` can't even parse. Reproduced directly: a scratch copy sent
  # SIGTERM ~50ms into copying a 200MB file left the destination at a
  # partial byte count matching neither the original nor the source.
  # Copying to a same-directory temp file and `mv`-ing it into place
  # mirrors what rsync already does and closes the same gap here.
  sudo cp "$BUILD_SRC/tools/server.js" "$LIVE_SERVER.new"
  sudo chown webapp:webapp "$LIVE_SERVER.new"
  sudo mv "$LIVE_SERVER.new" "$LIVE_SERVER"
  SERVER_CHANGED=true
fi

if [ "$SERVER_CHANGED" = true ] || ! sudo systemctl is-active --quiet dailyamnesia-web.service; then
  echo "== (re)starting service =="
  if ! sudo systemctl restart dailyamnesia-web.service; then
    echo "FAILED: systemctl restart didn't succeed." >&2
    echo "$RECOVERY_HINT" >&2
    exit 1
  fi
else
  echo "== server.js unchanged and service already running, no restart needed =="
fi

echo "== verifying =="
# Poll rather than a fixed sleep-then-check-once: a real restart was measured
# taking 600-750ms just to bind under normal load (see the post this fix
# shipped with), leaving a single 1-second sleep almost no margin before the
# one-shot curl below it would have reported a false FAILED on a slower bind.
for path in / /feed.xml; do
  code=000
  for _ in $(seq 1 40); do
    # curl's own -w already writes "000" on a failed/timed-out request, so the
    # fallback here only needs to stop `set -e` from killing the script on
    # curl's non-zero exit — `|| echo 000` used to also append a second,
    # literal "000" after curl's own, producing a misleading "000000" in the
    # FAILED message below on any real connection failure; `|| true` doesn't.
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 1 "http://127.0.0.1:3000$path" || true)"
    [ "$code" = "200" ] && break
    sleep 0.25
  done
  if [ "$code" != "200" ]; then
    echo "FAILED: http://127.0.0.1:3000$path never returned 200 (last: $code)" >&2
    systemctl status dailyamnesia-web.service --no-pager -l >&2 || true
    echo "$RECOVERY_HINT" >&2
    exit 1
  fi
  echo "  $path -> $code"
done

# Looked up via systemd's own MainPID rather than `ps | grep server.js`:
# a plain substring grep also matches any unrelated process whose command
# line happens to contain "server.js" (a scratch test server left running
# from earlier testing, or even a shell command that mentions the filename
# as a string), which turns this into a false "FAILED" after a deploy that
# actually succeeded.
#
# Every exit below this point is reached only *after* the HTTP poll above
# already got a real 200 from both / and /feed.xml — the new content is
# live and being served correctly, full stop. That's a fundamentally
# different situation from every other "FAILED" exit earlier in this
# script (uncommitted tree, failing tests, refused post-count guard, a
# restart that didn't come back), all of which mean nothing shipped or the
# site is actually down. A plain `exit 1` here can't be told apart from
# those by anything watching this script's exit code (or grepping its
# stderr for "FAILED") -- a caller that reacts to "FAILED" by e.g.
# re-deploying a previous commit, or paging someone as "site is down",
# would be reacting to a deploy that in fact fully succeeded and is
# already verified live, just failed this one extra process-identity
# sanity check. Reproduced directly: a scratch copy of this exact tail
# section, pointed at a real HTTP server actually answering 200 on both
# paths and a faked `ps` reporting the wrong owner, still printed a bare
# "FAILED: ... not webapp" and exited 1 -- identical in both shape and
# exit code to a completely unrelated, nothing-happened early abort (a
# dirty working tree) from the very top of this same script. Giving this
# case its own exit code and saying outright that the content is already
# live lets a caller (or a human) tell "the deploy didn't happen" apart
# from "the deploy happened and is live; only this last sanity check
# failed" without having to guess from prose alone.
POST_VERIFY_SANITY_FAILED=2
pid="$(systemctl show -p MainPID --value dailyamnesia-web.service)"
if [ -z "$pid" ] || [ "$pid" = "0" ]; then
  echo "FAILED: deploy succeeded and the new content is verified live (both / and /feed.xml returned 200) — but could not determine dailyamnesia-web.service's running PID afterward, so its ownership couldn't be checked. Investigate directly; no further action is needed to ship this deploy." >&2
  exit "$POST_VERIFY_SANITY_FAILED"
fi
# Guarded the same way the post-count checks above are: `ps -o user= -p
# "$pid"` can itself fail (e.g. the process has already exited by the time
# this runs — a real race right after a restart) while `tr` still succeeds
# trivially on its empty input. Under `set -o pipefail` that leaves the
# pipeline's exit status as ps's non-zero one, which a bare assignment would
# let `set -e` act on silently, killing the script right here with none of
# this script's own `FAILED:` messages ever printed — the same failure shape
# the post-count checks were fixed for in session 95.
if ! owner="$(ps -o user= -p "$pid" | tr -d ' ')"; then
  echo "FAILED: deploy succeeded and the new content is verified live (both / and /feed.xml returned 200) — but could not determine the owning user of server.js process (pid $pid) afterward; it may have already exited. Investigate directly; no further action is needed to ship this deploy." >&2
  exit "$POST_VERIFY_SANITY_FAILED"
fi
if [ "$owner" != "webapp" ]; then
  echo "FAILED: deploy succeeded and the new content is verified live (both / and /feed.xml returned 200) — but server.js process (pid $pid) is owned by '$owner', not webapp. Investigate directly; no further action is needed to ship this deploy." >&2
  exit "$POST_VERIFY_SANITY_FAILED"
fi

echo "== done: deployed, verified, process owned by webapp =="
