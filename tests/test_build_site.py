import contextlib
import io
import os
import re
import subprocess
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

    def test_invisible_unicode_character_adjacent_to_asterisk_is_not_treated_as_an_emphasis_boundary(self):
        # test_standalone_multiplication_asterisks_are_not_treated_as_emphasis
        # above relies on _ITALIC_RE/_BOLD_RE's `[^*\s]` boundary check: the
        # character immediately touching a "*" must not be whitespace, so a
        # literal, space-flanked "*" (e.g. "3 * 4 * 5") isn't misread as an
        # emphasis delimiter. But `\s` only recognizes actual whitespace --
        # a zero-width space (U+200B) sitting in that exact spot is neither
        # "*" nor whitespace to the regex, so it satisfies `[^*\s]` and the
        # match succeeds anyway, even though it's visually indistinguishable
        # from the already-protected space-flanked case.
        # render_inline("3 *​4​* 5 = 60") used to render
        # "3 <em>​4​</em> 5 = 60" -- a real <em> wrapped around
        # what still visually reads as bare multiplication.
        zwsp = "​"
        text = f"3 *{zwsp}4{zwsp}* 5 = 60"
        self.assertEqual(build_site.render_inline(text), text)

    def test_invisible_unicode_character_adjacent_to_bold_asterisks_is_not_treated_as_a_boundary(self):
        zwsp = "​"
        text = f"**{zwsp}bold{zwsp}**"
        self.assertEqual(build_site.render_inline(text), text)

    def test_invisible_unicode_boundary_on_a_nested_italic_leaves_only_that_span_literal(self):
        # An invisible boundary on a *nested* italic run inside an otherwise
        # well-formed **bold** span must reject only that inner match, not
        # the outer bold -- the outer <strong> still has genuinely visible
        # boundary characters ("b" and "e" here) and should still be wrapped.
        zwsp = "​"
        text = f"**bold *{zwsp}text{zwsp}* more**"
        self.assertEqual(
            build_site.render_inline(text),
            f"<strong>bold *{zwsp}text{zwsp}* more</strong>",
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

    def test_nested_italic_inside_triple_asterisk_bold_does_not_cross_tags(self):
        # A different way to reach the same crossing-tags failure shape as
        # test_bold_with_unmatched_asterisk_does_not_produce_crossing_tags
        # above, not covered by that fix: _BOLD_RE's middle is allowed to
        # contain a fully self-paired *italic* run (that's what lets
        # "**bold *and italic* together**" match at all), and the old
        # substitution spliced that captured group -- asterisks and all --
        # straight into the string as `<strong>{captured text}</strong>`
        # via a plain backreference, *before* the separate _ITALIC_RE.sub()
        # pass ever ran. That pass then re-scanned the *whole* string with
        # no notion of HTML tag boundaries and was free to pair a raw "*"
        # still sitting inside the freshly-inserted <strong>...</strong>
        # with an unrelated, unconsumed "*" outside of it -- e.g. the
        # leading "*" left over from a "***" bold+italic combo delimiter.
        # render_inline("***x*y*z***") used to render
        # "<em><strong>x</em>y<em>z</strong></em>" -- an <em> that opens
        # inside <strong> and closes after </strong>, straddling it.
        out = build_site.render_inline("***x*y*z***")
        self.assertEqual(out, "<em><strong>x<em>y</em>z</strong></em>")
        stack = []
        for closing, tag in re.findall(r"<(/?)(em|strong|code)>", out):
            if not closing:
                stack.append(tag)
            else:
                self.assertTrue(stack and stack[-1] == tag, f"crossing tags in {out!r}")
                stack.pop()
        self.assertEqual(stack, [], f"unclosed tag(s) in {out!r}")

    def test_unpaired_asterisk_does_not_pair_across_an_unrelated_bold_delimiter(self):
        # A lone, unpaired "*" from a literal multiplication (e.g. "x*y",
        # with no space around the asterisk so it can't match
        # test_standalone_multiplication_asterisks_are_not_treated_as_emphasis'
        # space-flanked case) has no partner of its own anywhere in the
        # string. But _ITALIC_RE's old pattern -- r"\*([^*\s](?:[^*]*[^*\s])?)\*"
        # -- placed no restriction on what character sits on the *other*
        # side of either delimiter: its `[^*]*` middle was free to run
        # straight past a space and swallow one half of a later, unrelated
        # "**" run (e.g. the "**" in "a**b", itself not a valid bold
        # delimiter pair on its own -- there's no closing "**" for it), and
        # treat the near asterisk of that pair as this italic's closing
        # delimiter instead. render_inline("x*y a**b") used to render
        # "x<em>y a</em>*b" -- eating the literal "*" out of "x*y",
        # wrapping unrelated text spanning two words in <em>, and leaving a
        # stray "*" dangling in front of "b" -- instead of leaving both
        # literal fragments untouched. Requiring each italic delimiter to
        # be an isolated single "*" (not preceded or followed by another
        # "*", via (?<!\*) / (?!\*) on both ends) closes the gap: a "*"
        # that is one half of a "**" run can no longer serve as either an
        # opening or closing italic delimiter at all.
        self.assertEqual(build_site.render_inline("x*y a**b"), "x*y a**b")
        self.assertEqual(build_site.render_inline("2*a b**c"), "2*a b**c")
        # A genuine, well-formed *italic* elsewhere in the same paragraph
        # must still work once the unrelated "**" run is out of the way.
        self.assertEqual(
            build_site.render_inline("x*y a**b *world*"),
            "x*y a**b <em>world</em>",
        )


class TestPage(unittest.TestCase):
    def test_no_description_by_default(self):
        out = build_site.page("Title", "<p>Body</p>")
        self.assertNotIn('name="description"', out)

    def test_description_rendered_and_escaped(self):
        out = build_site.page("Title", "<p>Body</p>", description='A "quoted" claim & more')
        self.assertIn(
            '<meta name="description" content="A &quot;quoted&quot; claim &amp; more">', out
        )

    def test_a_genuinely_empty_description_still_gets_a_tag(self):
        # description=None (the default, tested above) means "nothing was
        # ever computed" and correctly omits the tag entirely. But
        # _summary() can legitimately compute a real, non-None answer that
        # happens to be "" -- a post whose body opens with a heading or a
        # fenced code block and never reaches a leading plain-text
        # paragraph (see the next test). A truthiness check on
        # `description` used to treat that the same as the None case,
        # silently dropping the <meta name="description"> tag for a page
        # that *did* compute a description, just an empty one -- the same
        # "every page gets this tag" invariant already enforced for the
        # 404 page, reached through a post's own content this time instead
        # of a missing argument.
        out = build_site.page("Title", "<p>Body</p>", description="")
        self.assertIn('<meta name="description" content="">', out)


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

    def test_line_of_only_invisible_unicode_characters_still_separates_paragraphs(self):
        # A "blank" line that visually looks empty in an editor but actually
        # contains only invisible Unicode formatting characters (e.g. a
        # zero-width space left behind by a paste) is exactly the kind of
        # blank-looking-but-not-`str.isspace()` value `_is_blank()` exists
        # to catch -- already applied to frontmatter values and to emphasis
        # match boundaries (see `_has_invisible_boundary()`), but the
        # paragraph-break check here still used the plain `line.strip() ==
        # ""` it always had, which only recognizes ordinary whitespace as
        # "blank". A zero-width space survives that strip untouched (it's
        # Unicode category 'Cf', not whitespace), so the "blank" line didn't
        # end the paragraph at all: render_markdown("First.\n​\nSecond.")
        # used to fold both sentences into one <p>, splicing the invisible
        # character in as literal (invisible) text between them, instead of
        # producing two separate paragraphs the way a genuinely blank line
        # does.
        self.assertEqual(
            build_site.render_markdown("First.\n​\nSecond."),
            "<p>First.</p>\n<p>Second.</p>",
        )

    def test_heading(self):
        self.assertEqual(build_site.render_markdown("## A Heading"), "<h2>A Heading</h2>")

    def test_blank_heading_raises_instead_of_producing_an_invisible_h2(self):
        # A "## " line whose only content is an invisible Unicode formatting
        # character (e.g. a zero-width space) used to sail straight through
        # to render_inline() with no check at all -- unlike a required
        # frontmatter value, a blockquote's blank-continuation line, or a
        # paragraph-break line, none of which this heading branch shares any
        # code with. render_markdown("## ​\nSome text.") used to produce
        # "<h2>​</h2>\n<p>Some text.</p>": a real <h2> element in the
        # markup that carries no visible or accessible text at all, sitting
        # right above the paragraph it was meant to introduce. There's no
        # legitimate reason for a heading to be blank (unlike a blank
        # blockquote-continuation line), so this now fails the build the
        # same way an unterminated code fence does -- loud, naming the file.
        body = "## ​\nSome text."
        with self.assertRaises(ValueError) as ctx:
            build_site.render_markdown(body, source="posts/example.md")
        self.assertIn("posts/example.md", str(ctx.exception))
        self.assertIn("heading has no visible heading text", str(ctx.exception))

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

    def test_longer_opening_fence_is_not_closed_by_a_shorter_bare_fence_inside_it(self):
        # The standard way to show a literal ``` fence inside a fenced block
        # is to wrap it in a longer one (here "````"). Before, the fence
        # closer only ever matched a bare "```" regardless of how many
        # backticks actually opened it, so a "````"-opened block closed on
        # the first plain "```" line inside -- reopening the exact
        # silently-corrupting failure mode
        # test_fully_nested_fence_example_raises_instead_of_silently_corrupting
        # guards against, just reachable through a longer opener instead of
        # an equal-length one. This should raise the same loud
        # unterminated-fence error, not silently split into stray code
        # blocks and a leaked paragraph.
        body = "````\nfoo\n```\nbar\n```\nclosed content\n```\n\nFinal paragraph."
        with self.assertRaises(ValueError) as ctx:
            build_site.render_markdown(body, source="posts/example.md")
        self.assertIn("unterminated code fence", str(ctx.exception))

    def test_longer_opening_fence_closes_only_on_a_matching_length_marker(self):
        # The well-formed version of the above: a "````"-opened block
        # closes only on a bare "````" line, so a literal "```" (and
        # anything else short of a 4-backtick-only line) inside it stays
        # part of the code block's content instead of ending it early.
        body = "````\nfoo\n```\nbar\n```\nclosed content\n````\n\nFinal paragraph."
        self.assertEqual(
            build_site.render_markdown(body),
            "<pre><code>foo\n```\nbar\n```\nclosed content</code></pre>\n"
            "<p>Final paragraph.</p>",
        )

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

    def test_invisible_only_quote_line_is_treated_as_blank_not_content(self):
        # A "> " line whose only content is an invisible Unicode formatting
        # character (e.g. a zero-width space left behind by a paste) is
        # exactly the same "blank paragraph separator inside a
        # multi-paragraph blockquote" case the bare-">" fix above already
        # covers -- but the content check here was a plain truthy test
        # (`if content:`), which only recognizes a *literally empty* string
        # as "no content." `line[2:].strip()` doesn't remove a Cf-category
        # character (the same gap _is_blank() exists to close everywhere
        # else in this file, e.g. for a whole blank line or a frontmatter
        # value), so this line's "content" came out as one non-empty,
        # invisible character, was appended to the quote's line list, and
        # was spliced into the middle of the rendered blockquote as literal
        # (invisible) text: render_markdown("> Para one.\n> ​\n> Para
        # two.") used to produce "Para one. ​ Para two." instead of
        # "Para one. Para two." like the equivalent bare-">" separator.
        body = "> Para one.\n> ​\n> Para two."
        self.assertEqual(
            build_site.render_markdown(body),
            "<blockquote><p>Para one. Para two.</p></blockquote>",
        )

    def test_quote_marker_with_no_space_followed_by_invisible_char_continues_the_blockquote(self):
        # Same gap as the bare-">" and "> <invisible>" fixes just above, one
        # character position earlier: a "blank quote separator" line can
        # also be written as ">" with *no* space at all, immediately
        # followed by an invisible Unicode formatting character (e.g. a
        # zero-width space pasted right after the marker instead of a real
        # space). `line.startswith("> ")` is false (the second character
        # isn't a literal space) and `line.rstrip() == ">"` is also false
        # (str.rstrip() only trims *whitespace*, and a Cf-category character
        # like U+200B isn't whitespace -- the identical blind spot
        # `_is_blank()` exists everywhere else in this file to close). So
        # this line fell through to the default paragraph-text branch,
        # flushing the in-progress quote early, rendering the line itself as
        # a bogus visible "<p>&gt;​</p>" paragraph, and opening a second,
        # separate <blockquote> for what followed -- splitting one intended
        # multi-paragraph blockquote into two, with a stray literal ">"
        # sandwiched between them.
        body = "> Para one.\n>​\n> Para two."
        self.assertEqual(
            build_site.render_markdown(body),
            "<blockquote><p>Para one. Para two.</p></blockquote>",
        )


class TestSummary(unittest.TestCase):
    def test_first_paragraph(self):
        body = "First paragraph.\n\nSecond paragraph."
        self.assertEqual(build_site._summary(body), "First paragraph.")

    def test_line_of_only_invisible_unicode_characters_still_separates_paragraphs(self):
        # Mirrors render_markdown()'s own fix (see the matching test there
        # for the full explanation): a "blank" line made only of invisible
        # Unicode formatting characters (e.g. a zero-width space) survives
        # `line.strip() == ""` untouched, so it used to fail to end the
        # leading paragraph -- _summary("First paragraph.\n​\nSecond
        # paragraph.") pulled in the second sentence (and the invisible
        # character) as if they were part of the same first paragraph,
        # instead of stopping at the actual paragraph break.
        body = "First paragraph.\n​\nSecond paragraph."
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

    def test_longer_opening_fence_is_not_closed_by_a_shorter_bare_fence_inside_it(self):
        # Mirrors render_markdown()'s own fix: _summary() used to toggle out
        # of "in code" mode on any bare "```" line regardless of how many
        # backticks actually opened the fence, so a leading "````"-opened
        # block containing a literal "```" line closed early right there,
        # leaking the fence's own remaining content ("bar") into the
        # summary instead of skipping the whole fence and reaching the
        # real first paragraph after it.
        body = "````\nfoo\n```\nbar\n```\nclosed content\n````\n\nActual first paragraph."
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

    def test_invisible_unicode_character_adjacent_to_asterisk_is_not_treated_as_an_emphasis_boundary(self):
        # _summary()'s sibling of
        # TestRenderInline.test_invisible_unicode_character_adjacent_to_asterisk_is_not_treated_as_an_emphasis_boundary
        # above: _summary() reuses the same _BOLD_RE/_ITALIC_RE (and their
        # invisible-boundary check), so a zero-width space standing in for
        # an ordinary space next to a literal "*" must stay literal here
        # too, not have its asterisks silently stripped as if it were real
        # emphasis markup.
        zwsp = "​"
        body = f"The trick was 3 *{zwsp}4{zwsp}* 5 = 60, done by hand."
        self.assertEqual(build_site._summary(body), body)

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

    def test_bold_containing_nested_italic_does_not_leak_asterisks_to_an_unrelated_stray_one(self):
        # render_inline()'s _bold_replace() resolves a bold match's own
        # nested *italic* run (e.g. the "*b*" in "**a *b* c**") to plain
        # text right when the match is found, scoped to that match, before
        # splicing the result back into the string -- so by the time its
        # separate, whole-string _ITALIC_RE.sub() pass runs afterward, none
        # of those inner asterisks are still there for it to find. _summary()
        # re-parses the same raw markdown for the same result but used to
        # strip bold via a plain `_BOLD_RE.sub(r"\1", text)` backreference
        # instead -- splicing the captured group straight back in with its
        # nested italic's asterisks still literally present. Its own later,
        # whole-string _ITALIC_RE.sub() pass then saw those survived
        # asterisks as ordinary text, free to pair one of them with an
        # unrelated, unpaired "*" sitting *outside* the original bold span
        # -- e.g. a literal, space-free multiplication like "2*a" earlier in
        # the same paragraph -- instead of leaving both alone.
        # _summary("2*a a**b x*y ` `x*y` a**b") used to return
        # "2a ab xy  x*y` ab" (the literal "2*a" corrupted into "2a", and a
        # stray "*" left in "x*y`") -- diverging from what render_inline()
        # (and therefore the actual rendered post) shows for the identical
        # raw text. Resolving a bold match's own nested italic the same way
        # render_inline() does, via _bold_strip_replace(), keeps the two in
        # sync.
        body = "2*a a**b x*y ` `x*y` a**b"
        rendered_plain_text = re.sub(
            r"<[^>]+>", "", build_site.render_inline(body)
        )
        self.assertEqual(build_site._summary(body), rendered_plain_text)
        self.assertEqual(build_site._summary(body), "2*a ab xy  xy` ab")

    def test_truncates_long_paragraph_at_word_boundary(self):
        summary = build_site._summary("word " * 100)
        self.assertTrue(summary.endswith("…"))
        self.assertLessEqual(len(summary), 281)
        self.assertNotIn("  ", summary)

    def test_truncation_falls_back_to_a_hard_cut_when_the_word_boundary_is_blank(self):
        # The word-boundary truncation above (`text[:280].rsplit(" ",
        # 1)[0]`) assumes the space it splits on has real content in front
        # of it. That's true when the paragraph is made of ordinary
        # space-separated words, but a restored code span can put a literal
        # space at or near the very start of the text -- a single-backtick
        # span whose content is only whitespace (e.g. "` `", the natural way
        # to name a literal space character in prose about this renderer's
        # own code-span rules -- see test_code_span_content_is_not_further_
        # stripped_of_backticks_or_asterisks above) isn't stripped by
        # _stash_code_spans() at all, since its "trim one leading/trailing
        # space" rule only fires when the content isn't *all* spaces. If
        # that leading space is immediately followed by a long unspaced run
        # (a hash, a slug, a URL -- easy to hit 280 characters with no other
        # space before the cutoff), it becomes the *only* space in
        # text[:280], and rsplit(" ", 1) splits right there: the "before"
        # half is empty, so the old code produced a summary that was
        # nothing but "…" -- the entire actual paragraph silently discarded,
        # in both the feed <summary> and the post's own <meta
        # name="description">. Falling back to a hard 280-character cut
        # when the word-boundary split would leave nothing (or only
        # whitespace) in front of it keeps some of the real text instead of
        # throwing it all away.
        body = "` `" + "a1b2c3d4e5" * 30  # 303 chars total, one leading space, then unbroken
        summary = build_site._summary(body)
        self.assertTrue(summary.endswith("…"))
        self.assertGreater(len(summary.rstrip("…").strip()), 0)

    def test_empty_for_a_post_with_no_leading_paragraph(self):
        # A post that opens directly with a fenced code block, with no
        # plain-text paragraph before it, has no first paragraph to
        # summarize -- _summary() falls through every branch and returns
        # "". This is the real-world case that reaches TestPage's
        # test_a_genuinely_empty_description_still_gets_a_tag: such a
        # post's own page() call is handed description="", not None.
        body = "```\nsome code, no leading prose\n```\n"
        self.assertEqual(build_site._summary(body), "")

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

    def test_invisible_only_quote_line_is_treated_as_blank_not_content(self):
        # _summary()'s sibling of render_markdown()'s equivalent fix above:
        # a "> " line holding only an invisible Unicode formatting character
        # (e.g. a zero-width space) survived `line[2:].strip()` as one
        # non-empty character and was spliced into the summary as literal
        # invisible text between the two quoted lines, instead of being
        # treated as a blank paragraph separator like a bare ">" already is.
        body = "> Para one.\n> ​\n> Para two."
        self.assertEqual(build_site._summary(body), "Para one. Para two.")

    def test_quote_marker_with_no_space_followed_by_invisible_char_continues_the_blockquote(self):
        # _summary()'s sibling of render_markdown()'s equivalent fix above:
        # a ">" with no following space, immediately followed only by an
        # invisible Unicode formatting character, is a blank paragraph
        # separator inside a multi-paragraph blockquote, not new content
        # that ends it -- see the matching test/comment in
        # TestRenderMarkdown for the concrete repro of the old behavior.
        body = "> Para one.\n>​\n> Para two."
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

    def test_whitespace_only_quoted_required_value_names_the_file(self):
        # A quoted value containing only whitespace (e.g. `title: "   "`) is
        # just as broken as an outright-blank or missing key, but the plain
        # `not meta.get(required)` check missed it: the surrounding
        # `value.strip()` runs before the quotes are stripped off, so it only
        # ever trims whitespace *outside* a quoted value, never inside it --
        # `"   "` stays non-empty (truthy) and sails through. The post then
        # built successfully but rendered a blank <title>/<h1> and an index
        # entry whose link had no visible or accessible text at all. Must
        # raise the same named-file error the blank/missing cases already do.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: "   "\ndate: 2026-01-01\n---\nBody with a blank-looking title.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("title", str(ctx.exception))

    def test_zero_width_only_title_names_the_file(self):
        # A title made entirely of invisible Unicode formatting characters
        # (category 'Cf' -- zero-width spaces here) is just as blank-looking
        # as "title: \"   \"" (already caught above), but survives every
        # `.strip()` call in parse_post(): `str.strip()` only trims
        # characters `str.isspace()` calls whitespace, and a zero-width
        # space isn't whitespace by that definition -- it's a real,
        # non-empty character that happens to render invisibly. `not
        # meta.get(required)` alone sees three real characters and never
        # fires, so the post used to build successfully with a `<title>`,
        # `<h1>`, and index link that are all present in the markup but
        # carry no visible or accessible text. Must raise the same
        # named-file error the blank/whitespace-only cases already do.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: "​​​"\ndate: 2026-01-01\n---\n'
                'Body with an invisible title.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("title", str(ctx.exception))

    def test_control_character_only_title_names_the_file(self):
        # A title made entirely of raw control bytes (e.g. a stray ESC from
        # a pasted terminal log -- the exact scenario _strip_invalid_xml_chars()
        # itself was written to guard against) is just as blank-looking as
        # the whitespace-only and zero-width-only cases already caught
        # above, but slips past _is_blank() through a gap neither of those
        # fixes closed: _is_blank() only treats a character as blank if
        # `str.isspace()` says so or it's Unicode category 'Cf' (invisible
        # formatting characters). A C0 control character like U+0001 is
        # neither -- it's category 'Cc', not whitespace -- so _is_blank()
        # sees one "real" character and the required-key check passes. The
        # title then reaches the return dict, where
        # _strip_invalid_xml_chars() (applied to close the *separate*
        # feed.xml well-formedness gap) strips that same control character
        # right back out, leaving `post["title"]` an empty string: a post
        # that built "successfully" with a blank, inaccessible
        # <title>/<h1> and an index link with no visible text at all --
        # the same failure mode as the two prior fixes, just via a third
        # character class neither of them checked for.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_bytes(
                b'---\ntitle: "\x01"\ndate: 2026-01-01\n---\n'
                b"Body with a control-character title.\n"
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("title", str(ctx.exception))

    def test_variation_selector_only_title_names_the_file(self):
        # A title made entirely of variation selectors (e.g. U+FE0F, the
        # "render the preceding character as an emoji" selector -- plausible
        # if an emoji got deleted out of a title during editing but the
        # trailing selector it modified was left behind) is just as
        # blank-looking as the whitespace-only, zero-width-only, and
        # control-character-only cases already caught above, but slips past
        # _is_blank() through a gap none of those fixes closed: a variation
        # selector has no glyph of its own and only ever modifies the
        # character immediately before it, so standing alone it renders as
        # nothing at all -- exactly as invisible as a Cf character -- but
        # the Unicode Character Database files it under general category
        # 'Mn' (nonspacing mark), not 'Cf'. _is_blank() only ever treated a
        # character as blank via `str.isspace()` or category 'Cf', so it saw
        # three "real" (non-blank) characters here and the required-key
        # check passed. The post then built successfully with a
        # `<title>`/`<h1>` and an index link that are all present in the
        # markup but carry no visible or accessible text at all -- the same
        # failure mode as the three prior fixes, just via a fourth character
        # class none of them checked for.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: "️️️"\ndate: 2026-01-01\n---\n'
                'Body with a variation-selector-only title.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("title", str(ctx.exception))

    def test_hangul_filler_only_title_names_the_file(self):
        # A title made entirely of Hangul filler characters (U+3164 HANGUL
        # FILLER here -- the same "invisible name" trick used on some chat
        # platforms) is just as blank-looking as the whitespace-only,
        # zero-width-only, control-character-only, and variation-selector-
        # only cases already caught above, but slips past _is_blank()
        # through a gap none of those fixes closed: U+3164 (and its
        # siblings U+115F, U+1160, U+FFA0) exist purely as placeholder jamo
        # slots for composing a Hangul syllable block and render with no
        # glyph of their own -- exactly as invisible as a Cf character or a
        # variation selector -- but the Unicode Character Database files
        # them under general category 'Lo' (letter, other), the same
        # category as an ordinary visible letter. _is_blank() only ever
        # treated a character as blank via `str.isspace()`, category 'Cf',
        # or the variation-selector ranges, so it saw three "real"
        # (non-blank) characters here and the required-key check passed.
        # The post then built successfully with a `<title>`/`<h1>` and an
        # index link that are all present in the markup but carry no
        # visible or accessible text at all -- the same failure mode as the
        # four prior fixes, just via a fifth character class none of them
        # checked for.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: "ㅤㅤㅤ"\ndate: 2026-01-01\n---\n'
                'Body with a Hangul-filler-only title.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("title", str(ctx.exception))

    def test_combining_grapheme_joiner_only_title_names_the_file(self):
        # A title made entirely of U+034F COMBINING GRAPHEME JOINER is just
        # as blank-looking as the whitespace-only, zero-width-only,
        # control-character-only, variation-selector-only, and
        # Hangul-filler-only cases already caught above, but slips past
        # _is_blank() through a gap none of those fixes closed: CGJ exists
        # only to block otherwise-automatic combining/ligating behavior
        # between the two characters on either side of it, and renders as
        # nothing at all when it appears with no such neighbors -- exactly
        # as invisible as a Cf character, a variation selector, or a
        # Hangul filler -- but the Unicode Character Database files it
        # under general category 'Mn' (nonspacing mark), the same category
        # the variation-selector check above already had to look past once
        # since 'Mn' also holds ordinary diacritics that render visibly on
        # their own. _is_blank() only ever treated a character as blank via
        # `str.isspace()`, category 'Cf', the variation-selector ranges, or
        # the four explicit Hangul filler code points, so it saw three
        # "real" (non-blank) characters here and the required-key check
        # passed. The post then built successfully with a `<title>`/`<h1>`
        # and an index link that are all present in the markup but carry no
        # visible or accessible text at all -- the same failure mode as the
        # five prior fixes, just via a sixth character class none of them
        # checked for.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: "͏͏͏"\ndate: 2026-01-01\n---\n'
                'Body with a combining-grapheme-joiner-only title.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("title", str(ctx.exception))

    def test_mongolian_free_variation_selector_only_title_names_the_file(self):
        # Same root cause as the combining-grapheme-joiner case directly
        # above -- U+180B/U+180C/U+180D/U+180F, the Mongolian free variation
        # selectors one through four, are siblings of the U+FE00-U+FE0F
        # variation-selector block already checked, just a separate Unicode
        # block used to pick a glyph variant for Mongolian script instead of
        # CJK, and equally invisible with no base character to modify. Also
        # category 'Mn', so _is_blank() missed it the same way.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: "᠋᠌᠍᠏"\ndate: 2026-01-01\n---\n'
                'Body with a Mongolian-free-variation-selector-only title.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("title", str(ctx.exception))

    def test_khmer_inherent_vowel_only_title_names_the_file(self):
        # Same root cause again -- U+17B4/U+17B5, the Khmer inherent vowel
        # signs AQ/AA, exist only to override a consonant's own default
        # inherent vowel and render as nothing when they appear with no
        # consonant to modify. Also category 'Mn', so _is_blank() missed it
        # the same way as the combining-grapheme-joiner and Mongolian cases
        # above.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_text(
                '---\ntitle: "឴឵"\ndate: 2026-01-01\n---\n'
                'Body with a Khmer-inherent-vowel-only title.\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))
            self.assertIn("title", str(ctx.exception))

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

    def test_non_utf8_file_names_the_file(self):
        # A post accidentally saved with a stray non-UTF-8 byte (e.g. a
        # Windows-1252 smart quote pasted from a word processor) raised a
        # bare UnicodeDecodeError with no file path in its message --
        # "'utf-8' codec can't decode byte 0xff in position 68: invalid
        # start byte" -- unlike every other failure mode in this function,
        # which all deliberately prefix `path` for exactly this reason.
        # UnicodeDecodeError is itself a ValueError subclass, so the
        # exception *type* a caller sees was never the problem; with 100+
        # real posts, it's the missing path in the *message* that turns a
        # one-file typo into a build failure nobody can immediately place.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "bad.md"
            path.write_bytes(
                b'---\ntitle: "Bad Bytes"\ndate: 2026-01-01\n---\n'
                b"Body with a stray byte: \xff here.\n"
            )
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_post(path)
            self.assertIn(str(path), str(ctx.exception))

    def test_quoted_value_padded_with_whitespace_inside_the_quotes_is_trimmed(self):
        # session 146 fixed a *whitespace-only* quoted value (`title: "   "`)
        # slipping past the required-key check as truthy. The narrower
        # sibling gap that fix left open: a quoted value that isn't blank,
        # just *padded* -- real content with extra leading/trailing
        # whitespace still sitting inside the quotes (e.g. `title: "  Real
        # Title  "`). That passes the required-key check fine (it's
        # non-empty), but the padding itself was never stripped, because the
        # one `.strip()` call that runs before the quote characters are
        # sliced off only ever reaches whitespace *outside* the quoted pair.
        # The exact same title written *without* quotes comes out clean
        # (the pre-quote strip handles that case), so quoting a title was
        # the one way to make its padding survive into <title>/<h1>/the
        # index link text/the feed's own <title> -- an inconsistency with no
        # good reason, and a real, visible defect in the shipped content.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "2026-01-01-padded.md"
            path.write_text(
                '---\ntitle: "  Real Title  "\ndate: 2026-01-01\n---\nBody.\n',
                encoding="utf-8",
            )
            post = build_site.parse_post(path)
            self.assertEqual(post["title"], "Real Title")

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

    def test_quoted_value_with_escaped_quote_is_unescaped(self):
        # A quoted title that needs to contain a literal quote mark has no
        # way to say so except escaping it with a backslash (`\"`) -- the
        # same convention JSON/YAML-style quoted strings use, and the one a
        # real post in this journal actually reached for:
        # `title: "A deck named \".\" blamed the wrong thing"`, meant to
        # render as `A deck named "." blamed the wrong thing`. But the
        # quote-stripping above (`value[1:-1].strip()`) only ever removes
        # the outer wrapping pair; it has no notion of backslash escapes at
        # all, so every `\"` inside survived untouched -- the stored title
        # kept its literal backslashes, `A deck named \".\" blamed the
        # wrong thing`, and that garbled text reached <title>, <h1>, the
        # index link, and the feed entry for that real, live post.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "2026-01-01-escaped-quote.md"
            path.write_text(
                '---\ntitle: "A deck named \\".\\" blamed the wrong thing"\n'
                "date: 2026-01-01\n---\nBody.\n",
                encoding="utf-8",
            )
            post = build_site.parse_post(path)
            self.assertEqual(post["title"], 'A deck named "." blamed the wrong thing')

    def test_control_character_in_title_and_body_is_stripped(self):
        # render_feed() strips characters XML 1.0 forbids (control bytes,
        # lone surrogates, U+FFFE/U+FFFF -- see _strip_invalid_xml_chars())
        # from a post's title/body before they reach feed.xml, so a stray
        # control byte (e.g. an ESC from a pasted terminal log) can't break
        # the feed. But parse_post() -- the single place every post's raw
        # title/body first gets read -- never applied that same rule, so the
        # actual HTML pages (each post's own <title>/<h1>/<meta
        # description>, and the index page's listing, all built straight
        # from parse_post()'s output without going anywhere near
        # render_feed()) shipped the raw, un-sanitized control byte, even
        # though the feed for the exact same post was clean.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "2026-01-01-esc-post.md"
            path.write_text(
                '---\ntitle: "Session log \x1b[31mERROR\x1b[0m recap"\ndate: 2026-01-01\n'
                "---\nBody with a stray \x1b control byte in it.\n",
                encoding="utf-8",
            )
            post = build_site.parse_post(path)
            self.assertNotIn("\x1b", post["title"])
            self.assertNotIn("\x1b", post["body"])

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

    def test_commit_time_does_not_borrow_an_unrelated_same_date_posts_history(self):
        # _first_commit_time() runs `git log --follow -- <path>` to find when
        # a post was actually first committed, used to order same-date posts
        # (see TestBuildOrdersSameDatePostsByCommitTime below). --follow is
        # meant only to carry a file's history across a later rename of that
        # *same* file, but git's rename/copy detector (on by default under
        # --follow, ~50% similarity threshold) will happily pair an added
        # file with any other unrelated, untouched file already in the tree
        # that just happens to be similar enough -- and reports that pairing
        # as if the new file had been renamed from the old one. Two short
        # posts sharing this journal's own frontmatter boilerplate, differing
        # only in title and one line of body, are exactly the kind of pair
        # likely to cross that threshold by accident -- most plausible of all
        # for two posts filed under the same date, precisely the case this
        # sort key exists to order correctly. `_first_commit_time` takes the
        # *last* line of `git log --follow`'s output as "the" first commit;
        # with the false pairing, that silently became the *other* post's
        # earlier commit time, not this post's own.
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)

            def git(*args):
                subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

            def commit(relpath, content, at):
                path = repo / relpath
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
                git("add", relpath)
                run_env = {**os.environ, "GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at}
                subprocess.run(
                    ["git", "commit", "-q", "-m", relpath],
                    cwd=repo, check=True, capture_output=True, text=True, env=run_env,
                )
                return path

            git("init", "-q")
            git("config", "user.email", "test@example.test")
            git("config", "user.name", "Test")
            commit(
                "posts/2026-08-09-alpha.md",
                '---\ntitle: "Alpha"\ndate: 2026-08-09\n---\n'
                "Something happened today worth writing down in full detail.\n",
                "2026-08-09T09:00:00Z",
            )
            beta_path = commit(
                "posts/2026-08-09-beta.md",
                '---\ntitle: "Beta"\ndate: 2026-08-09\n---\n'
                "Something happened today worth writing down in full detail.\n",
                "2026-08-09T15:00:00Z",
            )

            orig_repo_root = build_site.REPO_ROOT
            try:
                build_site.REPO_ROOT = repo
                post = build_site.parse_post(beta_path)
            finally:
                build_site.REPO_ROOT = orig_repo_root

            self.assertEqual(post["commit_time"], "2026-08-09T15:00:00Z")


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

    def test_blank_title_line_raises(self):
        # A "# " line with nothing after it passes the `startswith("# ")`
        # check just fine -- the same gap parse_post() closed for a post's
        # own blank title, with `not meta.get(required)` originally missing
        # it entirely. parse_charter() never got the equivalent check at
        # all: title_line[2:].strip() silently produced an empty string,
        # which then reached page()'s <title>/<h1> as charter.html's own
        # completely blank, invisible-to-a-reader title -- the identical
        # "every page carries this tag" failure already fixed three times
        # over for parse_post()'s title, just never applied to this
        # sibling function that builds the exact same kind of page.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "CHARTER.md"
            path.write_text("# \n\nRule one.\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_charter(path)
            self.assertIn(str(path), str(ctx.exception))

    def test_zero_width_only_title_line_raises(self):
        # Same gap as test_blank_title_line_raises, through the Unicode
        # angle parse_post()'s own title check already guards against
        # (three zero-width spaces are non-whitespace, non-empty
        # characters that still render as nothing -- see _is_blank()).
        # parse_charter() ran no equivalent check at all, so this title
        # sailed straight through to a charter.html with a <title>/<h1>
        # that are present in the markup but carry no visible or
        # accessible text.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "CHARTER.md"
            path.write_text("# \u200b\u200b\u200b\n\nRule one.\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_charter(path)
            self.assertIn(str(path), str(ctx.exception))

    def test_non_utf8_file_names_the_file(self):
        # Same gap as parse_post()'s own non-UTF-8 handling: a raw
        # UnicodeDecodeError never names the file it choked on.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "CHARTER.md"
            path.write_bytes(b"# Charter\n\nRule with a stray byte: \xff here.\n")
            with self.assertRaises(ValueError) as ctx:
                build_site.parse_charter(path)
            self.assertIn(str(path), str(ctx.exception))

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


class TestCommitSortKeyOrdersByUtcInstant(unittest.TestCase):
    """(date, commit_time) used to sort same-date posts by comparing
    `commit_time` -- git's strict-ISO-8601 author date, e.g.
    "2026-08-08T23:30:00+09:00" -- as a plain string. Two commits on the
    same calendar date but authored under different UTC offsets don't
    compare correctly that way: "2026-08-08T23:30:00+09:00" (14:30 UTC)
    sorts *after* "2026-08-08T08:00:00-07:00" (15:00 UTC) as text, purely
    because "23" > "08" as characters, even though the second commit
    happened later in real time."""

    def test_same_date_posts_ordered_by_utc_instant_not_offset_string(self):
        orig_repo_root = build_site.REPO_ROOT
        orig_posts_dir = build_site.POSTS_DIR
        orig_static_dir = build_site.STATIC_DIR
        orig_charter_path = build_site.CHARTER_PATH
        try:
            with tempfile.TemporaryDirectory() as d:
                repo = Path(d)
                posts_dir = repo / "posts"
                posts_dir.mkdir()
                static_dir = repo / "static"
                static_dir.mkdir()
                (static_dir / "favicon.svg").write_text("<svg></svg>", encoding="utf-8")
                (repo / "CHARTER.md").write_text("# Charter\n\nA rule.\n", encoding="utf-8")

                def git(*args, env=None):
                    subprocess.run(
                        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, env=env
                    )

                git("init", "-q")
                git("config", "user.email", "test@example.test")
                git("config", "user.name", "Test")

                def commit(relpath, content, at):
                    path = repo / relpath
                    path.write_text(content, encoding="utf-8")
                    git("add", relpath)
                    run_env = {**os.environ, "GIT_AUTHOR_DATE": at, "GIT_COMMITTER_DATE": at}
                    git("commit", "-q", "-m", relpath, env=run_env)

                # Authored 23:30 under UTC+9 -> 14:30 UTC: the earlier real
                # instant, but the lexicographically *larger* offset string.
                commit(
                    "posts/2026-08-08-earlier-instant.md",
                    '---\ntitle: "Earlier Instant"\ndate: 2026-08-08\n---\nWritten first.\n',
                    "2026-08-08T23:30:00+09:00",
                )
                # Authored 08:00 under UTC-7 -> 15:00 UTC: the later real
                # instant, but the lexicographically *smaller* offset string.
                commit(
                    "posts/2026-08-08-later-instant.md",
                    '---\ntitle: "Later Instant"\ndate: 2026-08-08\n---\nWritten second.\n',
                    "2026-08-08T08:00:00-07:00",
                )

                build_site.REPO_ROOT = repo
                build_site.POSTS_DIR = posts_dir
                build_site.STATIC_DIR = static_dir
                build_site.CHARTER_PATH = repo / "CHARTER.md"

                out = repo / "_site"
                build_site.build(out)
                index = (out / "index.html").read_text(encoding="utf-8")
        finally:
            build_site.REPO_ROOT = orig_repo_root
            build_site.POSTS_DIR = orig_posts_dir
            build_site.STATIC_DIR = orig_static_dir
            build_site.CHARTER_PATH = orig_charter_path

        # Newest-first: the later real-world instant must come first, no
        # matter which commit's raw offset string sorts first as text.
        # Matched against the actual <li> post-list entry specifically
        # (not a bare substring search), since "the first one" start-here
        # link elsewhere on the page also references whichever post sorts
        # oldest and would otherwise produce a false match.
        self.assertLess(
            index.index('<li><a href="posts/2026-08-08-later-instant.html">'),
            index.index('<li><a href="posts/2026-08-08-earlier-instant.html">'),
        )


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

    def test_404_page_has_a_description_too(self):
        # index/post/charter each pass page() a description; the 404 page's
        # own page() call (build_site.build()'s not_found_body branch) never
        # got one, so it silently lacks the <meta name="description"> tag
        # every other page in the site carries -- the same "every page gets
        # X" consistency this project already enforces for the favicon link
        # (see TestBuildIncludesFavicon, which explicitly checks 404.html
        # too) was never extended to the description meta tag.
        with tempfile.TemporaryDirectory() as d:
            build_site.build(d)
            not_found = (Path(d) / "404.html").read_text(encoding="utf-8")

        self.assertIn('<meta name="description" content=', not_found)


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


class TestBuildStripsControlCharactersFromHtmlOutput(unittest.TestCase):
    """render_feed() already strips characters XML 1.0 forbids from a post's
    title/body before they reach feed.xml; parse_post() didn't apply that
    same rule, so the actual built HTML (the post's own page and the index
    listing) shipped the raw control byte even though the feed for the same
    post was clean. Confirms the fix at the level a reader would actually
    notice it: the real built pages, not just parse_post()'s return value."""

    def test_control_character_does_not_reach_post_page_or_index(self):
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
                (posts_dir / "2026-01-01-esc-post.md").write_text(
                    '---\ntitle: "Session log \x1b[31mERROR\x1b[0m recap"\ndate: 2026-01-01\n'
                    "---\nBody with a stray \x1b control byte in it.\n",
                    encoding="utf-8",
                )

                build_site.POSTS_DIR = posts_dir
                build_site.STATIC_DIR = static_dir
                build_site.CHARTER_PATH = d / "CHARTER.md"

                out = d / "_site"
                build_site.build(out)
                index = (out / "index.html").read_text(encoding="utf-8")
                post_page = (out / "posts" / "2026-01-01-esc-post.html").read_text(encoding="utf-8")
        finally:
            build_site.POSTS_DIR = orig_posts_dir
            build_site.STATIC_DIR = orig_static_dir
            build_site.CHARTER_PATH = orig_charter_path

        self.assertNotIn("\x1b", index)
        self.assertNotIn("\x1b", post_page)


class TestBuildRemovesStalePostPages(unittest.TestCase):
    """index.html, feed.xml, charter.html, and 404.html are all rewritten in
    full on every build, so they can never carry stale content. A post page
    used to be different: build() only ever wrote a page for each *current*
    source file, never removed one for a source file that's gone -- so
    renaming or deleting a post and rebuilding into the same output
    directory (the ordinary local workflow: `python3 tools/build_site.py`
    writes into `_site/` every time, and README.md says to open
    `_site/index.html` directly, nothing about wiping it first) left the
    old post's page sitting on disk indefinitely: unlinked from the index
    and feed, but still live at its old URL with its last-rendered content.
    deploy.sh's own rsync passes happen to clean this up in production
    (`--delete-delay`), but that's the deploy pipeline's job, not this
    script's, and nothing plays that role for a local rebuild."""

    def test_removed_posts_source_file_orphaned_page_is_deleted_on_rebuild(self):
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
                first = posts_dir / "2026-01-01-first-post.md"
                first.write_text(
                    '---\ntitle: "First post"\ndate: 2026-01-01\n---\nHello, first post.\n',
                    encoding="utf-8",
                )
                (posts_dir / "2026-01-02-second-post.md").write_text(
                    '---\ntitle: "Second post"\ndate: 2026-01-02\n---\nHello, second post.\n',
                    encoding="utf-8",
                )

                build_site.POSTS_DIR = posts_dir
                build_site.STATIC_DIR = static_dir
                build_site.CHARTER_PATH = d / "CHARTER.md"

                out = d / "_site"
                build_site.build(out)
                first_page = out / "posts" / "2026-01-01-first-post.html"
                second_page = out / "posts" / "2026-01-02-second-post.html"
                self.assertTrue(first_page.exists())
                self.assertTrue(second_page.exists())

                # The first post's source file is renamed/deleted, then the
                # site is rebuilt into the SAME output directory -- no
                # `rm -rf _site/` in between, matching what README.md
                # actually documents as the local build workflow.
                first.unlink()
                build_site.build(out)
                first_page_still_exists = first_page.exists()
                second_page_still_exists = second_page.exists()
        finally:
            build_site.POSTS_DIR = orig_posts_dir
            build_site.STATIC_DIR = orig_static_dir
            build_site.CHARTER_PATH = orig_charter_path

        self.assertFalse(first_page_still_exists)
        self.assertTrue(second_page_still_exists)


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

    def test_empty_string_argument_is_rejected_not_treated_as_current_dir(self):
        # Pre-fix, an empty string doesn't start with "-", so it fell
        # straight through to `return arg` -- and `Path("")` is `Path(".")`,
        # the current directory, not "no directory." A caller that builds
        # the output-dir argument itself (e.g. `build_site.py "$OUT_DIR"`
        # with `$OUT_DIR` unset) silently built the whole site into
        # whatever directory the script was run from -- for the common
        # case of running it from the repo root, overwriting index.html/
        # charter.html/feed.xml/404.html at the top level and dropping
        # every post's rendered .html right next to its .md source inside
        # posts/ itself, with no error and exit code 0.
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(stderr):
                build_site._resolve_output_dir(["build_site.py", ""])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("blank", stderr.getvalue())

    def test_whitespace_only_argument_is_rejected(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            with contextlib.redirect_stderr(stderr):
                build_site._resolve_output_dir(["build_site.py", "   "])
        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("blank", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
