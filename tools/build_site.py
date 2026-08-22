#!/usr/bin/env python3
"""Render posts/*.md into a static HTML site.

Stdlib only, no dependencies. Reads the small, deliberately limited
subset of markdown actually used in this journal (## headings, *italic*,
**bold**, `inline code`, fenced ``` code blocks, > blockquotes,
paragraphs) and writes index.html + posts/<slug>.html into an output
directory.

Usage: build_site.py [output_dir]   (default: _site)
"""
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
    meta_description = (
        f'<meta name="description" content="{html.escape(description)}">\n' if description else ""
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
            ["git", "log", "--follow", "--format=%aI", "--", str(path)],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        lines = result.stdout.strip().splitlines()
        return lines[-1] if lines else UNCOMMITTED_SENTINEL
    except (subprocess.CalledProcessError, FileNotFoundError):
        return UNCOMMITTED_SENTINEL


def parse_post(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: missing frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"{path}: frontmatter opened with '---' but never closed")
    frontmatter, body = text[4:end], text[end + 5:]
    meta = {}
    for line in frontmatter.splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')
    for required in ("title", "date"):
        if required not in meta:
            raise ValueError(f"{path}: frontmatter is missing required key {required!r}")
    slug = path.stem
    return {
        "slug": slug,
        "title": meta["title"],
        "date": meta["date"],
        "commit_time": _first_commit_time(path),
        "body": body.strip("\n"),
    }


def parse_charter(path=CHARTER_PATH):
    """CHARTER.md's own leading `# Title` line becomes the page title (the
    site's markdown subset only renders `##`-and-deeper as headings), the
    rest is rendered like a post body."""
    text = path.read_text(encoding="utf-8")
    title_line, _, body = text.partition("\n")
    if not title_line.startswith("# "):
        raise ValueError(f"{path}: expected a leading '# Title' line")
    return title_line[2:].strip(), body.strip("\n")


def render_inline(text):
    text = html.escape(text)

    # Code spans are stashed out and restored after bold/italic run, so
    # e.g. `*not italic*` isn't itself reinterpreted as markdown.
    code_spans = []

    def stash(match):
        code_spans.append(match.group(1))
        return f"\x00{len(code_spans) - 1}\x00"

    text = re.sub(r"`([^`]+)`", stash, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
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
            while i < len(lines) and not lines[i].startswith("```"):
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
        if line.startswith("> "):
            flush_paragraph()
            quote.append(line[2:].strip())
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
        if line.startswith("```"):
            # render_markdown() flushes the current paragraph/quote before a
            # fence starts, same as a blank line does; a fence with nothing
            # accumulated yet (before the real first paragraph) is skipped,
            # same as a leading heading is below.
            if paragraph:
                break
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.strip() == "":
            if paragraph:
                break
            continue
        if line.startswith("## "):
            if paragraph:
                break
            continue
        if line.startswith("> "):
            if paragraph and not quoting:
                break
            quoting = True
            paragraph.append(line[2:].strip())
            continue
        if paragraph and quoting:
            break
        quoting = False
        paragraph.append(line.strip())
    text = re.sub(r"[`*]", "", " ".join(paragraph))
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


def render_feed(posts, base_url):
    updated = _entry_timestamp(posts[0]) if posts else "1970-01-01T00:00:00Z"
    entries = []
    for post in posts:
        url = f"{base_url}/posts/{post['slug']}.html"
        entries.append(f"""  <entry>
    <title>{html.escape(post['title'])}</title>
    <link href="{html.escape(url)}"/>
    <id>{html.escape(url)}</id>
    <updated>{_entry_timestamp(post)}</updated>
    <summary>{html.escape(_summary(post['body']))}</summary>
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
        f'    <li><a href="posts/{p["slug"]}.html">{html.escape(p["title"])}</a> '
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
    (out_dir / "404.html").write_text(page("Not found", not_found_body), encoding="utf-8")

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
