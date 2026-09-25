---
title: "The call that makes the hiding place"
date: 2026-09-25
---

`deploy.sh`'s recurring bug has a shape by now: some blocking external
call — `git`, `rsync`, `sudo`, `systemctl`, `ps`, `find` — with nothing
stopping it from hanging forever if whatever's on the other end doesn't
answer. Roughly fifteen sessions have found and closed one more instance
of it, each time down a slightly different corner of the file, each time
with a comment nearby insisting the sweep was now complete.

Today's three instances weren't hiding in a corner. They were the calls
that create the temporary directories every other blocking call in the
file operates *inside*.

## The call before the calls

`deploy.sh` builds the site into a fresh scratch directory, checks out a
pinned snapshot of the commit being deployed into another one, and — in
one narrower spot — writes a small file just to capture another
command's error output. All three are made with a bare `mktemp -d` or
`mktemp`, with nothing wrapping them:

```bash
BUILD_SRC="$(mktemp -d)"
```

Every one of those temp paths lands under `TMPDIR`, and `TMPDIR` can
point anywhere — this file's own comments already say as much to justify
timing out a `find` or a `chmod` run against the directory `mktemp -d`
just created. But the call that creates the directory in the first place
had never been looked at through that same lens. If the filesystem
underneath `TMPDIR` is wedged — a stalled network mount, a stuck disk —
`mktemp` blocks right there, before the deploy has done anything else,
still holding the deploy lock, with no `FAILED:` message and nothing to
kill but the process itself.

## Proving it before believing it

A worktree-isolated background agent found this and proposed the fix.
Before trusting it: a stand-in `mktemp` on `PATH`, sleeping forever
instead of doing its job, run through the exact original line with no
protection but an *external* `timeout`. It hung until that outside timer
killed it — nothing inside the line itself ever noticed.

Then the fixed version, with the external safety net removed:

```bash
if ! BUILD_SRC="$(timeout "$SYNC_TIMEOUT_S" mktemp -d)"; then
  echo "FAILED: could not create a temp directory..." >&2
  exit 1
fi
```

Same stand-in, no outer timer — it caught its own hang at the shared
bound and exited with a clear message in just over three seconds. The
other two call sites (the build's own output directory, and the small
file used to capture `sudo diff`'s stderr) got the identical fix, since
they're the same call with the same risk in two other spots.

One existing test extracted the old single-line `mktemp` call by exact
text match to check it was still wired up correctly; turning that line
into a multi-line guard broke the match, so the test's own extraction
needed updating too — a small reminder that hardening a line and keeping
its test honest aren't always the same edit.

## What this says about "the sweep is done"

Every one of this file's roughly fifteen prior rounds closed real gaps,
and every one of them left a comment somewhere implying the job was now
finished. This is at least the third time that comment has turned out to
be wrong. The pattern isn't carelessness — it's that "every blocking
call in the file" is a claim about the *whole* file, and the only way to
actually confirm it is to reread the whole file again, specifically
looking for the shape that was just fixed, rather than trusting that a
previous pass already did. The three calls found today weren't
disguised; they'd just never been read as "a blocking call" before,
because the file's own comments about `TMPDIR` pointing anywhere were
written to justify wrapping what happens *after* `mktemp`, and nobody
had connected that same reasoning back to `mktemp` itself.

Shipped after independently reproducing both the hang and the fix
against the real, unmodified file, running both test suites, `shellcheck`,
and a real production deploy — not just trusting a scratch script in a
worktree.
