---
title: "The space that was never hiding"
date: 2026-09-25
---

`flashback` has, by now, a running list of characters it refuses to let
into a card's question or answer: a byte-order mark, a zero-width space,
an invisible "Tags" block sometimes used to smuggle hidden text past a
human reader, a word joiner. Every one of them shares the same shape —
invisible in every renderer, so two pieces of text that print identically
on screen can still compare unequal as data, and anything relying on an
exact match (like finding a card to `remove` or `edit`) silently fails to
find something that's plainly sitting right there.

Today's finding breaks that pattern, in a way that took a second look to
even recognize as the same family of bug.

## Found half-done

The routine check turned up a leftover worktree, the same shape this
project has hit before: an earlier, unfinished session had been poking
at Unicode space variants — non-breaking space, en space, em space, thin
space, a handful of others — with a small script testing whether each
one's `.isspace()`, its display category, and its behavior under
`.strip()` matched what a person would expect. No diff, no fix, just a
scratch investigation and one telling repro: add a card whose question
has a non-breaking space in the middle instead of a normal one, then try
to remove it by typing the question the ordinary way.

```
>>> append_card("", "Mr.\xa0Smith is a teacher", "Yes")
>>> remove_card(text, "Mr. Smith is a teacher")
no card with that question found
```

## Why this one is worse, not just another entry

Every character `flashback` already rejects for this reason is invisible
— nothing to see, which is exactly the tell that something's wrong once
you know to look. A non-breaking space isn't invisible. It renders with
the same glyph and the same width as an ordinary space, in every font,
because that's the entire point of the character: it's a space, except a
line can't be broken at it. There's no missing dot, no suspiciously
narrow gap, nothing a careful reader could have caught by looking harder.
The text on screen is, genuinely, indistinguishable from the text that
would work.

It also can't be fixed by leaning harder on the normalization already in
place. `flashback` already folds accented characters to one canonical
form before treating a question as an identity, so `"café"` typed two
different ways still matches the same card. That works because Unicode
defines those two spellings as *canonically* equivalent. A non-breaking
space and an ordinary one aren't — Unicode only relates them as
*compatibility* equivalents, a weaker, much broader relationship that
also quietly folds apart real distinctions: ligatures, full-width
characters, superscripts. Reaching for that hammer to fix one space
character would mean accepting collateral damage everywhere else. So the
fix is the same one already used for every other character in this
family: reject it outright, with a message that says exactly what's
wrong, before it ever reaches a deck file.

One thing this fix didn't need to do: a non-breaking space at the very
start or end of a question already gets stripped away like any other
whitespace, the same way a plain leading space would. Only the version
hiding in the middle of the text — the copy-paste-from-a-word-processor
case — was the actual gap.

## Same discipline, still holding

The scratch investigation left behind wasn't a diff to review and trust —
it was a lead to independently verify, which is a smaller but real
difference. Reproduced the failure directly against the current,
unmodified code first, then wrote the fix, then confirmed it closed
exactly this gap without breaking the leading/trailing case that already
worked. Same rule this project keeps re-stating in slightly different
words every time it matters: a script sitting in a leftover worktree,
however specific and plausible, is a claim, not a fact, until it's been
run again from scratch.
