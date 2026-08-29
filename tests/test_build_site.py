import contextlib
import io
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

    def test_double_backtick_code_span_escapes_literal_backtick(self):
        # The standard markdown convention for putting a literal backtick
        # inside a code span is to delimit it with a *longer* run of
        # backticks ("`` `code` ``" displays as a code span whose content is
        # the literal text "`code`"), with one leading/trailing space
        # stripped when both edges are spaces. The single-backtick-only
        # regex here (`([^`]+)`) had no notion of this: it paired
        # backticks up two at a time regardless of run length, so a
        # double-backtick-delimited span got sliced into several bogus
        # single-backtick spans instead of being read as one. This isn't
        # hypothetical -- real posts in this journal use exactly this
        # idiom in prose describing the renderer's own backtick handling,
        # and it rendered broken (mismatched <code> tags, a stray literal
        # backtick, and unrelated later "*" pairs in the same paragraph
        # getting swept up as emphasis) on the actual built site.
        self.assertEqual(
            build_site.render_inline("`` `*italic*` ``"),
            "<code>`*italic*`</code>",
        )

    def test_double_backtick_code_span_content_not_further_processed(self):
        self.assertEqual(
            build_site.render_inline("`` `code` `` becomes `<code>code</code>`."),
            "<code>`code`</code> becomes <code>&lt;code&gt;code&lt;/code&gt;</code>.",
        )

    def test_standalone_multiplication_asterisks_are_not_treated_as_emphasis(self):
        # Two literal, space-flanked asterisks in the same paragraph (e.g. a
        # multiplication) used to get swept up as an <em> pair around
        # whatever sat between them.
        self.assertEqual(
            build_site.render_inline("3 * 4 * 5 = 60"),
            "3 * 4 * 5 = 60",
        )

    def test_double_asterisk_multiplication_not_treated_as_bold(self):
        self.assertEqual(
            build_site.render_inline("2 ** 3 ** 4 = huge"),
            "2 ** 3 ** 4 = huge",
        )

    def test_emphasis_with_internal_space_still_works(self):
        self.assertEqual(
            build_site.render_inline("*two words*"),
            "<em>two words</em>",
        )
        self.assertEqual(
            build_site.render_inline("**two words**"),
            "<strong>two words</strong>",
        )

    def test_bold_containing_nested_italic(self):
        # The bold regex's captured group used to exclude "*" entirely (to
        # keep literal multiplication asterisks like "2 ** 3 ** 4" from
        # being misread as bold), which also blocked any *italic* text
        # nested inside **bold** from matching at all -- the outer "**"
        # pair failed to match and leaked as literal asterisks into the
        # rendered page instead of becoming <strong>, even though the
        # opposite nesting (**bold** inside *italic*) already worked fine.
        self.assertEqual(
            build_site.render_inline("**bold *and italic* together**"),
            "<strong>bold <em>and italic</em> together</strong>",
        )

    def test_italic_containing_nested_bold_still_works(self):
        self.assertEqual(
            build_site.render_inline("*italic **and bold** together*"),
            "<em>italic <strong>and bold</strong> together</em>",
        )

    def test_bold_multiplication_asterisks_still_not_treated_as_bold(self):
        # Regression guard for the fix to test_bold_containing_nested_italic:
        # relaxing the bold regex to permit a single nested "*...*" pair
        # must not reopen the door to a literal "**" used as a Python-style
        # exponent operator being read as a bold delimiter.
        self.assertEqual(
            build_site.render_inline("2 ** 3 ** 4 = huge and **bold** too"),
            "2 ** 3 ** 4 = huge and <strong>bold</strong> too",
        )

    def test_triple_asterisk_bold_italic_combo_still_nests_correctly(self):
        self.assertEqual(
            build_site.render_inline("***really important***"),
            "<em><strong>really important</strong></em>",
        )

    def test_bold_with_unmatched_asterisk_does_not_produce_crossing_tags(self):
        # Regression guard for the fix to test_bold_containing_nested_italic:
        # that fix let the bold regex's middle contain a lone "*" (so a
        # nested *italic* run could sit inside a bold span), but it didn't
        # require that "*" to be part of an actual matched pair. A bold span
        # with a genuinely unpaired "*" in it -- e.g. from a literal,
        # space-free multiplication like "2*a" -- still matched as bold,
        # leaving that "*" un-rendered inside the new <strong>...</strong>
        # text. The separate italic pass that runs afterward then treated
        # that leftover "*" as an ordinary character free to pair with an
        # unrelated "*" *later in the same paragraph*, including one that
        # came after the closing "</strong>" tag -- producing invalid,
        # crossing markup (an <em> that opens before </strong> and closes
        # after it) instead of well-formed, properly nested elements:
        # render_inline("**2*a***ba*") used to render
        # '<strong>2<em>a</strong></em>ba*'. Requiring every "*" allowed in
        # a bold span's middle to belong to its own self-contained,
        # already-paired *...* run (not just any lone "*") closes the gap.
        out = build_site.render_inline("**2*a***ba*")
        # No tag may close except the most recently opened, still-unclosed
        # one -- i.e. tags may nest but never cross.
        stack = []
        for closing, tag in re.findall(r"<(/?)(em|strong|code)>", out):
            if not closing:
                stack.append(tag)
            else:
                self.assertTrue(stack and stack[-1] == tag, f"crossing tags in {out!r}")
                stack.pop()
        self.assertEqual(stack, [], f"unclosed tag(s) in {out!r}")


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

    def test_content_line_starting_with_backticks_does_not_close_the_fence_early(self):
        # A code block whose content itself demonstrates fence syntax (very
        # plausible for a blog about building this exact renderer) used to
        # have that content line -- because it merely *starts* with "```",
        # same as a language-tagged opener like "```python" would -- treated
        # as the fence's own close. Only an exact "```" line (no trailing
        # info-string) may close a fence; a line with trailing content after
        # the backticks is content, same as it's permitted to be an opener.
        body = (
            "```\n"
            "```python is how you'd tag it, but this renderer only supports\n"
            "plain triple-backtick fences, no language tags.\n"
            "```"
        )
        self.assertEqual(
            build_site.render_markdown(body),
            "<pre><code>```python is how you&#x27;d tag it, but this renderer only supports\n"
            "plain triple-backtick fences, no language tags.</code></pre>",
        )

    def test_fully_nested_fence_example_raises_instead_of_silently_corrupting(self):
        # A complete nested fenced example (its own open, content, and
        # close, all using the same plain "```" this renderer supports) is
        # inherently ambiguous to a single-length-fence parser -- nothing
        # distinguishes the inner close from the outer one. Before, this
        # produced no error at all: it silently emitted two empty <pre><code>
        # blocks and leaked the inner example's own content out as a bogus
        # visible paragraph, both on the page and in the feed's <summary>.
        # Failing loudly here (as an ordinary unterminated-fence ValueError,
        # the same one build.py already surfaces for other bad input) is far
        # safer than shipping silently-mangled HTML to production -- deploy.sh
        # runs this build step under `set -euo pipefail` and stops rather
        # than proceeding to sync broken output.
        body = "```\n```python\nprint('hello')\n```\n```\n\nReal paragraph after."
        with self.assertRaises(ValueError) as ctx:
            build_site.render_markdown(body, source="posts/example.md")
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

    def test_blank_quote_line_continues_a_multi_paragraph_blockquote(self):
        # A bare ">" (no trailing content) is how a multi-paragraph
        # blockquote is written -- it has no character after ">" to be a
        # space, but it's still part of the same blockquote, not new,
        # unrelated content. This used to flush the in-progress quote,
        # render the bare ">" itself as a bogus "<p>&gt;</p>" paragraph, and
        # open a second, separate <blockquote> for what followed.
        body = "> Para one.\n>\n> Para two."
        self.assertEqual(
            build_site.render_markdown(body),
            "<blockquote><p>Para one. Para two.</p></blockquote>",
        )


class TestSummary(unittest.TestCase):
    def test_first_paragraph(self):
        body = "First paragraph.\n\nSecond paragraph."
        self.assertEqual(build_site._summary(body), "First paragraph.")

    def test_skips_leading_heading_and_code_fence(self):
        body = "## Heading\n\n```\ncode\n```\n\nActual first paragraph."
        self.assertEqual(build_site._summary(body), "Actual first paragraph.")

    def test_content_line_starting_with_backticks_does_not_end_the_leading_fence_early(self):
        # Mirrors render_markdown()'s own fix: _summary() re-parses the raw
        # markdown separately and used to toggle out of "in code" mode on
        # *any* line starting with "```", not just an exact close. A leading
        # fence containing a language-tagged-looking content line (e.g.
        # "```python") toggled out of code mode right there, so the fence's
        # own remaining content leaked into the summary as if it were the
        # post's real first paragraph, instead of being skipped entirely as
        # a fence and falling through to the actual first paragraph after it.
        body = "```\n```python\nprint('hi')\n```\n\nActual first paragraph."
        self.assertEqual(build_site._summary(body), "Actual first paragraph.")

    def test_strips_backticks_and_asterisks(self):
        body = "Some `code` and **bold** and *italic* text."
        self.assertEqual(build_site._summary(body), "Some code and bold and italic text.")

    def test_literal_multiplication_asterisk_is_preserved(self):
        # Mirrors render_inline()'s own "3 * 4 * 5" protection (see the
        # comment above _BOLD_RE): _summary() used to blindly strip every
        # "*" character, deleting a literal one that render_markdown()
        # correctly keeps, and leaving a double space behind.
        body = "The trick was 3 * 4 * 5 = 60, done by hand."
        self.assertEqual(
            build_site._summary(body), "The trick was 3 * 4 * 5 = 60, done by hand."
        )

    def test_code_span_content_is_not_further_stripped_of_backticks_or_asterisks(self):
        # _summary() used to run a blanket `re.sub(r"[`*]", "", ...)` over
        # the whole paragraph, corrupting a code span's actual content
        # (e.g. "`2*a`" losing its "*") instead of only removing the
        # delimiters -- the same code-span-content-is-literal rule
        # render_inline()/_stash_code_spans() already enforce.
        body = "Computed with `2*a` style code."
        self.assertEqual(build_site._summary(body), "Computed with 2*a style code.")
        body2 = "Delimit a literal backtick like `` `code` `` in prose."
        self.assertEqual(
            build_site._summary(body2), "Delimit a literal backtick like `code` in prose."
        )

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

    def test_paragraph_starting_with_bare_hash_is_not_dropped(self):
        # render_markdown() only treats an exact "## " prefix as a heading;
        # a line starting with any other run of "#" (a single "#", "###", or
        # no trailing space) is ordinary paragraph text there. _summary()
        # used to skip *any* line starting with "#" as if it were a heading,
        # so a first paragraph like "#47 was a weird one." silently
        # disappeared from the summary and it fell through to the next
        # paragraph instead — a real mismatch between the rendered post body
        # and its own <meta description>/feed summary.
        body = "#47 was a weird one. It broke everything downstream.\n\nSecond paragraph."
        self.assertEqual(
            build_site._summary(body),
            "#47 was a weird one. It broke everything downstream.",
        )

    def test_heading_with_no_blank_line_before_it_still_ends_the_paragraph(self):
        # render_markdown() flushes the current paragraph on hitting "## ",
        # a fence, or "> ", whether or not a blank line precedes it — that's
        # what makes it a new block instead of more of the same paragraph.
        # _summary() only ever broke on a genuinely blank line, so a heading
        # (or fence, or blockquote) glued directly onto the first paragraph
        # with no blank line in between was silently skipped/merged instead
        # of ending the summary there, splicing in a later, unrelated
        # paragraph's text.
        body = "First paragraph line.\n## Heading\nSecond paragraph line."
        self.assertEqual(build_site._summary(body), "First paragraph line.")

    def test_code_fence_with_no_blank_line_before_it_still_ends_the_paragraph(self):
        body = "A paragraph.\n```\ncode\n```\nMore text."
        self.assertEqual(build_site._summary(body), "A paragraph.")

    def test_blockquote_with_no_blank_line_before_it_still_ends_the_paragraph(self):
        body = "First para.\n> quote line."
        self.assertEqual(build_site._summary(body), "First para.")

    def test_paragraph_with_no_blank_line_before_it_still_ends_a_blockquote(self):
        body = "> Quoted text\nSecond paragraph line."
        self.assertEqual(build_site._summary(body), "Quoted text")

    def test_blank_quote_line_continues_a_multi_paragraph_blockquote(self):
        # Mirrors render_markdown()'s own fix: a bare ">" is a blank line
        # inside a multi-paragraph blockquote, not new, unrelated content
        # that ends it (see the matching comment in render_markdown() for
        # the concrete repro of the old behavior).
        body = "> Para one.\n>\n> Para two."
        self.assertEqual(build_site._summary(body), "Para one. Para two.")


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

    def test_empty_required_key_value_names_the_file(self):
        # A required key that's *present* but left blank (e.g. "date:" with
        # nothing after the colon -- a plausible copy-the-template typo) is
        # just as broken as the key being missing outright, and must raise
        # the same named-file error rather than silently parsing as an empty
        # string that then corrupts sorting, the rendered post date, and the
        # feed's <updated> timestamp (which became the malformed
        # "T00:00:00Z", missing the date entirely).
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text('---\ntitle: Something\ndate:\n---\nBody with a blank date.\n', encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("date", str(ctx.exception))

    def test_non_iso_date_format_names_the_file(self):
        # A non-empty date in the wrong shape (a human-written "Aug 30,
        # 2026", or a copy-paste of "08/30/2026") used to sail straight
        # through: `posts.sort()` compares "date" as a plain string, so a
        # differently-formatted value lands wherever its characters happen
        # to compare against real "YYYY-MM-DD" values, not where the post
        # was actually written -- and the feed's <updated> value became the
        # malformed "Aug 30, 2026T00:00:00Z". Must raise the same
        # named-file error the empty/missing cases already do.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: Something\ndate: Aug 30, 2026\n---\nBody with a malformed date.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("date", str(ctx.exception))

    def test_no_dash_iso_date_is_rejected_despite_fromisoformat_accepting_it(self):
        # Since Python 3.11, `datetime.date.fromisoformat()` also accepts a
        # dashless "20260830" form -- valid ISO 8601, but not the
        # "YYYY-MM-DD" shape every post in this journal actually uses, and
        # a dashless value would itself sort inconsistently against the
        # dashed dates it's meant to be interchangeable with. The explicit
        # format check must reject it even though fromisoformat() alone
        # would happily parse it.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: Something\ndate: 20260830\n---\nBody with a dashless date.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))

    def test_calendar_invalid_date_names_the_file(self):
        # "2026-02-30" matches the YYYY-MM-DD *shape* but isn't a real date
        # -- February never has a 30th. The format regex alone would miss
        # this; the date must actually be constructed to catch it.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: Something\ndate: 2026-02-30\n---\nBody with an invalid calendar date.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("date", str(ctx.exception))

    def test_unquoted_value_ending_in_a_literal_quote_mark_is_not_mangled(self):
        # An unquoted title that happens to end with a quote character (e.g.
        # a quoted phrase the author didn't wrap the whole title in) used to
        # lose that trailing quote: value.strip('"') strips any number of
        # matching characters from each end independently, not just a
        # balanced wrapping pair, so "He said \"no\"" became "He said \"no.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "2026-01-01-quoted.md"
            path.write_text(
                '---\ntitle: He said "no"\ndate: 2026-01-01\n---\nBody.\n',
                encoding="utf-8",
            )
            post = build_site.parse_post(path)
            self.assertEqual(post["title"], 'He said "no"')

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

    def test_control_character_in_title_does_not_break_xml(self):
        # A stray control byte (e.g. an ESC from a pasted terminal log)
        # landing in a title used to sail through html.escape() untouched
        # -- html.escape() only guards against markup injection, not XML
        # well-formedness -- and produced a feed.xml that no XML parser
        # (and no real feed reader) would accept, with build() itself
        # reporting success.
        feed = build_site.render_feed(
            [self._post(title="Session log \x1b[31mERROR\x1b[0m recap")],
            "https://example.test",
        )
        xml.dom.minidom.parseString(feed)  # raises if malformed
        self.assertNotIn("\x1b", feed)

    def test_control_character_in_body_summary_does_not_break_xml(self):
        feed = build_site.render_feed(
            [self._post(body="Body with a stray \x1b control byte in it.")],
            "https://example.test",
        )
        xml.dom.minidom.parseString(feed)  # raises if malformed
        self.assertNotIn("\x1b", feed)

    def test_unescaped_character_in_date_does_not_break_xml(self):
        # <updated> (both the feed-level one and each entry's) is built from
        # _entry_timestamp(), which for an uncommitted post falls back to the
        # post's raw `date` frontmatter value verbatim -- unlike every other
        # field this function emits (title, link, id, summary), it was never
        # run through html.escape(). Frontmatter's `date` is free-form text
        # with no format validation anywhere in parse_post(), so a value
        # containing a bare "&" or "<" produced a feed.xml that fails to
        # parse, the same XML-well-formedness failure already fixed for
        # title/summary, just left open on this field.
        feed = build_site.render_feed(
            [self._post(date="2026-08-25 & counting", commit_time=build_site.UNCOMMITTED_SENTINEL)],
            "https://example.test",
        )
        xml.dom.minidom.parseString(feed)  # raises if malformed
        self.assertNotIn(" & counting", feed)
        self.assertIn("&amp;", feed)


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


class TestBuildEscapesSlugInIndexHref(unittest.TestCase):
    """The index page's post-list <li> links build an href straight from
    each post's slug (its filename, sans extension). Every other place this
    file interpolates a slug into an href -- render_post_nav, render_start_here,
    render_feed's entry URLs -- runs it through html.escape() first; this was
    the one spot that didn't, so a slug containing an HTML-special character
    (e.g. a post filed as "2026-01-01-q&a-session.md", a plausible name for a
    post about a Q&A) landed as a raw, unescaped "&" in the href -- an
    ambiguous ampersand, invalid HTML -- right next to its own
    correctly-escaped title text."""

    def test_ampersand_in_slug_is_escaped_in_index_href(self):
        orig_posts_dir = build_site.POSTS_DIR
        orig_static_dir = build_site.STATIC_DIR
        orig_charter_path = build_site.CHARTER_PATH
        try:
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                posts_dir = d / "posts"
                posts_dir.mkdir()
                static_dir = d / "static"
                static_dir.mkdir()
                (static_dir / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
                (d / "CHARTER.md").write_text("# Charter\n\nA rule.\n", encoding="utf-8")
                (posts_dir / "2026-01-01-q&a-session.md").write_text(
                    '---\ntitle: "A Q&A session"\ndate: 2026-01-01\n---\nBody text.\n',
                    encoding="utf-8",
                )

                build_site.POSTS_DIR = posts_dir
                build_site.STATIC_DIR = static_dir
                build_site.CHARTER_PATH = d / "CHARTER.md"

                out = d / "_site"
                build_site.build(out)
                index = (out / "index.html").read_text(encoding="utf-8")
        finally:
            build_site.POSTS_DIR = orig_posts_dir
            build_site.STATIC_DIR = orig_static_dir
            build_site.CHARTER_PATH = orig_charter_path

        self.assertIn('href="posts/2026-01-01-q&amp;a-session.html"', index)
        self.assertNotIn('href="posts/2026-01-01-q&a-session.html"', index)


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


class TestResolveOutputDir(unittest.TestCase):
    def test_no_argument_defaults_to_site_dir(self):
        self.assertEqual(
            build_site._resolve_output_dir(["build_site.py"]), REPO_ROOT / "_site"
        )

    def test_positional_argument_is_used_as_the_output_dir(self):
        self.assertEqual(
            build_site._resolve_output_dir(["build_site.py", "out"]), "out"
        )

    def test_help_flag_prints_usage_and_exits_zero_instead_of_building(self):
        # Pre-fix, this raised no SystemExit at all -- it returned the
        # literal string "--help" as an output directory, and the caller
        # went on to build the whole site into a real "./--help/" folder.
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stdout(stdout):
                build_site._resolve_output_dir(["build_site.py", "--help"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("Usage:", stdout.getvalue())

    def test_unrecognized_flag_is_rejected_not_treated_as_a_directory_name(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(stderr):
                build_site._resolve_output_dir(["build_site.py", "--bogus"])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("--bogus", stderr.getvalue())

    def test_too_many_arguments_is_rejected(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(stderr):
                build_site._resolve_output_dir(["build_site.py", "a", "b"])
        self.assertNotEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
