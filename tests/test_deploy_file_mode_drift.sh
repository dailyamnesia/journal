#!/usr/bin/env bash
# Regression test for a file-mode drift bug in deploy.sh's build-to-live
# sync: build_site.py writes index.html/feed.xml/404.html/charter.html/
# favicon.svg/posts/*.html via Python's plain write_text()/write_bytes(),
# which land at whatever mode this shell's own umask leaves (0666 minus
# umask), not a fixed mode of its own. Two earlier fixes chmod $BUILD_DIR
# and $BUILD_DIR/posts themselves back to 755 for exactly this reason, but
# neither one reaches the individual *files* build_site.py writes inside
# them -- and every `rsync -a` pass below carries each file's own mode
# along with its content (-a implies -p), so a deploy run under a
# stricter umask (a hardened shell profile, a systemd unit's own UMask=,
# an operator's leftover `umask 077`) silently drops every synced page
# down to 0600 on the live site: the same "doesn't break the site itself,
# since its owner can always read its own files, but silently locks out
# anyone else" failure already closed for the two directories, just one
# level lower, at the actual served content itself.
#
# This extracts the two directory chmods, the file-mode normalization
# fix, and the four real rsync passes verbatim out of the current,
# unmodified tools/deploy.sh (grepped out by their exact literal command
# text, not hand-copied) and runs them for real against scratch
# directories -- the same commands the real deploy actually runs, so this
# fails if the fix is ever reverted or the command shape changes without
# updating this test.
set -euo pipefail

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

CHMOD_BUILD_DIR="$(get_line 'chmod 755 "$BUILD_DIR"')"
CHMOD_POSTS="$(get_line 'chmod 755 "$BUILD_DIR/posts"')"
FIND_CHMOD_FILES="$(get_line 'find "$BUILD_DIR" -type f -exec chmod 644 {} +')"
RSYNC1="$(get_line 'sudo rsync -a --ignore-existing "$BUILD_DIR/posts/" "$LIVE_PUBLIC/posts/"')"
RSYNC2="$(get_line 'sudo rsync -a "$BUILD_DIR/posts/" "$LIVE_PUBLIC/posts/"')"
RSYNC3="$(get_line "sudo rsync -a --delete-delay --exclude='/posts/' \"\$BUILD_DIR/\" \"\$LIVE_PUBLIC/\"")"
RSYNC4="$(get_line 'sudo rsync -a --delete-delay "$BUILD_DIR/posts/" "$LIVE_PUBLIC/posts/"')"

WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

BUILD_DIR="$WORK/build"
LIVE_PUBLIC="$WORK/live"
mkdir -p "$LIVE_PUBLIC/posts"
chmod 755 "$LIVE_PUBLIC" "$LIVE_PUBLIC/posts"

# Stand-in sudo: no real root needed for scratch dirs this test owns.
mkdir -p "$WORK/bin"
cat > "$WORK/bin/sudo" <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF
chmod +x "$WORK/bin/sudo"
export PATH="$WORK/bin:$PATH"

# Simulate build_site.py writing content under a stricter umask than this
# deploy's own ordinary invocation environment -- the exact condition the
# bug needs to fire.
(
  umask 077
  mkdir -p "$BUILD_DIR/posts"
  echo "<html>index</html>" > "$BUILD_DIR/index.html"
  echo "<xml>feed</xml>" > "$BUILD_DIR/feed.xml"
  echo "<html>404</html>" > "$BUILD_DIR/404.html"
  echo "<html>post</html>" > "$BUILD_DIR/posts/hello-world.html"
)

eval "$CHMOD_BUILD_DIR"
eval "$CHMOD_POSTS"
eval "$FIND_CHMOD_FILES"
eval "$RSYNC1"
eval "$RSYNC2"
eval "$RSYNC3"
eval "$RSYNC4"

fail=0
for f in "$LIVE_PUBLIC/index.html" "$LIVE_PUBLIC/feed.xml" "$LIVE_PUBLIC/404.html" "$LIVE_PUBLIC/posts/hello-world.html"; do
  mode="$(stat -c '%a' "$f")"
  if [ "$mode" != "644" ]; then
    echo "FAIL: $f ended up mode $mode after sync (expected 644) -- a deploy run under a stricter umask than this test's own invocation would silently ship this file at $mode, locking out anyone but its owner." >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  exit 1
fi

echo "PASS: every synced content file ended up at the correct 644 regardless of the umask build_site.py ran under."
