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


def _first_commit_time(path):
    """ISO timestamp of the post file's first commit, for ordering same-date posts.

    Falls back to a sentinel that sorts after every real timestamp, so an
    uncommitted (just-written) post still lands first, newest-first.
    """
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
        return lines[-1] if lines else UNCOMMITTED_SENTINEL
    except (subprocess.CalledProcessError, FileNotFoundError):
        return UNCOMMITTED_SENTINEL


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
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{path}: not valid UTF-8: {e}") from None
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
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
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
        meta[key.strip()] = value
    for required in ("title", "date"):
        # A key present but left blank (e.g. "date:" with nothing after it)
        # is just as broken as the key being absent -- `not in meta` alone
        # missed it, letting an empty string reach the rendered post and a
        # malformed feed <updated> timestamp instead of failing here. A
        # quoted value containing only whitespace (e.g. `title: "   "`) is
        # just as broken and is now caught the same way, since the parsing
        # loop above already strips it down to an empty string before it
        # ever reaches this check.
        if not meta.get(required):
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
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{path}: not valid UTF-8: {e}") from None
    title_line, _, body = text.partition("\n")
    if not title_line.startswith("# "):
        raise ValueError(f"{path}: expected a leading '# Title' line")
    # Same rule as parse_post() above: sanitize once at the source rather
    # than leave every HTML call site to apply it individually.
    return (
        _strip_invalid_xml_chars(title_line[2:].strip()),
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
_ITALIC_INNER = r"\*[^*\s](?:[^*]*[^*\s])?\*"
_BOLD_RE = re.compile(r"\*\*([^*\s](?:(?:[^*]|" + _ITALIC_INNER + r")*[^*\s])?)\*\*")
_ITALIC_RE = re.compile(r"\*([^*\s](?:[^*]*[^*\s])?)\*")


def _bold_replace(match):
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
    inner = _ITALIC_RE.sub(r"<em>\1</em>", match.group(1))
    return f"<strong>{inner}</strong>"


def render_inline(text):
    text = html.escape(text)

    # Code spans are stashed out and restored after bold/italic run, so
    # e.g. `*not italic*` isn't itself reinterpreted as markdown.
    text, code_spans = _stash_code_spans(text)
    text = _BOLD_RE.sub(_bold_replace, text)
    text = _ITALIC_RE.sub(r"<em>\1</em>", text)
    for i, code in enumerate(code_spans):
        text = text.replace(f"\x00{i}\x00", f"<code>{code}</code>")
    return text


def render_markdown(body, source="post"):
    lines = body.split("\n")
    out = []
    i = 0
    paragraph = []
    quote = []

    def flush_paragraph():
        if paragraph:
            out.append(f"<p>{render_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_quote():
        if quote:
            out.append(f"<blockquote><p>{render_inline(' '.join(quote))}</p></blockquote>")
            quote.clear()

    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush_paragraph()
            flush_quote()
            i += 1
            code_lines = []
            # Only an exact "```" line (no trailing info-string) closes the
            # fence -- the opening line is matched permissively (startswith,
            # so "```python"/"```bash" language tags work, as real posts in
            # this repo use extensively), but a content line that happens to
            # start with "```" too (e.g. a post demonstrating this renderer's
            # own fence syntax, wrapping a sample "```python ... ```" block
            # inside an outer fence) must not itself be mistaken for the
            # close. It used to be: any line starting with "```" closed the
            # fence, so render_markdown("```\n```python\nprint(1)\n```\n```")
            # -- an outer fence containing a literal nested fenced example --
            # closed on the inner "```python" line, leaving an empty code
            # block, dumping "print(1)" out as a bogus visible paragraph,
            # and opening a second, also-empty code block from the line
            # that was meant to be the inner block's own close.
            while i < len(lines) and lines[i].rstrip() != "```":
                code_lines.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ValueError(f"{source}: unterminated code fence (``` opened but never closed)")
            out.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
            i += 1
            continue
        if line.startswith("## "):
            flush_paragraph()
            flush_quote()
            out.append(f"<h2>{render_inline(line[3:])}</h2>")
            i += 1
            continue
        if line.startswith("> ") or line.rstrip() == ">":
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
            flush_paragraph()
            content = line[2:].strip() if line.startswith("> ") else ""
            if content:
                quote.append(content)
            i += 1
            continue
        if line.strip() == "":
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
    quoting = False
    in_code = False
    for line in body.split("\n"):
        if not in_code and line.startswith("```"):
            # render_markdown() flushes the current paragraph/quote before a
            # fence starts, same as a blank line does; a fence with nothing
            # accumulated yet (before the real first paragraph) is skipped,
            # same as a leading heading is below.
            if paragraph:
                break
            in_code = True
            continue
        if in_code:
            # Mirrors render_markdown()'s own close condition: only an exact
            # "```" line ends the fence, not any line that merely starts
            # with backticks (a nested fenced-code example inside an outer
            # fence, e.g. "```python", must not prematurely end it -- see
            # the matching comment in render_markdown() for the concrete
            # repro).
            if line.rstrip() == "```":
                in_code = False
            continue
        if line.strip() == "":
            if paragraph:
                break
            continue
        if line.startswith("## "):
            if paragraph:
                break
            continue
        if line.startswith("> ") or line.rstrip() == ">":
            # Mirrors render_markdown()'s own fix: a bare ">" is a blank
            # line inside a multi-paragraph blockquote, not new content, so
            # it must not be treated as ordinary text that ends the quote
            # (see the matching comment in render_markdown() for the
            # concrete repro).
            if paragraph and not quoting:
                break
            quoting = True
            content = line[2:].strip() if line.startswith("> ") else ""
            if content:
                paragraph.append(content)
            continue
        if paragraph and quoting:
            break
        quoting = False
        paragraph.append(line.strip())
    # Reuses render_inline()'s own code-span stashing and bold/italic
    # regexes (see the comment above _BOLD_RE) instead of the blind
    # `re.sub(r"[`*]", "", ...)` this used to be -- that stripped *every*
    # backtick/asterisk unconditionally, deleting literal ones (e.g. the
    # "*" in "3 * 4 * 5") and corrupting code-span content (e.g. "`2*a`"
    # became "2a") rather than only removing real markdown delimiters.
    text, code_spans = _stash_code_spans(" ".join(paragraph))
    text = _BOLD_RE.sub(r"\1", text)
    text = _ITALIC_RE.sub(r"\1", text)
    for i, code in enumerate(code_spans):
        text = text.replace(f"\x00{i}\x00", code)
    if len(text) > 280:
        text = text[:280].rsplit(" ", 1)[0] + "…"
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
        url = f"{base_url}/posts/{post['slug']}.html"
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


def build(out_dir):
    out_dir = Path(out_dir)
    (out_dir / "posts").mkdir(parents=True, exist_ok=True)
    (out_dir / "favicon.svg").write_bytes((STATIC_DIR / "favicon.svg").read_bytes())

    posts = [parse_post(p) for p in sorted(POSTS_DIR.glob("*.md"))]
    # Same-date posts are ordered by first-commit time, not slug — slug
    # order has no relationship to when a post was actually written.
    posts.sort(key=lambda p: (p["date"], p["commit_time"]), reverse=True)

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
        return arg
    return REPO_ROOT / "_site"


if __name__ == "__main__":
    build(_resolve_output_dir(sys.argv))
