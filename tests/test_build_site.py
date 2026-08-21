import re
import sys
import tempfile
import unittest
import xml.dom.minidom
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))
import build_site  # noqa: E402


class TestRenderInline(unittest.TestCase):
    def test_escapes_html(self):
        self.assertEqual(build_site.render_inline("<script>&"), "&lt;script&gt;&amp;")

    def test_code(self):
        self.assertEqual(
            build_site.render_inline("use `flashback sync`"),
            "use <code>flashback sync</code>",
        )

    def test_bold(self):
        self.assertEqual(build_site.render_inline("**important**"), "<strong>important</strong>")

    def test_italic(self):
        self.assertEqual(build_site.render_inline("*emphasis*"), "<em>emphasis</em>")

    def test_code_content_not_further_processed(self):
        self.assertEqual(build_site.render_inline("`**not bold**`"), "<code>**not bold**</code>")


class TestPage(unittest.TestCase):
    def test_no_description_by_default(self):
        out = build_site.page("Title", "<p>Body</p>")
        self.assertNotIn('name="description"', out)

    def test_description_rendered_and_escaped(self):
        out = build_site.page("Title", "<p>Body</p>", description='A "quoted" claim & more')
        self.assertIn(
            '<meta name="description" content="A &quot;quoted&quot; claim &amp; more">', out
        )


class TestRenderMarkdown(unittest.TestCase):
    def test_single_paragraph(self):
        self.assertEqual(build_site.render_markdown("Hello world."), "<p>Hello world.</p>")

    def test_multiline_paragraph_joins_with_space(self):
        self.assertEqual(
            build_site.render_markdown("Line one\nline two."), "<p>Line one line two.</p>"
        )

    def test_blank_line_separates_paragraphs(self):
        self.assertEqual(
            build_site.render_markdown("First.\n\nSecond."),
            "<p>First.</p>\n<p>Second.</p>",
        )

    def test_heading(self):
        self.assertEqual(build_site.render_markdown("## A Heading"), "<h2>A Heading</h2>")

    def test_fenced_code_block_not_inline_processed(self):
        body = "```\n*not italic* & <tag>\n```"
        self.assertEqual(
            build_site.render_markdown(body),
            "<pre><code>*not italic* &amp; &lt;tag&gt;</code></pre>",
        )

    def test_paragraph_then_code_block_then_paragraph(self):
        body = "Before.\n\n```\ncode\n```\n\nAfter."
        self.assertEqual(
            build_site.render_markdown(body),
            "<p>Before.</p>\n<pre><code>code</code></pre>\n<p>After.</p>",
        )

    def test_unterminated_code_fence_raises_instead_of_swallowing_rest(self):
        body = "Intro.\n\n```\ncode that never closes\n\n## looks like a heading, isn't\n"
        with self.assertRaises(ValueError) as ctx:
            build_site.render_markdown(body, source="posts/example.md")
        self.assertIn("posts/example.md", str(ctx.exception))
        self.assertIn("unterminated code fence", str(ctx.exception))

    def test_blockquote(self):
        self.assertEqual(
            build_site.render_markdown("> quoted line"),
            "<blockquote><p>quoted line</p></blockquote>",
        )

    def test_multiline_blockquote_joins_into_one_paragraph(self):
        body = "> line one\n> line two"
        self.assertEqual(
            build_site.render_markdown(body),
            "<blockquote><p>line one line two</p></blockquote>",
        )

    def test_blockquote_separated_from_surrounding_paragraphs(self):
        body = "Before.\n\n> quoted.\n\nAfter."
        self.assertEqual(
            build_site.render_markdown(body),
            "<p>Before.</p>\n<blockquote><p>quoted.</p></blockquote>\n<p>After.</p>",
        )

    def test_blockquote_marker_without_trailing_space_is_not_special(self):
        # A bare "&gt;" with no following space (e.g. a Python REPL prompt
        # like ">>>") is ordinary paragraph text, not a blockquote.
        self.assertEqual(build_site.render_markdown(">>> foo"), "<p>&gt;&gt;&gt; foo</p>")


class TestSummary(unittest.TestCase):
    def test_first_paragraph(self):
        body = "First paragraph.\n\nSecond paragraph."
        self.assertEqual(build_site._summary(body), "First paragraph.")

    def test_skips_leading_heading_and_code_fence(self):
        body = "## Heading\n\n```\ncode\n```\n\nActual first paragraph."
        self.assertEqual(build_site._summary(body), "Actual first paragraph.")

    def test_strips_backticks_and_asterisks(self):
        body = "Some `code` and **bold** and *italic* text."
        self.assertEqual(build_site._summary(body), "Some code and bold and italic text.")

    def test_truncates_long_paragraph_at_word_boundary(self):
        summary = build_site._summary("word " * 100)
        self.assertTrue(summary.endswith("…"))
        self.assertLessEqual(len(summary), 281)
        self.assertNotIn("  ", summary)

    def test_strips_blockquote_marker(self):
        # render_markdown() has treated "> " as a real blockquote since
        # session 67; _summary() re-parses the same raw markdown separately
        # (for <meta description> and the Atom feed) and needs the same rule,
        # or the literal "> " marker leaks into both.
        body = "> quoted line one\n> quoted line two\n\nReal paragraph after."
        self.assertEqual(build_site._summary(body), "quoted line one quoted line two")

    def test_blockquote_marker_without_trailing_space_is_not_stripped(self):
        # Matches render_markdown()'s own rule: a bare ">>>" with no space
        # isn't a blockquote, so it's left as ordinary paragraph text.
        self.assertEqual(build_site._summary(">>> foo"), ">>> foo")


class TestParsePost(unittest.TestCase):
    def test_parses_frontmatter_and_body(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "2026-01-01-test-post.md"
            path.write_text(
                '---\ntitle: "A Test Post"\ndate: 2026-01-01\n---\nBody text here.\n',
                encoding="utf-8",
            )
            post = build_site.parse_post(path)
            self.assertEqual(post["slug"], "2026-01-01-test-post")
            self.assertEqual(post["title"], "A Test Post")
            self.assertEqual(post["date"], "2026-01-01")
            self.assertEqual(post["body"], "Body text here.")

    def test_missing_frontmatter_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text("no frontmatter here\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_site.parse_post(path)

    def test_unterminated_frontmatter_names_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text('---\ntitle: "Never closes"\ndate: 2026-01-01\n', encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("never closed", str(ctx.exception))

    def test_missing_required_key_names_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text('---\ndate: 2026-01-01\n---\nBody with no title.\n', encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("title", str(ctx.exception))

    def test_uncommitted_file_gets_sentinel_commit_time(self):
        # A file outside this repo's worktree can't be resolved by `git log`
        # (exactly what's true of a just-written, not-yet-committed post at
        # build time) -- _first_commit_time should fall back, not raise.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "2026-01-01-uncommitted.md"
            path.write_text(
                "---\ntitle: Uncommitted\ndate: 2026-01-01\n---\nBody.\n",
                encoding="utf-8",
            )
            post = build_site.parse_post(path)
            self.assertEqual(post["commit_time"], build_site.UNCOMMITTED_SENTINEL)


class TestRenderFeed(unittest.TestCase):
    def _post(self, **overrides):
        base = {
            "slug": "example-post",
            "title": "Example Post",
            "date": "2026-01-01",
            "commit_time": "2026-01-01T12:00:00Z",
            "body": "Some body text.",
        }
        base.update(overrides)
        return base

    def test_entry_fields(self):
        feed = build_site.render_feed([self._post()], "https://example.test")
        self.assertIn("<title>Example Post</title>", feed)
        self.assertIn('<link href="https://example.test/posts/example-post.html"/>', feed)
        self.assertIn("<updated>2026-01-01T12:00:00Z</updated>", feed)

    def test_escapes_title(self):
        feed = build_site.render_feed([self._post(title="A & B <em>")], "https://example.test")
        self.assertIn("A &amp; B &lt;em&gt;", feed)
        self.assertNotIn("<em>", feed)

    def test_uncommitted_post_uses_date_midnight(self):
        feed = build_site.render_feed(
            [self._post(commit_time=build_site.UNCOMMITTED_SENTINEL)], "https://example.test"
        )
        self.assertIn("<updated>2026-01-01T00:00:00Z</updated>", feed)

    def test_valid_xml(self):
        feed = build_site.render_feed(
            [self._post(), self._post(slug="two", title="Two")], "https://example.test"
        )
        xml.dom.minidom.parseString(feed)  # raises if malformed

    def test_empty_posts_list_still_valid_xml(self):
        feed = build_site.render_feed([], "https://example.test")
        xml.dom.minidom.parseString(feed)


class TestParseCharter(unittest.TestCase):
    def test_parses_title_and_body(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "CHARTER.md"
            path.write_text("# Charter\n\nRule one.\n\n## Goal\n\nRule two.\n", encoding="utf-8")
            title, body = build_site.parse_charter(path)
            self.assertEqual(title, "Charter")
            self.assertEqual(body, "Rule one.\n\n## Goal\n\nRule two.")

    def test_missing_title_line_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "CHARTER.md"
            path.write_text("Rule one.\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                build_site.parse_charter(path)

    def test_actual_charter_parses(self):
        # The real CHARTER.md this project runs on should render cleanly
        # through the site's own (deliberately limited) markdown subset.
        title, body = build_site.parse_charter()
        self.assertEqual(title, "Charter")
        html_out = build_site.render_markdown(body)
        self.assertIn("<h2>Goal</h2>", html_out)
        self.assertIn("<h2>Ownership</h2>", html_out)


class TestBuildOrdersSameDatePostsByCommitTime(unittest.TestCase):
    """Regression test for the session-7 bug: same-date posts were once
    ordered by slug text (accidentally matched write order for the first
    six posts, then broke on the seventh). Order must track when each post
    was actually written, not its filename."""

    def test_actual_posts_ordered_chronologically(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            index = (Path(d) / "index.html").read_text(encoding="utf-8")

        # newest-first, per each file's real first-commit timestamp (checked
        # directly against `git log --follow` for these five same-date posts)
        aug9_posts = [
            "something-to-follow",
            "nothing-broke",
            "the-site-reads-itself-now",
            "someone-read-it",
            "a-quiet-session",
        ]
        positions = [index.index(f"posts/2026-08-09-{slug}.html") for slug in aug9_posts]
        self.assertEqual(positions, sorted(positions))


class TestBuildIncludesMetaDescriptions(unittest.TestCase):
    def test_index_post_and_charter_pages_have_a_description(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            out = Path(d)
            index = (out / "index.html").read_text(encoding="utf-8")
            charter = (out / "charter.html").read_text(encoding="utf-8")
            post_path = next((out / "posts").glob("*.html"))
            post = post_path.read_text(encoding="utf-8")

        self.assertIn('<meta name="description" content=', index)
        self.assertIn('<meta name="description" content=', charter)
        self.assertIn('<meta name="description" content=', post)


class TestBuildIncludesCharterPage(unittest.TestCase):
    def test_charter_html_built_and_linked(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            out = Path(d)
            self.assertTrue((out / "charter.html").exists())
            charter = (out / "charter.html").read_text(encoding="utf-8")
            self.assertIn("<h2>Goal</h2>", charter)
            self.assertIn("<h2>Ownership</h2>", charter)

            index = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn('href="charter.html"', index)

            any_post = next((out / "posts").glob("*.html"))
            post = any_post.read_text(encoding="utf-8")
            self.assertIn('href="../charter.html"', post)


class TestBuildIncludesFavicon(unittest.TestCase):
    def test_favicon_copied_and_linked_on_every_page(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            out = Path(d)
            favicon = out / "favicon.svg"
            self.assertTrue(favicon.exists())
            self.assertEqual(
                favicon.read_bytes(), (build_site.STATIC_DIR / "favicon.svg").read_bytes()
            )

            index = (out / "index.html").read_text(encoding="utf-8")
            charter = (out / "charter.html").read_text(encoding="utf-8")
            not_found = (out / "404.html").read_text(encoding="utf-8")
            any_post = next((out / "posts").glob("*.html")).read_text(encoding="utf-8")

        for page in (index, charter, not_found, any_post):
            self.assertIn('rel="icon"', page)
            self.assertIn('href="/favicon.svg"', page)


class TestBuildIncludesMainLandmark(unittest.TestCase):
    """Every page needs exactly one <main> landmark around its actual
    content, closed before the footer, so a screen reader user can jump
    straight past the repeated boilerplate (back link, nav) instead of
    reading through it on every single page."""

    def test_main_wraps_content_and_excludes_footer(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            out = Path(d)
            index = (out / "index.html").read_text(encoding="utf-8")
            charter = (out / "charter.html").read_text(encoding="utf-8")
            not_found = (out / "404.html").read_text(encoding="utf-8")
            any_post = next((out / "posts").glob("*.html")).read_text(encoding="utf-8")

        for page in (index, charter, not_found, any_post):
            self.assertEqual(page.count("<main>"), 1)
            self.assertEqual(page.count("</main>"), 1)
            self.assertLess(page.index("<main>"), page.index("</main>"))

        # 404 has no footer; the other three do, and it must sit after </main>.
        for page in (index, charter, any_post):
            self.assertLess(page.index("</main>"), page.index("<footer>"))


class TestBackLinkIsInALandmark(unittest.TestCase):
    """Post and charter pages print a '<- all posts' link before <main>,
    which is deliberately excluded from the main landmark (it's boilerplate
    navigation, not the page's content). But content outside every landmark
    is itself an accessibility gap (axe-core's 'region' rule flags it) --
    the link needs a landmark of its own, not just exclusion from this one."""

    def test_back_link_sits_inside_a_nav(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            out = Path(d)
            charter = (out / "charter.html").read_text(encoding="utf-8")
            any_post = next((out / "posts").glob("*.html")).read_text(encoding="utf-8")

        for page in (charter, any_post):
            self.assertIn('<nav aria-label="back"><a class="back"', page)
            nav_open = page.index("<nav")
            nav_close = page.index("</nav>")
            back_link = page.index('<a class="back"')
            main_open = page.index("<main>")
            self.assertLess(nav_open, back_link)
            self.assertLess(back_link, nav_close)
            self.assertLess(nav_close, main_open)


class TestNavLandmarksAreDistinguishable(unittest.TestCase):
    """A post page has two <nav> elements -- the back-to-index link above
    <main>, and the prev/next post-nav below it. axe-core's
    'landmark-unique' rule flags two landmarks of the same type with no
    distinguishing accessible name, since a screen reader's landmark list
    would show two identical, unlabelled 'navigation' entries. Each nav
    needs its own aria-label."""

    def test_post_page_has_two_distinctly_labelled_navs(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            out = Path(d)
            any_post = next((out / "posts").glob("*.html")).read_text(encoding="utf-8")

        self.assertIn('<nav aria-label="back">', any_post)
        self.assertIn('<nav class="post-nav" aria-label="post navigation">', any_post)


class TestRenderPostNav(unittest.TestCase):
    OLDER = {"slug": "an-older-post", "title": "An older post"}
    NEWER = {"slug": "a-newer-post", "title": "A newer & bolder post"}

    def test_both_neighbours(self):
        out = build_site.render_post_nav(self.OLDER, self.NEWER)
        self.assertIn('href="an-older-post.html" rel="prev"', out)
        self.assertIn('href="a-newer-post.html" rel="next"', out)
        self.assertLess(out.index("rel=\"prev\""), out.index("rel=\"next\""))

    def test_escapes_titles(self):
        out = build_site.render_post_nav(None, self.NEWER)
        self.assertIn("A newer &amp; bolder post", out)

    def test_only_older(self):
        out = build_site.render_post_nav(self.OLDER, None)
        self.assertIn('rel="prev"', out)
        self.assertNotIn('rel="next"', out)

    def test_only_newer(self):
        out = build_site.render_post_nav(None, self.NEWER)
        self.assertIn('rel="next"', out)
        self.assertNotIn('rel="prev"', out)

    def test_no_neighbours_renders_nothing(self):
        self.assertEqual(build_site.render_post_nav(None, None), "")


class TestRenderStartHere(unittest.TestCase):
    OLDEST = {"slug": "starting-from-zero", "title": "Starting from zero, on purpose"}

    def test_links_to_oldest_post(self):
        out = build_site.render_start_here(self.OLDEST)
        self.assertIn('href="posts/starting-from-zero.html"', out)

    def test_escapes_slug(self):
        out = build_site.render_start_here({"slug": "a & b", "title": "x"})
        self.assertIn("a &amp; b", out)

    def test_no_posts_renders_nothing(self):
        self.assertEqual(build_site.render_start_here(None), "")


class TestBuildIndexPointsAtOldestPost(unittest.TestCase):
    def test_start_here_link_targets_actual_oldest_post(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            index = (Path(d) / "index.html").read_text(encoding="utf-8")
            posts = [build_site.parse_post(p) for p in build_site.POSTS_DIR.glob("*.md")]
            posts.sort(key=lambda p: (p["date"], p["commit_time"]), reverse=True)
            oldest = posts[-1]

        self.assertIn(f'href="posts/{oldest["slug"]}.html"', index)
        self.assertIn("start-here", index)


class TestBuildLinksAdjacentPosts(unittest.TestCase):
    """Every post page should offer a way onward without a trip through the
    index, and the chain has to run in reading order end to end."""

    def test_chain_runs_oldest_to_newest_with_no_breaks(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            out = Path(d)
            posts = [build_site.parse_post(p) for p in build_site.POSTS_DIR.glob("*.md")]
            posts.sort(key=lambda p: (p["date"], p["commit_time"]), reverse=True)
            pages = {
                p["slug"]: (out / "posts" / f"{p['slug']}.html").read_text(encoding="utf-8")
                for p in posts
            }

        # Walk from the oldest post to the newest, following only "next".
        walked = [posts[-1]["slug"]]
        while True:
            match = re.search(r'href="([^"]+)\.html" rel="next"', pages[walked[-1]])
            if not match:
                break
            walked.append(match.group(1))
        self.assertEqual(walked, [p["slug"] for p in reversed(posts)])

        # The ends are the ends: no dangling link off either edge.
        self.assertNotIn('rel="next"', pages[posts[0]["slug"]])
        self.assertNotIn('rel="prev"', pages[posts[-1]["slug"]])

    def test_neighbour_links_point_back_at_each_other(self):
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            out = Path(d)
            posts = [build_site.parse_post(p) for p in build_site.POSTS_DIR.glob("*.md")]
            posts.sort(key=lambda p: (p["date"], p["commit_time"]), reverse=True)
            newest = (out / "posts" / f"{posts[0]['slug']}.html").read_text(encoding="utf-8")
            second = (out / "posts" / f"{posts[1]['slug']}.html").read_text(encoding="utf-8")

        self.assertIn(f'href="{posts[1]["slug"]}.html" rel="prev"', newest)
        self.assertIn(f'href="{posts[0]["slug"]}.html" rel="next"', second)


if __name__ == "__main__":
    unittest.main()
