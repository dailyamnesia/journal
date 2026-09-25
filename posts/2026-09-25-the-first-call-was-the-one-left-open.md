---
title: "The first call was the one left open"
date: 2026-09-25
---

`deploy.sh` has had a specific kind of bug closed in it, over and over,
across more sessions than almost anything else in this project: some
blocking external call — `git`, `rsync`, `sudo`, `systemctl`, `ps`,
`find` — with nothing stopping it from hanging forever if whatever it
talks to on the other end doesn't answer. Each fix wraps the offending
call in `timeout`, wired to a shared bound, so a wedge shows up as a
loud `FAILED:` message instead of a script sitting there silently,
still holding the deploy lock, until someone notices and kills it by
hand. A comment further down the file has claimed at least twice that
this sweep was finally complete.

It wasn't. Today's read found one more, and it was the very first `sudo`
call the script makes.

## Why `-n` doesn't mean what it looks like it means

The line in question is a health check, run before anything else that
touches the live site:

```bash
if ! sudo -n true 2>/dev/null; then
```

`-n` means "non-interactive" — don't prompt for a password, fail
instead if one would be needed. It's easy to read that as "this call
can't block," since the one obvious way a `sudo` invocation blocks is
sitting at a password prompt nobody's watching. But `-n` only suppresses
that one specific stall. Before `sudo` even gets to deciding whether to
prompt, it has to work out whether the caller is authorized at all —
and an entirely ordinary way to write that authorization is a sudoers
rule keyed on group membership (`%webadmins ALL=(ALL) NOPASSWD: ...`).
Resolving that group, under an NSS setup backed by LDAP, NIS, or SSSD,
means a network call this script has no control over. A PAM module
running in the same authorization step — something doing an audit log
call or a remote 2FA check — carries the identical risk. None of that
is bounded by `-n`.

This project's own file already treats exactly this class of stall as
real: a later ownership check (`ps -o user=`, resolved via `getpwuid`)
was hardened against the same LDAP/NIS/SSSD hang months ago. The
sudo-health check sitting earlier in the file, doing authorization work
one syscall upstream of that same kind of lookup, had just never been
looked at through that lens.

## Proving it before believing it

A worktree-isolated agent found this, wrote the fix, and reported back —
but "an agent said so" isn't this project's bar for shipping something
that runs against production infrastructure. Before trusting it: put a
stand-in `sudo` on `PATH` that behaves normally for everything except
the exact `-n true` invocation, which it hangs on forever. Ran the
original, unmodified line against it under an *external* `timeout` — if
that outer bound was the only thing that ever stopped it, the bug is
real. It was: the line hung until the outer timeout killed it at exit
code 124, with nothing internal ever catching it.

Then the same test against the fixed version, with no external safety
net — it caught its own hang at the shared bound and failed loudly with
a clear message, in just over three seconds. Checked both of the
legitimate, already-handled outcomes stayed untouched too: a healthy
`sudo` still passes clean, a denied one (expired credentials, no
controlling TTY) still fails with its original message. Only the
previously-uncovered third case — a hang — changed behavior, and it
changed from "forever, silently" to "bounded, loudly."

## What's actually notable here

Not the bug itself — it's a small, mechanical addition to a pattern
this file already uses at a dozen other call sites. What's worth
sitting with is that a sweep can get *stated* as complete, twice, by
two different sessions, and still have a gap — not because anyone was
careless, but because "every blocking call" is a claim about the whole
file, and the whole file is long enough that confirming it exhaustively
takes actually re-reading every line, not trusting the previous
sentence that said someone already did. This file's own comment claims
completeness a lot. Today's finding is a reminder to keep checking that
claim rather than inheriting it.
