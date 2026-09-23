#!/usr/bin/env python3
"""Render posts/*.md into a static HTML site.

Stdlib only, no dependencies. Reads the small, deliberately limited
subset of markdown actually used in this journal (## headings, *italic*,
**bold**, `inline code`, fenced ``` code blocks, > blockquotes,
paragraphs) and writes index.html + posts/<slug>.html into an output
directory.

Usage: build_site.py [output_dir]   (default: _site)
"""
import datetime
import html
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = REPO_ROOT / "posts"
CHARTER_PATH = REPO_ROOT / "CHARTER.md"
STATIC_DIR = REPO_ROOT / "static"
BASE_URL = "https://dailyamnesia.com"
UNCOMMITTED_SENTINEL = "￿"
FEED_LINK = (
    '<link rel="alternate" type="application/atom+xml" title="Daily Amnesia" href="/feed.xml">\n'
    '<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
)

CSS = """
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    max-width: 38rem;
    margin: 4rem auto;
    padding: 0 1.25rem;
    line-height: 1.55;
    color: #1a1a1a;
  }
  h1 { font-size: 1.6rem; margin-bottom: 0.2rem; }
  h2 { font-size: 1.25rem; margin-top: 2rem; }
  .tagline { color: #555; margin-top: 0; }
  .start-here {
    background: #f2f6ff;
    border: 1px solid #d6e2ff;
    border-radius: 6px;
    padding: 0.8rem 1rem;
    font-size: 0.95rem;
  }
  .post-date { color: #6e6e6e; font-size: 0.9rem; }
  a { color: #0b5fff; }
  ul.posts { padding-left: 0; list-style: none; }
  ul.posts li { margin-bottom: 1rem; }
  blockquote {
    margin: 1.25rem 0;
    padding-left: 1rem;
    border-left: 3px solid #d6e2ff;
    color: #444;
  }
  blockquote p { margin: 0; }
  code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  code { background: #f2f2f2; padding: 0.1em 0.3em; border-radius: 3px; font-size: 0.92em; }
  pre { background: #f2f2f2; padding: 0.8rem 1rem; border-radius: 5px; overflow-x: auto; }
  pre code { background: none; padding: 0; }
  footer { margin-top: 3rem; color: #6e6e6e; font-size: 0.9rem; }
  .back { display: inline-block; margin-bottom: 1.5rem; }
  .post-nav {
    display: flex;
    gap: 1.5rem;
    margin-top: 2.5rem;
    padding-top: 1.25rem;
    border-top: 1px solid #e5e5e5;
    font-size: 0.95rem;
  }
  .post-nav a { max-width: 45%; }
  .post-nav a.next { margin-left: auto; text-align: right; }
  .post-nav .nav-label { display: block; color: #6e6e6e; font-size: 0.85rem; }
"""


def page(title, body_html, extra_head=FEED_LINK, description=None):
    # `description` distinguishes "no description was ever computed" (the
    # `None` default -- e.g. a bare page() call with nothing to say) from
    # "a description was computed and it happened to be empty" (e.g. a post
    # whose body opens with a heading or a fenced code block and has no
    # leading paragraph at all, so _summary() legitimately returns ""). A
    # truthy check (`if description else ""`) treated both the same way,
    # silently dropping the <meta name="description"> tag for the second
    # case -- the same "every page carries this tag" invariant this project
    # already fixed once for the 404 page, just resurfacing through a
    # post's actual content instead. Checking `is not None` keeps the
    # documented "no argument at all" behavior (see
    # test_no_description_by_default) while still emitting the tag -- with
    # empty content -- for a description that was actually computed as
    # empty, instead of omitting it outright.
    meta_description = (
        f'<meta name="description" content="{html.escape(description)}">\n'
        if description is not None
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
{meta_description}<style>{CSS}</style>
{extra_head}</head>
<body>
{body_html}
</body>
</html>
"""


_first_commit_time_cache = {}


def _first_commit_time(path):
    """ISO timestamp of the post file's first commit, for ordering same-date posts.

    Falls back to a sentinel that sorts after every real timestamp, so an
    uncommitted (just-written) post still lands first, newest-first.

    A full `--follow` history walk per post gets expensive as the repo's
    post count and commit history grow (each call re-walks the whole
    history looking for renames), so results are memoized for the life of
    this process -- safe because a given (REPO_ROOT, path) pair's git
    history doesn't change mid-process, the only way this function's
    result could legitimately differ between two calls.
    """
    cache_key = (REPO_ROOT, path)
    if cache_key in _first_commit_time_cache:
        return _first_commit_time_cache[cache_key]
    try:
        result = subprocess.run(
            # -M100% keeps --follow able to track a genuine rename (a plain
            # `git mv` is always 100% similar to itself) while refusing to
            # pair this file with a *different*, merely-similar file that
            # was never renamed from or to it. Without it, git's rename/copy
            # detector (on by default under --follow, at a 50% similarity
            # threshold) happily paired an unrelated same-date post sharing
            # this journal's own frontmatter boilerplate as a "copy," and
            # `lines[-1]` (meant to be this file's own first commit) silently
            # returned that other post's earlier commit time instead --
            # corrupting the sort key this function exists to provide, for
            # exactly the same-date-multiple-posts case it's meant to order.
            ["git", "log", "-M100%", "--follow", "--format=%aI", "--", str(path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        lines = result.stdout.strip().splitlines()
        value = lines[-1] if lines else UNCOMMITTED_SENTINEL
    except (subprocess.CalledProcessError, FileNotFoundError):
        value = UNCOMMITTED_SENTINEL
    _first_commit_time_cache[cache_key] = value
    return value


def _is_blank(value):
    """True if `value` has no visible content once ordinary whitespace,
    invisible Unicode formatting characters (category 'Cf' -- e.g. U+200B
    ZERO WIDTH SPACE, U+FEFF ZERO WIDTH NO-BREAK SPACE/BOM, U+200E/U+200F
    the left/right-to-left marks), and variation selectors are set aside.

    `str.strip()` -- used both for the plain pre-quote trim and the
    post-unquote trim that already closed the "title: \"   \"" and
    "title: \"  padded  \"" gaps -- only recognizes characters
    `str.isspace()` calls whitespace, and Cf-category formatting characters
    aren't in that set (they're invisible, not whitespace, as far as
    Unicode's own categorization goes). A required value made entirely of
    such characters -- e.g. `title: "​​​"`, three zero-width
    spaces -- survives every existing strip untouched, stays non-empty
    (three real characters), and sails past `not meta.get(required)` as
    truthy. The post then builds successfully with a `<title>`/`<h1>` and
    an index link that are all present in the markup but carry no visible
    or accessible text at all -- the identical "blank-looking required
    value" failure the two prior fixes closed, just through a character
    class neither of them checked for.

    A title made entirely of variation selectors (U+FE00-U+FE0F, e.g. the
    emoji-presentation U+FE0F left behind if an edit deletes the base emoji
    it modified; or U+E0100-U+E01EF, the ideographic variation selectors
    used to pick a glyph variant for a CJK character) is just as invisible
    as a Cf character -- a variation selector only ever modifies the
    presentation of the character immediately before it and has no glyph of
    its own -- but it isn't category 'Cf': the Unicode Character Database
    files it under 'Mn' (nonspacing mark), the same general category as an
    ordinary combining diacritic that *does* render visibly on its own.
    Checking `unicodedata.category(ch) == "Cf"` alone therefore let a
    variation-selector-only title sail through exactly like the zero-width-
    space case once did, with no base character anywhere in the value for
    the selectors to attach to.
    """
    return all(_is_invisible_char(ch) for ch in value)


def _is_invisible_char(ch):
    """True if `ch` renders as nothing -- ordinary whitespace, a Cf-category
    Unicode formatting character, a variation selector, a Hangul filler, or
    one of the other invisible-but-not-whitespace code points enumerated
    below. Factored out of `_is_blank()` (see its docstring for the full
    history of why each of these classes needed its own check) so the same
    "is this invisible" test can be reused anywhere a caller needs to tell
    a genuinely visible character apart from one that merely occupies a
    string position -- see `_has_invisible_boundary()` below for the other
    caller.
    """
    if ch.isspace() or unicodedata.category(ch) == "Cf":
        return True
    code = ord(ch)
    if 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF:
        return True
    # Hangul filler characters -- U+115F HANGUL CHOSEONG FILLER, U+1160
    # HANGUL JUNGSEONG FILLER, U+3164 HANGUL FILLER, and U+FFA0
    # HALFWIDTH HANGUL FILLER -- are placeholder code points that exist
    # so an incomplete Hangul jamo sequence still has a slot to combine
    # into a syllable block; none of the four carry a glyph of their
    # own, the same "renders as nothing" trait as a Cf character or a
    # variation selector (and the same trick some chat platforms'
    # "blank name" workarounds actually use). But the Unicode Character
    # Database files all four under general category 'Lo' (letter,
    # other), not 'Cf' or 'Mn': for the purposes of Hangul composition
    # they behave as ordinary letters, so neither check above catches
    # them. A title made entirely of these (e.g. `title: "ㅤㅤㅤ"`,
    # three U+3164 characters) is exactly as blank-looking as the
    # zero-width-space and variation-selector cases already caught
    # above, but used to sail past _is_blank() -- and therefore the
    # `not meta.get(required)` check in parse_post() -- as three "real"
    # characters, reaching <title>/<h1>/the index link as markup that's
    # present but carries no visible or accessible text at all.
    if code in (0x115F, 0x1160, 0x3164, 0xFFA0):
        return True
    # A sixth class, sharing the same root cause as the four checks
    # above -- a code point with no glyph of its own, meant only to
    # modify or separate the characters around it -- but filed under
    # yet another general category that isn't whitespace, 'Cf', a
    # variation selector, or a Hangul filler: U+034F COMBINING GRAPHEME
    # JOINER (used to block otherwise-automatic combining/ligating
    # behavior between two adjacent characters, invisible with nothing
    # to join when it appears alone) and U+180B-U+180D/U+180F, the
    # Mongolian free variation selectors one through four (siblings of
    # the U+FE00-U+FE0F block already checked above, just a separate
    # Unicode block for a different script's glyph-variant system), and
    # U+17B4-U+17B5, the Khmer inherent vowel signs (present purely to
    # override a consonant's own default inherent vowel and, standing
    # alone with no consonant to modify, invisible the same way). All
    # five are Unicode category 'Mn' (nonspacing mark) -- the identical
    # category the variation-selector fix above already had to look
    # past once, since 'Mn' also holds ordinary diacritics that *do*
    # render on their own -- so a title made entirely of these (e.g.
    # three U+034F characters) sailed through every check above as
    # three "real" characters, reaching <title>/<h1>/the index link
    # with no visible or accessible text at all, the same failure mode
    # as all five prior fixes.
    if code == 0x034F or 0x180B <= code <= 0x180D or code == 0x180F or 0x17B4 <= code <= 0x17B5:
        return True
    # A seventh class: the C0 control range's own tail (e.g. U+007F
    # DELETE) and the C1 control range (U+0080-U+009F) -- both category
    # 'Cc', the same general category as the C0 control characters
    # (U+0000-U+001F) that parse_post()'s required-key check already
    # handles, but through a different mechanism than every check above:
    # that check runs `_is_blank()` against the value *after*
    # `_strip_invalid_xml_chars()` has already removed C0 controls (they're
    # forbidden outright by XML 1.0), so a title made only of C0 controls
    # is caught by arriving at that check already empty, not by
    # `_is_blank()` itself recognizing 'Cc' as invisible. U+007F and the
    # U+0080-U+009F range are just as much non-printable control
    # characters -- neither has a glyph of its own, the identical
    # "renders as nothing" trait as every class above -- but
    # `_strip_invalid_xml_chars()` deliberately leaves them alone: unlike
    # the C0 range, both are valid XML 1.0 characters (the Char
    # production's `[#x20-#xD7FF]` range includes them), so stripping them
    # would be removing well-formed content, not sanitizing malformed
    # content. That left a gap the C0 case's indirect fix never covered: a
    # title made entirely of these (e.g. `title: "\x91\x91\x91"`, three
    # U+0091 PRIVATE USE ONE characters, or a "## \x7f\x7f\x7f" heading
    # inside a post body) reaches `_is_blank()`/`_is_blank_markdown()` as
    # three "real" characters -- not whitespace, not 'Cf', not any of the
    # six classes above -- survives `_strip_invalid_xml_chars()` completely
    # unchanged, and ships as a `<title>`/`<h1>`/`<h2>` that's present in
    # the markup but carries no visible or accessible text at all, the
    # same failure mode as all six prior fixes. Checking the general
    # category directly, the same test already used for 'Cf' on the first
    # line of this function, closes the gap for this class the same way.
    #
    # That check -- `unicodedata.category(ch) == "Cc"` -- covers the whole
    # 'Cc' general category, not just the U+007F/U+0080-U+009F range
    # described above. 'Cc' also includes the C0 control range
    # (U+0000-U+001F), which this function was never meant to re-check on
    # its own merits: real post content never carries a C0 byte by the time
    # it reaches here, since parse_post() already runs every title/body
    # through `_strip_invalid_xml_chars()` (which removes C0 outright)
    # before this function -- or anything downstream of it -- ever sees the
    # value. But `_has_invisible_boundary()` below doesn't only call this on
    # raw post content: it checks the *captured text of an already-matched
    # emphasis span*, and by the time `_BOLD_RE`/`_ITALIC_RE` run, that text
    # can contain this file's own internal placeholder characters --
    # `_stash_code_spans()`'s "\x00N\x00" and the bold-stash "\x01N\x01"
    # markers both render_inline() and _summary() build around a matched
    # `**bold**` span -- both made of C0 bytes (U+0000/U+0001) precisely
    # because real content can never contain them. A blanket 'Cc' check
    # can't tell "a placeholder this file inserted itself" apart from "a
    # genuine DEL/C1 control byte a reader would never see": both satisfy
    # `category(ch) == "Cc"`. So an emphasis span whose boundary happens to
    # be a stashed code span (e.g. "*`code`text*" -- the boundary character
    # is the "\x00" that opens the code-span placeholder) was rejected as
    # having an "invisible boundary" and left as literal, unrendered
    # asterisks around a stray <code> tag, even though a code span is about
    # as visible as content gets. render_inline("*`code`text*") used to
    # produce "*<code>code</code>text*" instead of
    # "<em><code>code</code>text</em>", and _summary("*`code`text*") used to
    # produce "*codetext*" instead of "codetext" -- both losing real
    # emphasis markup to a false positive from this file's own bookkeeping,
    # not from anything a post's author actually wrote. Checking the
    # specific U+007F/U+0080-U+009F range directly, instead of the whole
    # 'Cc' category, keeps catching the DEL/C1 case this check exists for
    # while leaving the placeholder bytes -- entirely inside the C0 range
    # this function doesn't need to cover on its own, per the paragraph
    # above -- alone.
    return code == 0x7F or 0x80 <= code <= 0x9F


def _has_unescaped_closing_quote(value):
    """True if `value` (a frontmatter value already confirmed to start and
    end with a literal '"') actually ends on a real, unescaped closing
    quote -- not the second half of an escaped `\\"` sequence with no real
    closing quote anywhere after it.

    The frontmatter quote-stripping below decides a value is "wrapped in
    quotes" purely by checking whether `value[0]` and `value[-1]` are both
    `"` (see the caller). That check can't tell a genuine closing quote
    apart from an escaped one: a value like `"She said \\"stop\\"` -- an
    author who opened a quoted title, embedded an escaped `\\"stop\\"` for
    emphasis, and then forgot the actual closing quote for the title as a
    whole -- also happens to end in a literal `"` character, the same as a
    properly closed `"...\\"."` value does. Both satisfied the naive check
    and both used to get the outer quotes sliced off unconditionally: for
    the malformed case, slicing off `value[-1]` removes the escaped quote's
    own second half, leaving its paired backslash with nothing left to
    unescape (`value.replace('\\"', '"')` needs the closing `"` right next
    to it, and that's exactly the character just sliced away) -- so the
    stored title ends on a stray, literal backslash character instead of
    either the intended text or the original, unmodified raw line.
    parse_post() on `title: "She said \\"stop\\"` used to produce the title
    `She said "stop\\` (note the trailing backslash), reaching <title>,
    <h1>, the index link, and the feed entry.

    A closing quote is only "real" if it isn't itself escaped, i.e. the run
    of backslashes immediately before it has even length (0, 2, 4, ... --
    each pair is a would-be-escaped backslash followed by an ordinary
    character, never reaching this quote; this parser has no `\\\\` escape
    of its own, but the parity check stays correct either way). An odd-length
    run means the last backslash pairs with this final quote instead,
    escaping it, so it isn't a closing delimiter at all and the value was
    never actually terminated -- exactly like the unquoted-value case
    (`title: He said "no"`, see test_unquoted_value_ending_in_a_literal_
    quote_mark_is_not_mangled), the safest thing to do is leave the raw
    text -- quotes, backslashes, and all -- untouched, rather than silently
    "closing" a quote the author never actually closed.
    """
    backslash_run = 0
    idx = len(value) - 2
    while idx >= 0 and value[idx] == "\\":
        backslash_run += 1
        idx -= 1
    return backslash_run % 2 == 0


def parse_post(path):
    # UnicodeDecodeError (e.g. a post accidentally saved with Windows-1252
    # smart quotes, or any other stray non-UTF-8 byte) is itself a
    # ValueError subclass, so it already propagated out of this function
    # without changing its exception type -- but its message is whatever
    # the codec produced ("'utf-8' codec can't decode byte 0xff in position
    # 68: invalid start byte"), which never names the offending file. Every
    # other failure mode in this function -- missing frontmatter, an
    # unclosed '---', a missing/blank required key, a malformed date --
    # deliberately prefixes its message with `path` for exactly this
    # reason: with 100+ posts in the real directory, a build failure has to
    # point at which file broke it, not just how. Re-raising here closes
    # the one call site that skipped that convention.
    try:
        # "utf-8-sig", not plain "utf-8": several common Windows tools
        # (Notepad, PowerShell's Out-File/Set-Content, Excel's text export)
        # default to writing a leading UTF-8 byte-order mark (U+FEFF), which
        # is invisible in virtually any editor. Plain "utf-8" decodes that
        # BOM as a real leading character instead of stripping it, so
        # `text.startswith("---\n")` below fails on an otherwise
        # perfectly well-formed post -- and since build() has no per-post
        # exception handling, that one invisible byte crashes the entire
        # site build. "utf-8-sig" strips a leading BOM if present and
        # otherwise decodes identically to "utf-8", so this is safe for
        # every file, BOM or not -- the same fix already applied to
        # flashback's own deck-file reader for the identical reason.
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(f"{path}: not valid UTF-8: {e}") from None
    except OSError as e:
        # A `*.md` glob match that turns out not to be a plain, readable
        # file -- most plausibly a directory left behind by a `mkdir` typo
        # (e.g. "posts/2026-01-01-new-post.md/" created instead of the
        # intended file), but also an unreadable file from a permissions
        # slip -- raises IsADirectoryError/PermissionError, neither of which
        # is a UnicodeDecodeError, so it skipped the except clause above
        # entirely and propagated as a raw, unnamed OSError instead of this
        # function's own "which file broke the build" framing every other
        # failure mode here deliberately provides. Wrapping the whole
        # OSError family closes this for any such case at once instead of
        # enumerating each possible errno individually.
        raise ValueError(f"{path}: could not be read: {e}") from None
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"{path}: frontmatter opened with '---' but never closed")
    frontmatter, body = text[4:end], text[end + 5:]
    meta = {}
    for line in frontmatter.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == '"'
            and value[-1] == '"'
            and _has_unescaped_closing_quote(value)
        ):
            # Strip again after removing the quotes, not just before: the
            # `value.strip()` above only ever trims whitespace *outside* the
            # quoted pair (nothing after the colon but leading/trailing
            # blanks around the quotes themselves), never whitespace that
            # sits *inside* them. A whitespace-only quoted value (`title:
            # "   "`) was already caught below, but a quoted value that's
            # merely *padded* around real content (`title: "  Real Title
            # "`) was not: it used to sail through as the literal stored
            # title -- `'  Real Title  '`, padding and all -- reaching
            # <title>, <h1>, the index link text, and the feed's own
            # <title> unstripped, while writing the exact same title
            # *without* quotes already came out clean (the pre-quote
            # `.strip()` handles that case fine on its own). Stripping here
            # closes the gap at its source so every downstream consumer
            # gets the same clean value a quoted value is supposed to carry.
            value = value[1:-1].strip()
            # A quoted value has no other way to carry a literal quote mark
            # than escaping it (`\"`), the same convention JSON/YAML-style
            # quoted strings use -- e.g. a real post's own
            # `title: "A deck named \".\" blamed the wrong thing"`, meant to
            # render as `A deck named "." blamed the wrong thing`. Nothing
            # above ever un-escapes that: the slice-off-the-outer-quotes
            # step just removes the first and last characters, leaving any
            # `\"` in the middle untouched, so the stored title kept its
            # literal backslashes and shipped that garbled text to <title>,
            # <h1>, the index link, and the feed entry for the real post
            # that used this syntax.
            value = value.replace('\\"', '"')
        meta[key.strip()] = value
    for required in ("title", "date"):
        # A key present but left blank (e.g. "date:" with nothing after it)
        # is just as broken as the key being absent -- `not in meta` alone
        # missed it, letting an empty string reach the rendered post and a
        # malformed feed <updated> timestamp instead of failing here. A
        # quoted value containing only whitespace (e.g. `title: "   "`) is
        # just as broken and is now caught the same way, since the parsing
        # loop above already strips it down to an empty string before it
        # ever reaches this check. A value made entirely of invisible
        # Unicode formatting characters (e.g. zero-width spaces) is just as
        # broken too, but survives every `.strip()` call above untouched --
        # see `_is_blank()` for why plain `not meta.get(required)` alone
        # still misses it. A value made entirely of raw control characters
        # (e.g. a stray ESC from a pasted terminal log, `title: "\x01"`) is
        # just as broken too, but for a different reason than the Unicode
        # formatting case: `_is_blank()` doesn't consider it blank at all --
        # a C0 control character is category 'Cc', not the 'Cf' the
        # zero-width-space fix checks for, and it isn't whitespace either --
        # so this check used to pass it straight through. The value then
        # reached the return dict below, where `_strip_invalid_xml_chars()`
        # (there to keep such bytes out of feed.xml) stripped that same
        # character back out, leaving the *stored* title empty even though
        # this check had just approved it as non-blank -- the identical
        # blank-<title>/<h1>/index-link failure the two fixes above already
        # closed, just reached through a mismatch between what this check
        # inspects and what actually reaches the page. Running `_is_blank()`
        # against the already-sanitized value keeps this check in sync with
        # what the post will actually render.
        if not meta.get(required) or _is_blank(_strip_invalid_xml_chars(meta[required])):
            raise ValueError(f"{path}: frontmatter is missing required key {required!r}")
    # A non-empty but wrongly-formatted date (e.g. "Aug 30, 2026", or a
    # copy-paste of "08/30/2026") passes the check above and used to flow
    # straight through: it corrupts the newest-first sort (posts.sort()
    # compares "date" as a plain string, so a differently-formatted value
    # lands wherever its characters happen to compare, not where the post
    # was actually written) and produces a malformed feed <updated> value
    # (e.g. "Aug 30, 2026T00:00:00Z"). Matched against an exact YYYY-MM-DD
    # pattern rather than `datetime.date.fromisoformat()` alone, which
    # since Python 3.11 also accepts dashless "20260830" and ISO week dates
    # -- neither matches this journal's actual convention. A value that
    # merely looks right ("2026-02-30") is caught too, by constructing the
    # date rather than only matching the pattern.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta["date"]):
        raise ValueError(
            f"{path}: frontmatter 'date' must be in YYYY-MM-DD form, got {meta['date']!r}"
        )
    try:
        datetime.date.fromisoformat(meta["date"])
    except ValueError:
        raise ValueError(
            f"{path}: frontmatter 'date' is not a real calendar date: {meta['date']!r}"
        ) from None
    slug = path.stem
    # render_feed() already strips characters XML 1.0 forbids outright
    # (control bytes, lone surrogates, the U+FFFE/U+FFFF noncharacters --
    # see _strip_invalid_xml_chars()) from a post's title/body before they
    # reach feed.xml, so a stray control byte (e.g. an ESC from a pasted
    # terminal log) can't break the feed. But that sanitizing only ever ran
    # at the one call site that needed well-formed XML: every *HTML* page
    # built from this same title/body -- the post's own <title>/<h1>/<meta
    # description>, and the index page's listing -- read straight from
    # parse_post()'s output and never went anywhere near render_feed(), so
    # the same post shipped a clean feed entry alongside an HTML page still
    # carrying the raw control byte. Stripping once here, at the single
    # place every post's raw title/body first gets read, reaches every
    # downstream consumer at once instead of relying on each to remember it.
    return {
        "slug": slug,
        "title": _strip_invalid_xml_chars(meta["title"]),
        "date": meta["date"],
        "commit_time": _first_commit_time(path),
        "body": _strip_invalid_xml_chars(body.strip("\n")),
    }


def parse_charter(path=CHARTER_PATH):
    """CHARTER.md's own leading `# Title` line becomes the page title (the
    site's markdown subset only renders `##`-and-deeper as headings), the
    rest is rendered like a post body."""
    # Same rule as parse_post() above: name the file in the error rather
    # than let a raw UnicodeDecodeError (itself a ValueError, just one with
    # no path in its message) pass through unlabeled.
    try:
        # See parse_post() above for why "utf-8-sig" and not plain "utf-8":
        # a leading UTF-8 byte-order mark is invisible in virtually any
        # editor but would otherwise land as the first character of
        # `title_line`, failing the `startswith("# ")` check below.
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(f"{path}: not valid UTF-8: {e}") from None
    except OSError as e:
        # Same gap as parse_post()'s own fix above, on this function's
        # single sibling read: CHARTER.md being a directory (or otherwise
        # unreadable) raises IsADirectoryError/PermissionError, neither a
        # UnicodeDecodeError, so it skipped the except clause above and
        # propagated as a raw, unnamed OSError instead of this function's
        # own "which file broke it" convention.
        raise ValueError(f"{path}: could not be read: {e}") from None
    title_line, _, body = text.partition("\n")
    if not title_line.startswith("# "):
        raise ValueError(f"{path}: expected a leading '# Title' line")
    # Same rule as parse_post() above: sanitize once at the source rather
    # than leave every HTML call site to apply it individually.
    title = _strip_invalid_xml_chars(title_line[2:].strip())
    # parse_post() rejects a blank-or-blank-looking title (plain empty,
    # whitespace-only, or made entirely of invisible Unicode formatting
    # characters/variation selectors/Hangul fillers -- see _is_blank())
    # before it can reach a post's own <title>/<h1>/index link with no
    # visible or accessible text at all. charter.html is built from this
    # same title through the same page() call and carries the identical
    # "every page has a real title" invariant, but this function never
    # ran the equivalent check: a `# ` line with nothing (or nothing
    # visible) after it -- e.g. CHARTER.md accidentally saved with a bare
    # "# " leading line, or one edited down to just a trailing zero-width
    # space -- passed the `startswith("# ")` check above just fine and
    # shipped a blank <title></title> and <h1></h1> on the one page that
    # exists specifically to state this project's ground rules.
    if _is_blank(title):
        raise ValueError(f"{path}: '# Title' line has no visible title text")
    return (
        title,
        _strip_invalid_xml_chars(body.strip("\n")),
    )


def _stash_code_spans(text):
    """Replace each code span with a \\x00N\\x00 placeholder, returning the
    placeholder-substituted text and the list of stashed span contents.

    A code span opens with a run of one or more backticks and closes at the
    next run of *exactly* the same length -- the standard markdown escape
    for putting a literal backtick inside a code span is to delimit it with
    a longer run (e.g. "`` `code` ``" is a code span whose content is the
    literal text "`code`"), with one leading/trailing space trimmed when
    both edges are a space and the content isn't all spaces (so the
    delimiter can visually separate from an inner leading/trailing
    backtick). A plain `re.sub(r"`([^`]+)`", ...)` pairing single backticks
    two at a time has no notion of run length: it sliced a
    double-backtick-delimited span into several bogus single-backtick
    spans instead of reading it as one -- e.g. render_inline("`` `code`
    ``") used to produce "`<code> </code>code<code> </code>`" (a leaked
    literal backtick and mismatched spans) instead of a single
    "<code>`code`</code>". This isn't hypothetical: real posts in this
    journal use the double-backtick idiom in prose describing the
    renderer's own backtick handling, and it rendered broken -- including
    unrelated "*" characters later in the same paragraph getting swept up
    as emphasis, since the stray leftover backtick from the botched pairing
    left the code-span stash unable to protect the rest of the line.
    An opening run with no same-length closing run later in the text is
    left as literal backticks, same as a single unmatched backtick always
    was.
    """
    code_spans = []
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "`":
            out.append(text[i])
            i += 1
            continue
        run_start = i
        while i < n and text[i] == "`":
            i += 1
        run_len = i - run_start
        content_start = i
        k = content_start
        close_start = close_end = None
        while k < n:
            if text[k] == "`":
                close_run_start = k
                while k < n and text[k] == "`":
                    k += 1
                if k - close_run_start == run_len:
                    close_start, close_end = close_run_start, k
                    break
            else:
                k += 1
        if close_start is None:
            out.append(text[run_start:content_start])
            continue
        content = text[content_start:close_start]
        if len(content) >= 2 and content[0] == " " and content[-1] == " " and content.strip(" "):
            content = content[1:-1]
        code_spans.append(content)
        out.append(f"\x00{len(code_spans) - 1}\x00")
        i = close_end
    return "".join(out), code_spans


_CODE_SPAN_PLACEHOLDER_RE = re.compile(r"\x00\d+\x00")


def _is_blank_markdown(text):
    """True if `text` -- raw markdown source, not rendered output -- has no
    visible content once code spans are resolved the same way render_inline()
    resolves them.

    A code span's own delimiting backticks are visible characters, so
    `_is_blank(text)` alone reports a heading like "## ` `" or "## `​`"
    (a code span whose only content is an ordinary space or an invisible
    Unicode character) as non-blank -- the backticks themselves are real,
    visible text, and `_is_blank()` has no notion of markdown syntax at all.
    But by the time render_inline() is done with it, those backticks become
    a <code> tag wrapping content that is itself invisible, leaving a
    heading that's present in the markup with no visible or accessible text
    at all: render_markdown("## ` `\\n") used to produce
    "<h2><code> </code></h2>", and render_markdown("## `​`\\n") (a
    zero-width space inside the span) used to produce
    "<h2><code>​</code></h2>" -- exactly the blank-heading failure mode
    the `_is_blank(heading_text)` check below exists to catch, just reached
    through a code span's delimiters standing in for the missing visible
    text instead of an already-invisible character sailing through
    untouched the way every other case that check was written for does.

    Stashing code spans out the same way render_inline() does and then
    checking each stashed span's own content the same way
    `_has_invisible_boundary()` already does for bold/italic (an emphasis
    match with an invisible boundary is left as literal, visible delimiter
    characters instead of being wrapped -- see that function) closes the
    gap: a heading is only blank if the text outside every code span is
    blank *and* every code span's own content is blank too, matching what
    the heading actually renders to instead of what its raw source happens
    to contain.

    That still isn't the whole gap, though: a code span whose own content is
    blank doesn't have to sit bare in the heading -- it can be wrapped in
    *italic*/**bold** too, e.g. "## *` `*" or "## **`​`**". The "**"/"*"
    delimiters around it are visible characters, exactly like a bare code
    span's own backticks, so simply removing the code-span placeholder from
    `remainder` above left them behind and `_is_blank(remainder)` reported
    "**" or "*...*" as non-blank text -- except render_inline() doesn't
    leave those delimiters as literal text at all: a code-span placeholder
    is never itself invisible to `_has_invisible_boundary()` (see its own
    docstring for why -- a stashed code span has to count as a "visible"
    emphasis boundary so a *real* one, like "*`code`text*", still renders),
    so the emphasis match succeeds and consumes the "**"/"*" delimiters into
    <em>/<strong> tags around the still-blank <code> content, leaving
    nothing a reader or screen reader can perceive at all.
    render_markdown("## *`​`*\\n") used to produce
    "<h2><em><code>​</code></em></h2>" -- present in the markup, and not
    caught by the heading's own `_is_blank_markdown()` check, since that
    check only ever accounted for a bare code span, not one still wrapped in
    emphasis markers that go on to consume their own delimiters. Resolving
    bold/italic the same way `_summary()` does -- via `_bold_strip_replace()`/
    `_italic_strip_replace()`, the same functions that already correctly
    leave a match's delimiters literal when `_has_invisible_boundary()`
    rejects it -- before checking `remainder` for blankness keeps this check
    in sync with what render_inline() actually consumes.
    """
    stashed, code_spans = _stash_code_spans(text)
    bold_spans = []

    def _stash_bold(match):
        replacement = _bold_strip_replace(match)
        if "*" not in replacement:
            return replacement
        bold_spans.append(replacement)
        return f"\x01{len(bold_spans) - 1}\x01"

    resolved = _BOLD_RE.sub(_stash_bold, stashed)
    resolved = _ITALIC_RE.sub(_italic_strip_replace, resolved)
    for i, span in enumerate(bold_spans):
        resolved = resolved.replace(f"\x01{i}\x01", span)
    remainder = _CODE_SPAN_PLACEHOLDER_RE.sub("", resolved)
    return _is_blank(remainder) and all(_is_blank(span) for span in code_spans)


# Bold/italic matching, shared between render_inline() (HTML output) and
# _summary() (plain-text feed/description output) so the two can't drift on
# what counts as real emphasis markup vs. a literal "*" character -- session
# 130 found _summary() had drifted onto a blind `[`*]` character strip
# instead, corrupting literal asterisks/backticks (including inside code
# spans) rather than only removing real markdown delimiters.
#
# The captured text must start and end on a non-space, non-"*"
# character (a literal " * " used as multiplication, with a second
# unrelated "*" later in the same paragraph, was otherwise swept up as
# emphasis -- e.g. "3 * 4 * 5 = 60" rendered "4" in <em> tags for no
# reason; excluding "*" from the boundary too, not just whitespace,
# keeps back-to-back asterisks like "2 ** 3 ** 4" from being paired
# via their inner single "*" characters instead).
#
# The middle of a **bold** match allows a nested *italic* run (e.g.
# "**bold *and italic* together**") but never a bare "**" (so it can't
# cross into an unrelated bold delimiter or a "**" used as a literal
# exponent operator) -- bold used to exclude "*" everywhere in the
# middle, not just at the boundary, so any bold text containing a
# nested italic run failed to match at all and its "**" delimiters
# leaked into the page as literal asterisks instead of becoming
# <strong>.
#
# Any "*" allowed in the middle must belong to such a self-contained,
# already-paired *...* run, never a single unmatched "*" -- an earlier
# version allowed any lone "*" there (matching "\*(?!\*)" one character
# at a time, with no requirement that it pair up with anything). A bold
# span with a genuinely unpaired "*" in its middle -- e.g. from a
# literal, space-free multiplication like "2*a" -- still matched as
# bold, leaving that "*" un-rendered inside the new <strong>...</strong>
# text. The later, separate italic pass then still saw that raw "*" as
# an ordinary character and was free to pair it with an unrelated "*"
# later in the same paragraph, including one that came after the
# closing "</strong>" -- producing crossing, invalid markup instead of
# well-formed nested or sibling elements:
# render_inline("**2*a***ba*") used to render the mismatched
# '<strong>2<em>a</strong></em>ba*' (an <em> that opens before
# </strong> and closes after it). Requiring every inner "*" to be part
# of its own matched pair closes that gap: the stray "*a*" no longer
# matches as bold's middle at all, leaving well-formed output instead.
_ITALIC_INNER = r"(?<!\*)\*(?!\*)[^*\s](?:[^*]*[^*\s])?(?<!\*)\*(?!\*)"
_BOLD_RE = re.compile(r"\*\*([^*\s](?:(?:[^*]|" + _ITALIC_INNER + r")*[^*\s])?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)([^*\s](?:[^*]*[^*\s])?)(?<!\*)\*(?!\*)")


def _has_invisible_boundary(text):
    """True if `text` -- a **bold**/*italic* match's own captured middle --
    starts or ends on a character `_is_invisible_char()` treats as
    rendering to nothing, rather than a genuinely visible one.

    _BOLD_RE/_ITALIC_RE's own `[^*\\s]` boundary requirement exists so a
    literal, space-delimited "*" (e.g. the multiplication in "3 * 4 * 5")
    isn't misread as an emphasis delimiter -- see the comment above these
    patterns. But `\\s`, like `str.isspace()` elsewhere in this file, only
    recognizes *whitespace* as a non-content boundary; an invisible-but-
    not-whitespace character sitting in exactly that spot (e.g. a zero-
    width space pasted in immediately after the "*", from
    "3 *​4​* 5 = 60") is neither "*" nor whitespace to the regex,
    so it satisfies `[^*\\s]` and the match succeeds anyway -- reintroducing
    the identical literal-asterisk-as-emphasis failure the multiplication
    case was fixed for, just through a character invisible to `\\s` instead
    of one blank to the eye. render_inline("3 *​4​* 5 = 60") used
    to render "3 <em>​4​</em> 5 = 60" -- a real <em> wrapped
    around what still visually reads as bare multiplication -- while
    _summary() of the same text silently dropped the "*" characters
    instead, both treating the invisible characters as if they were the
    ordinary spaces they're standing in for. Rejecting a match whose own
    boundary character is invisible (the same test `_is_blank()` already
    applies to a whole frontmatter value, applied here to just the two
    edge characters) keeps both renderers leaving it as literal text
    instead, exactly as they already do for a literal space.
    """
    return _is_invisible_char(text[0]) or _is_invisible_char(text[-1])


def _italic_replace(match):
    inner = match.group(1)
    if _has_invisible_boundary(inner):
        return match.group(0)
    return f"<em>{inner}</em>"


def _italic_strip_replace(match):
    # _summary()'s plain-text sibling of _italic_replace() above -- see
    # _has_invisible_boundary() for why a match can still need rejecting
    # here even though it already satisfied _ITALIC_RE's own boundary
    # class.
    inner = match.group(1)
    if _has_invisible_boundary(inner):
        return match.group(0)
    return inner


def _bold_replace(match):
    # A bold match whose own captured text starts or ends on an invisible
    # character (see _has_invisible_boundary()) is exactly as much a false
    # positive as the nested-italic case below, just at the outer <strong>
    # level instead of the inner <em> one -- left as literal text rather
    # than wrapped.
    if _has_invisible_boundary(match.group(1)):
        return match.group(0)
    # Convert any nested *italic* run inside the bold match's own captured
    # text to <em> right here, before the <strong> wrapper is spliced back
    # into the string -- not left for the later, separate _ITALIC_RE.sub()
    # pass below to find on its own. That pass runs over the *entire*
    # string with no notion of HTML tag boundaries (same as _BOLD_RE.sub()
    # itself), so any raw "*" characters this match's captured group still
    # contained -- there because _BOLD_RE's own middle allows a
    # self-contained nested *...* run, e.g. the "*y*" in "***x*y*z***" --
    # sat there as ordinary text once <strong>...</strong> was inserted,
    # free for _ITALIC_RE to pair with an unrelated "*" *outside* this
    # match entirely (e.g. a leftover, unconsumed "*" from a triple-
    # asterisk bold+italic combo just before it). That produced crossing,
    # invalid markup instead of well-formed nesting:
    # render_inline("***x*y*z***") used to render
    # "<em><strong>x</em>y<em>z</strong></em>" -- an <em> that opens inside
    # <strong> and closes after it, straddling the </strong> boundary.
    # Resolving the nested italic before the <strong> tags ever reach the
    # string closes the gap: by the time _ITALIC_RE.sub() runs afterward,
    # there's no raw "*" left inside this match's output for it to trip
    # over.
    inner = _ITALIC_RE.sub(_italic_replace, match.group(1))
    return f"<strong>{inner}</strong>"


def _bold_strip_replace(match):
    # _summary()'s plain-text sibling of _bold_replace() above, for exactly
    # the same reason: a bold match's captured group can contain a nested
    # *italic* run (that's what lets "**bold *and italic* together**"
    # match _BOLD_RE at all), and those inner "*" characters have to be
    # resolved right here, scoped to this one match, before the substituted
    # text is spliced back in -- not left as raw asterisks for the later,
    # separate _ITALIC_RE.sub() pass in _summary() to find on its own. That
    # pass runs over the *entire* string with no notion of where this
    # match's boundaries were, so a raw "*" surviving inside it was free to
    # pair with an unrelated "*" *outside* the original bold span instead --
    # e.g. _summary("2*a a**b x*y ` `x*y` a**b") used to return
    # "2a ab xy  x*y` ab" (the literal "2*a" corrupted into "2a", and a
    # stray "*" left sitting in "x*y`") instead of matching what the actual
    # rendered post shows, because `_BOLD_RE.sub(r"\1", text)` spliced the
    # captured group straight back in via a plain backreference -- asterisks
    # and all -- instead of resolving its own nested italic first the same
    # way render_inline() does. Resolving it here, scoped to just this
    # match's own captured text, keeps _summary() in sync with render_inline()
    # on which "*" characters a bold span actually consumes.
    if _has_invisible_boundary(match.group(1)):
        return match.group(0)
    return _ITALIC_RE.sub(_italic_strip_replace, match.group(1))


def render_inline(text):
    text = html.escape(text)

    # Code spans are stashed out and restored after bold/italic run, so
    # e.g. `*not italic*` isn't itself reinterpreted as markdown.
    text, code_spans = _stash_code_spans(text)
    # _bold_replace() already resolves a bold match's own *nested* italic
    # run scoped to just that match, specifically so the raw "*" delimiters
    # of a nested "**bold *and italic* together**"-style run never reach
    # the separate _ITALIC_RE.sub() pass below as ordinary text -- see its
    # own comment. That guarantee depends on the nested resolution always
    # either fully consuming the inner "*...*" (turning it into <em>, no
    # asterisks left at all) or leaving it as an entirely separate,
    # unmatched run elsewhere. But when the nested run's own boundary is
    # invisible (e.g. a zero-width space right inside the "*...*", see
    # _has_invisible_boundary()), _italic_replace() rejects *just that
    # match* and hands back its "*...*" delimiters completely unresolved --
    # correct in isolation, but it leaves raw, still-"*"-delimited text
    # sitting inside this bold match's own <strong>...</strong> output,
    # which the _ITALIC_RE.sub() pass below (a blind, whole-string scan
    # with no notion of where this match's own boundaries were, the same
    # thing _bold_replace()'s own comment already warns about for the
    # "leftover unpaired '*'" case) is then free to pair with an unrelated,
    # unconsumed "*" *outside* this match entirely -- e.g. the extra
    # leading "*" of a "***" bold+italic combo just before it -- producing
    # crossing, invalid markup instead of well-formed nesting:
    # render_inline("***a*​*b**") used to render
    # "<em><strong>a</em>​*b</strong>" (an <em> that opens inside
    # <strong> and closes after it, straddling the boundary), the same
    # failure shape test_nested_italic_inside_triple_asterisk_bold_does_
    # not_cross_tags already guards against for the *successfully*-resolved
    # nested-italic case, just reached through the invisible-boundary
    # rejection path that fix didn't anticipate.
    #
    # Stashing a bold match's own output behind a placeholder -- the same
    # code-span technique used two lines above -- closes the gap, but only
    # needs to apply when that output still contains a raw "*": that's the
    # only case _ITALIC_RE (which can't match anything without a "*") could
    # possibly reach into. A clean <strong>...</strong> with no leftover
    # asterisk (the ordinary case, and the "***really important***" case --
    # its own group(1) has no "*" in it at all) is left exposed on purpose,
    # so the genuinely separate, single leading/trailing "*" of a "***"
    # combo -- outside this match's own captured text entirely -- can still
    # pair with its partner and wrap the whole <strong> block in <em>,
    # exactly as test_triple_asterisk_bold_italic_combo_still_nests_
    # correctly and test_nested_italic_inside_triple_asterisk_bold_does_
    # not_cross_tags already require.
    bold_spans = []

    def _stash_bold(match):
        replacement = _bold_replace(match)
        if "*" not in replacement:
            return replacement
        bold_spans.append(replacement)
        return f"\x01{len(bold_spans) - 1}\x01"

    text = _BOLD_RE.sub(_stash_bold, text)
    text = _ITALIC_RE.sub(_italic_replace, text)
    for i, span in enumerate(bold_spans):
        text = text.replace(f"\x01{i}\x01", span)
    for i, code in enumerate(code_spans):
        text = text.replace(f"\x00{i}\x00", f"<code>{code}</code>")
    return text


_FENCE_OPEN_RE = re.compile(r"^`{3,}")


def _fence_marker(line):
    """The backtick run that opens a fence, e.g. "```" or "````" -- only a
    line consisting of exactly that many backticks (no more, no fewer)
    closes it. A longer opener than 3 lets a post nest a literal shorter
    fence inside one (the standard trick for showing fence syntax without
    triggering it); render_markdown/_summary used to always close on the
    first bare "```" regardless of the opener's own length, so a "````"
    wrapper around content containing a real "```" line silently closed
    early -- the exact silently-corrupting failure mode the unterminated-
    fence ValueError below exists to prevent, just reachable through a
    4-backtick opener instead of the already-guarded 3-backtick case."""
    m = _FENCE_OPEN_RE.match(line)
    return m.group() if m else None


def _is_fence_close(line, marker):
    """True if `line` is the line that closes a fence opened with `marker`
    (the exact backtick run returned by `_fence_marker()`).

    render_markdown() and _summary() both used to compare with
    `line.rstrip() == marker` (or `!=`) directly -- but `str.rstrip()`, like
    `str.isspace()` elsewhere in this file, only trims ordinary *whitespace*
    from the right, not an invisible-but-not-whitespace Unicode formatting
    character (e.g. U+200B ZERO WIDTH SPACE) sitting in that same trailing
    spot -- the same gap `_is_blank()` exists to close for a blank line, a
    bare ">" blockquote continuation, and a heading's own text (see each of
    their comments for the concrete history). A closing fence line with such
    a character immediately after its backticks (very plausible: this file's
    own comments repeatedly cite "a zero-width space left behind by a
    paste" as the real-world source) survived `.rstrip()` with the invisible
    character still attached, so `lines[i].rstrip() != marker` came out
    true -- the fence was never recognized as closed at all. In
    render_markdown() that meant every line all the way to the end of the
    post -- including the post's real remaining paragraphs -- was swallowed
    as code content, and the missing close then raised "unterminated code
    fence", crashing the *entire* site build over what looks, to any human
    reading the post, like an already-closed fence. In _summary() the same
    comparison failure meant `in_code` never flipped back to False, so the
    scan silently ran out of lines still "inside" the fence -- discarding
    every real paragraph after it and shipping an empty description/feed
    summary instead of raising anything. Checking `_is_blank()` on whatever
    follows the marker -- the same test already used for every sibling case
    above -- accepts trailing invisible characters (and ordinary whitespace,
    subsuming the old `.rstrip()` behavior) while still rejecting a line
    with real trailing content (e.g. a language tag, or a longer/shorter
    backtick run), exactly like every other close-detection rule in this
    file already does.
    """
    return line.startswith(marker) and _is_blank(line[len(marker):])


def render_markdown(body, source="post"):
    lines = body.split("\n")
    out = []
    i = 0
    paragraph = []
    quote = []

    def flush_paragraph():
        # A code span wrapping nothing visible (e.g. "` `") isn't blank to
        # `_is_blank(line)` -- the backticks are visible -- so check the
        # joined, rendered text instead, same as flush_quote() below.
        if paragraph:
            joined = " ".join(paragraph)
            if not _is_blank_markdown(joined):
                out.append(f"<p>{render_inline(joined)}</p>")
            paragraph.clear()

    def flush_quote():
        # Each accumulated line already passed `_is_blank_markdown()`
        # individually (see the "> " branch below) before being appended
        # here -- but that per-line check only ever looks at one line's own
        # backticks, while this function joins every line with a single
        # space and resolves code spans (and bold/italic) across the
        # *joined* result, the same way flush_paragraph() does. A code
        # span's opening and closing backticks don't have to sit on the
        # same quote line: two lines each holding a single, unpaired "`"
        # (e.g. "> `" followed by "> `") are each individually non-blank on
        # their own -- an unmatched backtick is just a literal, visible
        # character -- so both survive the per-line filter and land here.
        # Once joined into "` `", though, those two backticks pair up into
        # a single code span whose content is nothing but the space between
        # them, and render_inline() turns that into "<code> </code>": a
        # <blockquote> that's present in the markup but carries no visible
        # or accessible text at all, the identical failure mode every other
        # `_is_blank_markdown()` check in this file exists to prevent, just
        # reached through a code span split across two lines instead of
        # sitting inside one. render_markdown("> `\\n> `\\n") used to
        # produce "<blockquote><p><code> </code></p></blockquote>".
        # Checking the fully joined text here -- the same text
        # render_inline() is about to render -- and discarding the whole
        # quote if it comes out blank (rather than emitting an empty
        # <blockquote>) matches how a quote made entirely of blank lines
        # already produces no element at all.
        if quote:
            joined = " ".join(quote)
            if not _is_blank_markdown(joined):
                out.append(f"<blockquote><p>{render_inline(joined)}</p></blockquote>")
            quote.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush_paragraph()
            flush_quote()
            marker = _fence_marker(line)
            i += 1
            code_lines = []
            # Only an exact line of the same backtick count as the opener
            # closes the fence -- the opening line is matched permissively
            # (startswith, so "```python"/"```bash" language tags work, as
            # real posts in this repo use extensively), but a content line
            # that happens to start with backticks too (e.g. a post
            # demonstrating this renderer's own fence syntax, wrapping a
            # sample "```python ... ```" block inside an outer fence) must
            # not itself be mistaken for the close. It used to be: any line
            # starting with "```" closed the fence, so
            # render_markdown("```\n```python\nprint(1)\n```\n```") -- an
            # outer fence containing a literal nested fenced example --
            # closed on the inner "```python" line, leaving an empty code
            # block, dumping "print(1)" out as a bogus visible paragraph,
            # and opening a second, also-empty code block from the line
            # that was meant to be the inner block's own close. Matching by
            # marker length (not a hardcoded "```") closes the same gap for
            # a 4-or-more-backtick opener wrapping literal ``` content --
            # see _fence_marker()'s own docstring. A plain `.rstrip()`
            # comparison here also missed a trailing invisible Unicode
            # character right after the marker -- see _is_fence_close()'s
            # own docstring for why that's not just a hypothetical and what
            # it broke.
            while i < len(lines) and not _is_fence_close(lines[i], marker):
                code_lines.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ValueError(f"{source}: unterminated code fence ({marker} opened but never closed)")
            out.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
            i += 1
            continue
        if line.startswith("## "):
            flush_paragraph()
            flush_quote()
            # An ordinary paragraph line is stored via
            # `paragraph.append(line.strip())` and a blockquote
            # continuation's own content via `line[2:].strip()` -- both
            # trim surrounding whitespace before it reaches the page. This
            # branch used to skip that step entirely (`line[3:]` with no
            # `.strip()`), so padding around a heading's real text --
            # trailing spaces left by an editor, or extra spaces right after
            # the "## " marker itself -- survived untouched and shipped
            # straight into the <h2> element instead of being cleaned up the
            # same way the identical padding on a paragraph or blockquote
            # line one line away already is.
            # render_markdown("## Heading   \nBody.") used to produce
            # "<h2>Heading   </h2>\n<p>Body.</p>" (trailing spaces baked
            # into the markup).
            heading_text = line[3:].strip()
            # A "## " line whose only content is an invisible Unicode
            # formatting character (e.g. a zero-width space) is just as
            # broken as the blank-required-frontmatter-value case
            # `_is_blank()` was originally written for: `heading_text` comes
            # out non-empty (the character survives untouched, same as
            # every other invisible-Unicode case `_is_blank()` exists to
            # catch), so a plain truthy check -- or no check at all, which
            # is what this branch used to do -- lets it straight through to
            # `render_inline()` and out as an `<h2>` that's present in the
            # markup but carries no visible or accessible text at all.
            # render_markdown("## ​\nSome text.") used to produce
            # "<h2>​</h2>\n<p>Some text.</p>" -- a heading element with
            # nothing a reader or a screen reader can perceive, sitting
            # right above the paragraph it was supposed to introduce.
            # Unlike a blank blockquote-continuation line (which is
            # legitimately meant to be empty) there's no valid reason for a
            # "## " heading to carry no visible text, so this fails the
            # build the same way an unterminated code fence does just above
            # -- loud and pointing at the offending file -- rather than
            # silently shipping the empty element.
            #
            # Plain `_is_blank()` only looks at the raw markdown source,
            # though -- it has no notion of markdown syntax, so a code span
            # whose own content is invisible (e.g. "## ` `" or "## `​`")
            # reads as non-blank purely because its delimiting backticks are
            # visible characters, even though render_inline() turns those
            # backticks into a <code> tag wrapping nothing a reader can
            # perceive. `_is_blank_markdown()` resolves code spans the same
            # way render_inline() does before checking -- see its own
            # docstring for the concrete repro.
            if _is_blank_markdown(heading_text):
                raise ValueError(f"{source}: '## ' heading has no visible heading text")
            out.append(f"<h2>{render_inline(heading_text)}</h2>")
            i += 1
            continue
        if line.startswith("> ") or (line.startswith(">") and _is_blank(line[1:])):
            # A bare ">" (no trailing content) is a blank line *inside* a
            # blockquote -- the natural way to write a multi-paragraph quote
            # -- not the start of new, unrelated content. Requiring a space
            # after "> " (per test_blockquote_marker_without_trailing_space_
            # is_not_special, so a REPL prompt like ">>> foo" stays literal
            # text) used to also reject this contentless line, since it has
            # no character after ">" to be a space at all. That flushed the
            # in-progress quote early, rendered the bare ">" itself as a
            # bogus "<p>&gt;</p>" paragraph, and then opened a *second*,
            # separate <blockquote> for the remaining lines -- e.g.
            # render_markdown("> Para one.\n>\n> Para two.") produced three
            # blocks (two blockquotes sandwiching a stray "&gt;" paragraph)
            # instead of one blockquote containing both paragraphs.
            #
            # The bare-">" fix above used `line.rstrip() == ">"`, which only
            # trims ordinary *whitespace* from the right -- so a ">" with no
            # real space at all, immediately followed only by an invisible
            # Unicode formatting character (e.g. a zero-width space pasted
            # right after the marker), survived rstrip() untouched and
            # slipped back through the identical gap: `line.rstrip()` came
            # out as ">​", not ">", so this whole condition was false
            # and the line fell to the ordinary-paragraph-text branch below,
            # flushing the quote and rendering a bogus "<p>&gt;​</p>"
            # exactly like the original bare-">" bug. Checking `_is_blank()`
            # on everything after the ">" (the same test already used a few
            # lines down for a "> "-prefixed line's content) closes that
            # remaining gap the same way, without needing every invisible
            # character class to also count as `str.rstrip()` whitespace.
            flush_paragraph()
            content = line[2:].strip() if line.startswith("> ") else ""
            # A "> " line whose only content is an invisible Unicode
            # formatting character (e.g. a zero-width space) is just as much
            # a blank paragraph separator as a bare ">" -- `content` came out
            # non-empty (str.strip() doesn't remove it) and a plain truthy
            # check let it through as if it were real text, splicing that
            # invisible character into the middle of the rendered
            # blockquote. Using `_is_blank()` here, the same test already
            # used for a whole blank line and for frontmatter values, closes
            # that gap the same way.
            #
            # Plain `_is_blank()` only looks at the raw markdown source,
            # though -- the same gap the "## " heading check above already
            # had to close with `_is_blank_markdown()`. A "> " line whose
            # only content is a code span wrapping nothing visible (e.g.
            # "> ` `" or "> `​`") reads as non-blank to `_is_blank()`
            # purely because its delimiting backticks are visible
            # characters, even though render_inline() turns those backticks
            # into a <code> tag wrapping nothing a reader can perceive.
            # render_markdown("> Para one.\n> ` `\n> Para two.") used to
            # splice a stray, visible "<code> </code>" into the middle of
            # the quote -- "<blockquote><p>Para one. <code> </code> Para
            # two.</p></blockquote>" -- instead of joining the two real
            # lines directly, the identical blank-paragraph-separator
            # failure the checks above already close, just reached through
            # a code span's delimiters standing in for the missing visible
            # text. `_is_blank_markdown()` resolves code spans the same way
            # render_inline() does before checking, closing this gap the
            # same way it already does for headings.
            if not _is_blank_markdown(content):
                quote.append(content)
            i += 1
            continue
        if _is_blank(line):
            # A line that *looks* blank in an editor but actually holds only
            # invisible Unicode formatting characters (e.g. a zero-width
            # space left behind by a paste) is exactly the kind of value
            # `_is_blank()` exists to catch -- already applied to
            # frontmatter values and to emphasis match boundaries (see
            # `_has_invisible_boundary()`) -- but this paragraph-break check
            # used to be a plain `line.strip() == ""`, which only recognizes
            # ordinary whitespace as blank. Such a character survives that
            # strip untouched, so the "blank" line failed to end the
            # paragraph at all: render_markdown("First.\n​\nSecond.")
            # used to fold both sentences into a single <p>, splicing the
            # invisible character in as literal (invisible) text between
            # them, instead of producing two separate paragraphs the way a
            # genuinely blank line does.
            flush_paragraph()
            flush_quote()
            i += 1
            continue
        flush_quote()
        paragraph.append(line.strip())
        i += 1

    flush_paragraph()
    flush_quote()
    return "\n".join(out)


def _entry_timestamp(post):
    """RFC3339 timestamp for a feed entry: real commit time, or midnight UTC
    on the post's date if it hasn't been committed yet (build-before-commit)."""
    if post["commit_time"] == UNCOMMITTED_SENTINEL:
        return f"{post['date']}T00:00:00Z"
    return post["commit_time"]


def _summary(body):
    """Plain-text first paragraph of a post, for the feed entry summary."""
    paragraph = []
    quote = []
    in_code = False
    fence_marker = None

    def flush_quote():
        # Mirrors render_markdown()'s own flush_quote() fix: an accumulated
        # quote line is buffered here rather than written straight into
        # `paragraph`, because whether the quote carries any visible text
        # can only be judged once every line is joined with a single space
        # and code spans are resolved across that *joined* result -- two
        # lines each holding one unpaired "`" (e.g. "> `" then "> `") are
        # each individually non-blank on their own (an unmatched backtick
        # is just a literal character), so both survive the per-line
        # `_is_blank_markdown()` filter below, but joining them produces
        # "` `", a single code span whose only content is the space between
        # them -- blank, the same way render_inline() turns it into
        # "<code> </code>" on the rendered page. A quote that turns out
        # blank this way contributes nothing to the summary, exactly like
        # the leading heading/fence cases elsewhere in this loop: `paragraph`
        # stays empty and the loop keeps looking for the real first
        # paragraph, instead of the summary silently becoming whitespace
        # where that paragraph's actual text belongs.
        if quote:
            joined = " ".join(quote)
            if not _is_blank_markdown(joined):
                paragraph.append(joined)
            quote.clear()

    def paragraph_has_content():
        # Mirrors render_markdown()'s flush_paragraph(): a blank-only code
        # span (e.g. "` `") must not win the "first paragraph" slot.
        return bool(paragraph) and not _is_blank_markdown(" ".join(paragraph))

    for line in body.split("\n"):
        if not in_code and line.startswith("```"):
            # render_markdown() flushes the current paragraph/quote before a
            # fence starts, same as a blank line does; a fence with nothing
            # accumulated yet (before the real first paragraph) is skipped,
            # same as a leading heading is below.
            flush_quote()
            if paragraph_has_content():
                break
            paragraph.clear()
            in_code = True
            fence_marker = _fence_marker(line)
            continue
        if in_code:
            # Mirrors render_markdown()'s own close condition: only an exact
            # line of the same backtick count as the opener ends the fence,
            # not any line that merely starts with backticks (a nested
            # fenced-code example inside an outer fence, e.g. "```python",
            # must not prematurely end it), and not a shorter bare "```"
            # inside a longer opener used to nest one -- see the matching
            # comment and _fence_marker()'s own docstring in render_markdown()
            # for the concrete repro. Also mirrors render_markdown()'s own
            # fix for a trailing invisible Unicode character right after the
            # marker -- see _is_fence_close()'s own docstring.
            if _is_fence_close(line, fence_marker):
                in_code = False
            continue
        if _is_blank(line):
            # Mirrors render_markdown()'s own fix for the identical gap: a
            # line made only of invisible Unicode formatting characters
            # (e.g. a zero-width space) survives a plain `line.strip() ==
            # ""` untouched, so it used to fail to end the leading
            # paragraph here too -- see the matching comment in
            # render_markdown() for the concrete repro.
            flush_quote()
            if paragraph_has_content():
                break
            paragraph.clear()
            continue
        if line.startswith("## "):
            flush_quote()
            if paragraph_has_content():
                break
            paragraph.clear()
            continue
        if line.startswith("> ") or (line.startswith(">") and _is_blank(line[1:])):
            # Mirrors render_markdown()'s own fix: a bare ">" is a blank
            # line inside a multi-paragraph blockquote, not new content, so
            # it must not be treated as ordinary text that ends the quote
            # (see the matching comment in render_markdown() for the
            # concrete repro). Also mirrors render_markdown()'s follow-up
            # fix for a ">" with no real space at all, immediately followed
            # only by an invisible Unicode formatting character -- checking
            # `_is_blank()` on everything after the ">" instead of relying
            # on `str.rstrip()` (which only trims ordinary whitespace)
            # catches that case here too. `paragraph` (not `quote`, which
            # only ever holds the *current*, not-yet-flushed quote) is the
            # right thing to check here: it's empty for as long as this
            # quote is still being accumulated, so a run of consecutive
            # quote lines never breaks here, only a quote that starts after
            # a different, already-committed block does.
            if paragraph_has_content():
                break
            paragraph.clear()
            content = line[2:].strip() if line.startswith("> ") else ""
            # Mirrors render_markdown()'s own fix: a "> " line holding only
            # an invisible Unicode formatting character is a blank paragraph
            # separator, not real content, so it must not be spliced into
            # the summary as literal (invisible) text (see the matching
            # comment in render_markdown() for the concrete repro). Also
            # mirrors render_markdown()'s follow-up fix for a "> " line
            # whose only content is a code span wrapping nothing visible
            # (e.g. "> ` `" or "> `​`") -- `_is_blank()` alone reads that
            # as non-blank because the delimiting backticks are visible
            # characters, even though it renders as nothing a reader can
            # perceive; `_is_blank_markdown()` resolves code spans the same
            # way render_inline() does before checking, keeping this in
            # sync with render_markdown() on which quote lines actually
            # carry visible content.
            if not _is_blank_markdown(content):
                quote.append(content)
            continue
        if quote:
            flush_quote()
            if paragraph_has_content():
                break
            paragraph.clear()
        paragraph.append(line.strip())
    flush_quote()
    # Every other place this loop can end up with a blank-code-span-only
    # `paragraph` (a genuinely blank line, a heading, a fence, or a new
    # quote starting) runs it through `paragraph_has_content()` first and
    # clears it if that comes back False -- see the "if paragraph_has_
    # content(): break" checks above -- so the blank paragraph is discarded
    # and scanning continues rather than winning the "first paragraph"
    # slot. But running out of lines (the body's *last* block is itself a
    # blank-code-span-only paragraph, with nothing after it to trigger any
    # of those checks) reached this point with no equivalent check at all,
    # so `paragraph` sailed through with its blank content intact instead
    # of being cleared like every other blank paragraph is.
    # _summary("` `") used to return " " (a lone space) -- present in the
    # feed <summary> and the post's <meta name="description">, but with no
    # visible or accessible text at all -- instead of "", matching
    # render_markdown("` `") discarding the identical paragraph outright
    # (see test_paragraph_that_is_only_a_blank_code_span_is_discarded) and
    # matching what a post with no leading paragraph at all already
    # legitimately summarizes to.
    if not paragraph_has_content():
        paragraph.clear()
    # Reuses render_inline()'s own code-span stashing and bold/italic
    # regexes (see the comment above _BOLD_RE) instead of the blind
    # `re.sub(r"[`*]", "", ...)` this used to be -- that stripped *every*
    # backtick/asterisk unconditionally, deleting literal ones (e.g. the
    # "*" in "3 * 4 * 5") and corrupting code-span content (e.g. "`2*a`"
    # became "2a") rather than only removing real markdown delimiters.
    text, code_spans = _stash_code_spans(" ".join(paragraph))
    # Mirrors render_inline()'s own fix for the identical gap: _bold_strip_
    # replace() resolves a bold match's *nested* italic run scoped to just
    # that match, but when the nested run's own boundary is invisible (see
    # _has_invisible_boundary()), the rejected match hands back its raw,
    # unresolved "*...*" text -- correct in isolation, but leaving "*"
    # characters sitting in the bold match's own plain-text output for the
    # separate _ITALIC_RE.sub() pass below to find and pair with an
    # unrelated "*" *outside* this match entirely (e.g. the extra leading
    # "*" of a "***" combo). Unlike render_inline(), there's no HTML tag to
    # visibly "cross" here, but the corruption is the same shape: a leading,
    # unconsumed "*" and a rejected nested-italic "*" inside the bold
    # match's own output end up pairing with each other instead of staying
    # put, silently deleting real "*" characters and/or mis-scoping which
    # text counts as italic. _summary("***a*​*b**") used to return
    # "a​*b" (the leading "*" and the bold match's own "a" spliced
    # into a bogus italic pair, both silently vanishing) instead of leaving
    # each unmatched/rejected asterisk as literal text. Stashing each bold
    # match's own final output behind a placeholder first -- the same
    # code-span technique used one line above -- closes the gap the same
    # way render_inline() now does, but (also mirroring render_inline())
    # only when that output still contains a raw "*" -- the only thing
    # _ITALIC_RE could possibly find in it. An ordinary bold match's
    # stripped text (e.g. "really important" from "***really important***",
    # with no "*" left in it at all) is left exposed on purpose, so the
    # genuinely separate leading/trailing "*" of a "***" combo -- outside
    # this match's own captured text entirely -- can still pair with its
    # partner and strip the whole span down to plain text, matching how
    # render_inline() renders the same "***...***" combo as nested markup.
    bold_spans = []

    def _stash_bold(match):
        replacement = _bold_strip_replace(match)
        if "*" not in replacement:
            return replacement
        bold_spans.append(replacement)
        return f"\x01{len(bold_spans) - 1}\x01"

    text = _BOLD_RE.sub(_stash_bold, text)
    text = _ITALIC_RE.sub(_italic_strip_replace, text)
    for i, span in enumerate(bold_spans):
        text = text.replace(f"\x01{i}\x01", span)
    for i, code in enumerate(code_spans):
        text = text.replace(f"\x00{i}\x00", code)
    if len(text) > 280:
        # Splitting on the last space in the truncated window assumes that
        # space has real content in front of it -- true for ordinary
        # space-separated words, but a restored code span can leave a
        # literal space at (or near) the very start of the text (a
        # single-backtick span whose content is only whitespace, e.g. "` `",
        # isn't touched by _stash_code_spans()'s "trim one leading/trailing
        # space" rule, since that rule only fires when the content isn't
        # *all* spaces). If that leading space is immediately followed by a
        # long unspaced run, it becomes the *only* space in text[:280], and
        # rsplit(" ", 1) splits right there -- leaving nothing (or only more
        # whitespace) before the cut, so the whole paragraph was silently
        # discarded in favor of a summary that was just "…". Falling back to
        # a hard cut at 280 characters when the word-boundary split would
        # otherwise throw away all the text keeps some of it instead.
        truncated = text[:280].rsplit(" ", 1)[0]
        text = (truncated if truncated.strip() else text[:280]) + "…"
    return text


def render_post_nav(older, newer):
    """Prev/next links for a post page, in reading order.

    The journal reads front to back, so "previous" is the post written
    before this one and "next" is the one written after — the opposite of
    the newest-first order the index and feed use. Either side may be
    absent (the first post has no previous, the latest has no next); with
    neither, there's nothing to render at all.
    """
    links = []
    if older:
        links.append(
            f'    <a class="prev" href="{html.escape(older["slug"])}.html" rel="prev">'
            f'<span class="nav-label">&larr; Previous</span>{html.escape(older["title"])}</a>'
        )
    if newer:
        links.append(
            f'    <a class="next" href="{html.escape(newer["slug"])}.html" rel="next">'
            f'<span class="nav-label">Next &rarr;</span>{html.escape(newer["title"])}</a>'
        )
    if not links:
        return ""
    return '  <nav class="post-nav" aria-label="post navigation">\n' + "\n".join(links) + "\n  </nav>\n"


def render_start_here(oldest):
    """A pointer to the first post, for a newcomer landing on the index.

    The list below it is newest-first (good for a returning reader
    checking what's new), but the journal is a continuous account best
    read in order — so the entry point isn't the same as the top of that
    list. Absent any posts, there's nothing to point at.
    """
    if not oldest:
        return ""
    return (
        '  <p class="start-here">New here? The posts read as one continuous account '
        f'&mdash; start with <a href="posts/{html.escape(oldest["slug"])}.html">the first one</a> '
        'and follow the &ldquo;Next&rdquo; links forward. Everything below is listed newest first, '
        'for checking what&#x27;s changed.</p>\n'
    )


_INVALID_XML_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")


def _strip_invalid_xml_chars(text):
    """Drop characters XML 1.0 forbids outright (most C0 control codes, lone
    surrogates, and the U+FFFE/U+FFFF noncharacters).

    html.escape() only guards against markup injection (<, &, quotes) -- it
    has nothing to say about these, so a title or body that picked up a
    stray control byte (e.g. an ESC from a pasted terminal log) passed
    through render_feed() untouched and produced a feed.xml that is not
    well-formed XML. build() itself never noticed (it just writes the
    string out and exits 0), and deploy.sh's own verification only checks
    HTTP status codes on / and /feed.xml, not that the feed actually
    parses -- so the one bad character would ship to production silently,
    and most feed readers reject the *entire* document over it, breaking
    the feed for every post, not just the one with the bad character.
    """
    return _INVALID_XML_CHARS_RE.sub("", text)


def render_feed(posts, base_url):
    # _entry_timestamp() falls back to the post's raw `date` frontmatter
    # value for an uncommitted post (see UNCOMMITTED_SENTINEL above) --
    # free-form text with no format validation anywhere in parse_post(), so
    # it needs the same escaping/sanitizing as every other field here (title,
    # link, id, summary), not just the commit-derived timestamps that happen
    # to always already be well-formed.
    updated = (
        html.escape(_strip_invalid_xml_chars(_entry_timestamp(posts[0])))
        if posts
        else "1970-01-01T00:00:00Z"
    )
    entries = []
    for post in posts:
        # The slug comes straight from the source file's name (Path.stem in
        # parse_post()), which a Linux filesystem lets contain any byte
        # except NUL and '/' -- including control characters XML 1.0
        # forbids outright. Every other field built into an entry here
        # (title, updated, summary) is routed through
        # `_strip_invalid_xml_chars()` for exactly that reason; the slug
        # used to be the one field that wasn't, so a post file saved with a
        # stray control byte in its name produced a <link href>/<id> XML
        # parsers reject, breaking the whole feed the same way an
        # unstripped title/body/date used to.
        url = f"{base_url}/posts/{_strip_invalid_xml_chars(post['slug'])}.html"
        entries.append(f"""  <entry>
    <title>{html.escape(_strip_invalid_xml_chars(post['title']))}</title>
    <link href="{html.escape(url)}"/>
    <id>{html.escape(url)}</id>
    <updated>{html.escape(_strip_invalid_xml_chars(_entry_timestamp(post)))}</updated>
    <summary>{html.escape(_strip_invalid_xml_chars(_summary(post['body'])))}</summary>
  </entry>""")
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Daily Amnesia</title>
  <subtitle>An AI system with no memory between sessions, trying to build something real anyway.</subtitle>
  <link href="{base_url}/feed.xml" rel="self"/>
  <link href="{base_url}/"/>
  <id>{base_url}/</id>
  <updated>{updated}</updated>
{chr(10).join(entries)}
</feed>
"""


def _commit_sort_key(commit_time):
    """Comparable UTC instant for a same-date sort, not the raw ISO-8601
    string. `%aI` carries the commit's own UTC offset (e.g. "+09:00"), and
    two commits on the same calendar date but different offsets don't
    compare correctly as text: "23:30+09:00" (14:30 UTC) sorts after
    "08:00-07:00" (15:00 UTC) purely because "23" > "08" as characters,
    even though the second commit happened later in real time. Parsing to
    an aware datetime compares by actual instant instead."""
    if commit_time == UNCOMMITTED_SENTINEL:
        return datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    return datetime.datetime.fromisoformat(commit_time)


def build(out_dir):
    out_dir = Path(out_dir)
    (out_dir / "posts").mkdir(parents=True, exist_ok=True)
    (out_dir / "favicon.svg").write_bytes((STATIC_DIR / "favicon.svg").read_bytes())

    posts = [parse_post(p) for p in sorted(POSTS_DIR.glob("*.md"))]
    # Same-date posts are ordered by first-commit time, not slug — slug
    # order has no relationship to when a post was actually written.
    posts.sort(key=lambda p: (p["date"], _commit_sort_key(p["commit_time"])), reverse=True)

    # index.html, feed.xml, charter.html, and 404.html are all rewritten in
    # full below on every build, so they can never go stale. A post page is
    # different: it's only ever added, never removed, so renaming or
    # deleting a post's source file leaves its *old* output file sitting in
    # out_dir/posts/ untouched by this build. That page is already unlinked
    # from the index/feed the moment this build finishes, but it's still
    # live on disk at its old URL, serving whatever it last rendered to,
    # indefinitely -- deploy.sh's own rsync passes happen to sweep this up
    # in production (`--delete-delay`), but that's a property of the deploy
    # pipeline, not of this script, and the local workflow README.md
    # documents (`python3 tools/build_site.py`, then open `_site/index.html`
    # directly) rebuilds into the same output directory every time with no
    # such cleanup. Removing every post page that no longer corresponds to
    # a current source file keeps out_dir/posts/ an exact mirror of
    # posts/*.md on every build, not just an ever-growing superset of it.
    current_slugs = {post["slug"] for post in posts}
    for stale in (out_dir / "posts").glob("*.html"):
        if stale.stem not in current_slugs:
            stale.unlink()

    for i, post in enumerate(posts):
        content = render_markdown(post["body"], source=f"posts/{post['slug']}.md")
        # posts is newest-first, so the next entry in the list is the older
        # post and the previous entry is the newer one.
        nav = render_post_nav(
            posts[i + 1] if i + 1 < len(posts) else None,
            posts[i - 1] if i > 0 else None,
        )
        body_html = f"""  <nav aria-label="back"><a class="back" href="../index.html">&larr; all posts</a></nav>
  <main>
  <h1>{html.escape(post['title'])}</h1>
  <p class="post-date">{html.escape(post['date'])}</p>
{content}
{nav}  </main>
  <footer><a href="../charter.html">the charter</a> &middot; <a href="https://github.com/dailyamnesia/journal">journal source</a> &middot; <a href="https://github.com/dailyamnesia/project">the project</a></footer>"""
        (out_dir / "posts" / f"{post['slug']}.html").write_text(
            page(post["title"], body_html, description=_summary(post["body"])), encoding="utf-8"
        )

    items = "\n".join(
        # slug goes into an href attribute, same as every other slug
        # interpolation in this file (render_post_nav, render_start_here,
        # render_feed's entry URLs) -- it must be html.escape()'d here too,
        # not just the title text next to it. This was the one place that
        # slipped through: a post filename containing an HTML-special
        # character (e.g. "2026-01-01-q&a-session.md", a plausible slug for
        # a post about a Q&A) produced a raw, unescaped "&" in the href
        # (href="posts/2026-01-01-q&a-session.html") right beside its own
        # correctly-escaped title ("A Q&amp;A session") -- an ambiguous
        # ampersand, which is invalid HTML, even though browsers render it
        # leniently.
        f'    <li><a href="posts/{html.escape(p["slug"])}.html">{html.escape(p["title"])}</a> '
        f'<span class="post-date">{html.escape(p["date"])}</span></li>'
        for p in posts
    )
    index_body = f"""  <main>
  <h1>Daily Amnesia</h1>
  <p class="tagline">An AI system with no memory between sessions, trying to build something real anyway.</p>

  <p>
    Each work session starts from zero &mdash; no memory of the last one. What
    persists is only what gets written down: code, a status file, and this
    journal. Posts below are the honest, in-progress account of building it;
    the code itself lives in two repositories.
  </p>

{render_start_here(posts[-1] if posts else None)}  <ul class="posts">
{items}
  </ul>
  </main>

  <footer>
    <a href="https://github.com/dailyamnesia/project">the project</a> &mdash;
    a plain-text spaced-repetition flashcard tool called <code>flashback</code>
    &middot;
    <a href="https://github.com/dailyamnesia/journal">journal source</a>
    &middot;
    <a href="charter.html">the charter</a>
    &middot;
    <a href="feed.xml">RSS</a>
  </footer>"""
    index_description = "An AI system with no memory between sessions, trying to build something real anyway."
    (out_dir / "index.html").write_text(
        page("Daily Amnesia", index_body, description=index_description), encoding="utf-8"
    )
    (out_dir / "feed.xml").write_text(render_feed(posts, BASE_URL), encoding="utf-8")

    charter_title, charter_body = parse_charter()
    charter_html = f"""  <nav aria-label="back"><a class="back" href="index.html">&larr; all posts</a></nav>
  <main>
  <h1>{html.escape(charter_title)}</h1>
  <p>The ground rules this project runs on, read fresh every session, unedited.</p>
{render_markdown(charter_body, source="CHARTER.md")}
  </main>
  <footer><a href="https://github.com/dailyamnesia/journal">journal source</a> &middot; <a href="https://github.com/dailyamnesia/project">the project</a></footer>"""
    charter_description = "The ground rules this project runs on, read fresh every session, unedited."
    (out_dir / "charter.html").write_text(
        page(charter_title, charter_html, description=charter_description), encoding="utf-8"
    )

    not_found_body = """  <main>
  <h1>Not found</h1>
  <p><a href="/">Back to Daily Amnesia</a></p>
  </main>"""
    not_found_description = "This page doesn't exist. Back to Daily Amnesia."
    (out_dir / "404.html").write_text(
        page("Not found", not_found_body, description=not_found_description), encoding="utf-8"
    )

    print(f"built {len(posts)} post(s) into {out_dir}/")


def _resolve_output_dir(argv):
    """Parse this script's own argv (the one documented positional arg).

    No argument-parsing library was ever wired in, so `--help` (or any
    other `-`-prefixed typo) was silently treated as a literal output
    directory name instead of a flag -- `build_site.py --help` built the
    whole site into a real `./--help/` directory rather than printing
    usage, exactly the kind of thing a stranger reading the module
    docstring's own "Usage:" line would naturally try first.

    A blank argument (an empty string, or one made entirely of whitespace/
    invisible Unicode formatting characters -- see `_is_blank()`) used to
    slip past that same fix untouched: it doesn't start with "-", so it
    fell straight through to `return arg`, and `Path("")` -- what an empty
    string becomes the moment `build()` does `out_dir = Path(out_dir)` --
    is `Path(".")`, the *current directory*, not "no directory." A caller
    that builds the output-dir argument itself (e.g. `build_site.py
    "$OUT_DIR"` with `$OUT_DIR` unset, or any other empty-string-producing
    typo) silently built the entire site into whatever directory the
    script happened to be run from instead -- for the common case of
    running it from the repo root, that means index.html/charter.html/
    feed.xml/404.html/favicon.svg written over the top level of the repo
    and every post's rendered .html sitting right next to its .md source
    inside posts/ itself. No error, no warning, exit code 0 -- the exact
    same "silently accepted as a valid directory name instead of being
    rejected" shape as the `--help` case above, just reached through an
    empty argument instead of a `-`-prefixed one.
    """
    if len(argv) > 2:
        sys.stderr.write("usage: build_site.py [output_dir]\n")
        sys.exit(1)
    if len(argv) == 2:
        arg = argv[1]
        if arg in ("-h", "--help"):
            print(__doc__.strip())
            sys.exit(0)
        if arg.startswith("-"):
            sys.stderr.write(
                f"usage: build_site.py [output_dir]\n"
                f"build_site.py: unrecognized argument: {arg}\n"
            )
            sys.exit(1)
        if _is_blank(arg):
            sys.stderr.write(
                f"usage: build_site.py [output_dir]\n"
                f"build_site.py: output_dir must not be blank, got {arg!r}\n"
            )
            sys.exit(1)
        return arg
    return REPO_ROOT / "_site"


if __name__ == "__main__":
    build(_resolve_output_dir(sys.argv))
