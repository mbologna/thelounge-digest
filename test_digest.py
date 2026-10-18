"""Tests for digest.py — run with: python -m unittest test_digest -v"""

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, time
from unittest import mock

# digest.py reads env vars and opens files at import time only inside main(),
# so a plain import is safe.
import digest


def _make_db(rows):
    """Return an in-memory SQLite connection pre-populated with message rows.

    rows: list of (id, network, channel, time_ms, type_, nick, text)
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, network TEXT, channel TEXT, time INTEGER, type TEXT, msg TEXT)"
    )
    conn.executemany(
        "INSERT INTO messages VALUES (?,?,?,?,?,?)",
        [
            (r[0], r[1], r[2], r[3], r[4], json.dumps({"from": {"nick": r[5]}, "text": r[6], "self": False}))
            for r in rows
        ],
    )
    conn.commit()
    return conn


class TestFetchMessages(unittest.TestCase):
    _NET = "uuid-net"
    _CHAN = "#test"
    _BASE_MS = 1_700_000_000_000

    def _rows(self):
        return [
            # id, network, channel, time_ms, type, nick, text
            (1, self._NET, self._CHAN, self._BASE_MS - 10_000_000, "message", "alice", "old"),
            (2, self._NET, self._CHAN, self._BASE_MS - 1_000, "message", "bob", "recent"),
            (3, self._NET, self._CHAN, self._BASE_MS, "message", "carol", "newest"),
        ]

    def test_since_ms_filters_old_messages(self):
        conn = _make_db(self._rows())
        # Set since_ms between old and recent — should exclude id=1.
        cutoff = self._BASE_MS - 5_000_000
        msgs = digest.fetch_messages(conn, self._CHAN, 0, network_uuid=self._NET, since_ms=cutoff)
        ids = [m["id"] for m in msgs]
        self.assertNotIn(1, ids)
        self.assertIn(2, ids)
        self.assertIn(3, ids)

    def test_since_ms_zero_returns_all(self):
        conn = _make_db(self._rows())
        msgs = digest.fetch_messages(conn, self._CHAN, 0, network_uuid=self._NET, since_ms=0)
        self.assertEqual(len(msgs), 3)

    def test_last_id_still_respected_with_since_ms(self):
        conn = _make_db(self._rows())
        # last_id=1 + since_ms well in the past → only ids 2 and 3
        msgs = digest.fetch_messages(conn, self._CHAN, 1, network_uuid=self._NET, since_ms=0)
        ids = [m["id"] for m in msgs]
        self.assertNotIn(1, ids)
        self.assertIn(2, ids)

    def test_lookback_hours_default_is_24(self):
        self.assertEqual(digest.DIGEST_LOOKBACK_HOURS, 24)


class TestMaxIdAfter(unittest.TestCase):
    _NET = "uuid-net"
    _CHAN = "#test"
    _BASE_MS = 1_700_000_000_000

    def _rows(self):
        return [
            (1, self._NET, self._CHAN, self._BASE_MS - 10_000_000, "message", "alice", "old"),
            (2, self._NET, self._CHAN, self._BASE_MS - 1_000, "message", "bob", "recent"),
            (3, self._NET, self._CHAN, self._BASE_MS, "message", "carol", "newest"),
        ]

    def test_returns_highest_id_regardless_of_time(self):
        conn = _make_db(self._rows())
        self.assertEqual(digest.max_id_after(conn, self._CHAN, 0, network_uuid=self._NET), 3)

    def test_respects_last_id_floor(self):
        conn = _make_db(self._rows())
        self.assertEqual(digest.max_id_after(conn, self._CHAN, 2, network_uuid=self._NET), 3)

    def test_none_when_nothing_after_last_id(self):
        conn = _make_db(self._rows())
        self.assertIsNone(digest.max_id_after(conn, self._CHAN, 3, network_uuid=self._NET))

    def test_falls_back_to_channel_only_without_uuid(self):
        conn = _make_db(self._rows())
        self.assertEqual(digest.max_id_after(conn, self._CHAN, 0), 3)


class TestExtractUrls(unittest.TestCase):
    def test_plain_url(self):
        self.assertEqual(digest.extract_urls("https://example.com"), ["https://example.com"])

    def test_url_mid_sentence(self):
        self.assertEqual(
            digest.extract_urls("check https://example.com out"),
            ["https://example.com"],
        )

    def test_multiple_urls(self):
        result = digest.extract_urls("https://a.com and http://b.org/path")
        self.assertEqual(result, ["https://a.com", "http://b.org/path"])

    def test_non_http_scheme_ignored(self):
        self.assertEqual(digest.extract_urls("ftp://example.com"), [])

    def test_empty_string(self):
        self.assertEqual(digest.extract_urls(""), [])

    def test_trailing_period_stripped(self):
        self.assertEqual(digest.extract_urls("See https://example.com."), ["https://example.com"])

    def test_trailing_punctuation_variants(self):
        cases = [
            ("https://example.com,", "https://example.com"),
            ("https://example.com!", "https://example.com"),
            ("(https://example.com)", "https://example.com"),
            ("https://example.com/path?q=1.", "https://example.com/path?q=1"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(digest.extract_urls(raw), [expected])


class TestStripIrcFormatting(unittest.TestCase):
    def test_bold(self):
        self.assertEqual(digest.strip_irc_formatting("\x02bold\x02"), "bold")

    def test_color_code_no_params(self):
        self.assertEqual(digest.strip_irc_formatting("\x03text"), "text")

    def test_color_code_with_fg(self):
        self.assertEqual(digest.strip_irc_formatting("\x034red\x03"), "red")

    def test_color_code_with_fg_and_bg(self):
        self.assertEqual(digest.strip_irc_formatting("\x034,8text\x03"), "text")

    def test_reset(self):
        self.assertEqual(digest.strip_irc_formatting("a\x0fb"), "ab")

    def test_italic(self):
        self.assertEqual(digest.strip_irc_formatting("\x1ditalic\x1d"), "italic")

    def test_mixed(self):
        self.assertEqual(
            digest.strip_irc_formatting("\x02\x034,2hello\x03 world\x0f"),
            "hello world",
        )

    def test_plain_text_unchanged(self):
        self.assertEqual(digest.strip_irc_formatting("hello world"), "hello world")


class TestNickColor(unittest.TestCase):
    def test_palette_has_32_entries(self):
        self.assertEqual(len(digest._NICK_COLORS), 32)

    def test_matches_thelounge_algorithm(self):
        # TheLounge: 1 + (sum_of_charCodes % 32) → color-N (colorClass.ts)
        # Our mapping: _NICK_COLORS[sum(ord) % 32]
        for nick in ["alice", "bob", "Michele", "ChanServ", "foobar"]:
            expected = digest._NICK_COLORS[sum(ord(c) for c in nick) % 32]
            self.assertEqual(digest.nick_color(nick), expected)

    def test_deterministic_across_all_32_slots(self):
        # 32 nicks that cover all slots must each get a valid colour.
        seen_colors = set()
        for i in range(32):
            # Craft a nick whose char-sum mod 32 == i.
            # Single char with ord == i + 32 (printable range) works for i < 96.
            nick = chr(i + 32) * 3  # e.g. "   " for i=0, "!!!" for i=1 …
            color = digest.nick_color(nick)
            self.assertIn(color, digest._NICK_COLORS)
            seen_colors.add(color)
        # All 32 slots produce valid (not necessarily unique) colours.
        self.assertGreater(len(seen_colors), 0)


class TestBuildNickColors(unittest.TestCase):
    def _run(self, nicks):
        """Helper: build color map for a list of nicks via a fake channel."""
        channel_messages = {"Net/#ch": [{"nick": n} for n in nicks]}
        return digest.build_nick_colors([("Net", "#ch")], channel_messages)

    def test_single_nick_gets_a_color(self):
        colors = self._run(["alice"])
        self.assertIn("alice", colors)
        self.assertIn(colors["alice"], digest._NICK_COLORS)

    def test_same_nick_same_color_across_calls(self):
        c1 = self._run(["bob"])
        c2 = self._run(["bob"])
        self.assertEqual(c1["bob"], c2["bob"])

    def test_many_nicks_all_get_colors(self):
        nicks = [f"user{i}" for i in range(50)]
        colors = self._run(nicks)
        self.assertEqual(set(colors.keys()), set(nicks))
        for nick in nicks:
            self.assertIn(colors[nick], digest._NICK_COLORS)

    def test_colliding_nicks_get_different_colors(self):
        # Both hash to the same _NICK_COLORS index (verified empirically).
        colors = self._run(["Bucciarati", "michele"])
        self.assertNotEqual(colors["Bucciarati"], colors["michele"])

    def test_32_colliding_nicks_all_get_distinct_colors(self):
        # Pad each distinct "user{i}" nick with one extra char so every
        # nick's char-sum % 32 == 0 (all collide on the same preferred slot).
        nicks = []
        for i in range(32):
            base = f"user{i}"
            pad = (-sum(ord(c) for c in base)) % 32
            nicks.append(base + chr(32 + pad))
        self.assertEqual(len({sum(ord(c) for c in n) % 32 for n in nicks}), 1)
        colors = self._run(nicks)
        self.assertEqual(len(set(colors.values())), 32)

    def test_more_nicks_than_palette_does_not_raise(self):
        nicks = [f"user{i}" for i in range(33)]
        colors = self._run(nicks)
        self.assertEqual(set(colors.keys()), set(nicks))
        for nick in nicks:
            self.assertIn(colors[nick], digest._NICK_COLORS)

    def test_bots_excluded(self):
        import os

        old = os.environ.get("DIGEST_BOTS", "")
        try:
            os.environ["DIGEST_BOTS"] = "botty"
            importlib.reload(digest)
            colors = self._run(["alice", "botty"])
            self.assertNotIn("botty", colors)
            self.assertIn("alice", colors)
        finally:
            os.environ["DIGEST_BOTS"] = old
            importlib.reload(digest)


class TestChunkBody(unittest.TestCase):
    def test_short_body_is_single_chunk(self):
        body = "hello\nworld"
        self.assertEqual(digest.chunk_body(body, max_len=100), [body])

    def test_long_body_splits_on_newline(self):
        line = "x" * 100
        body = "\n".join([line] * 10)  # 1009 chars
        chunks = digest.chunk_body(body, max_len=300)
        self.assertGreater(len(chunks), 1)
        # Every chunk must be within the limit.
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 300)
        # Reassembled body must equal original.
        self.assertEqual("\n".join(chunks), body)

    def test_single_long_line_stays_together(self):
        line = "x" * 5000
        chunks = digest.chunk_body(line, max_len=4000)
        # Can't split a line with no newlines — kept as one oversized chunk.
        self.assertEqual(chunks, [line])

    def test_exact_limit_is_single_chunk(self):
        body = "a" * 4000
        self.assertEqual(len(digest.chunk_body(body, max_len=4000)), 1)


class TestWithPreview(unittest.TestCase):
    BASE = "http://apprise/notify/abc"

    def test_adds_preview_yes(self):
        url = digest.with_preview(self.BASE, enabled=True)
        self.assertIn("preview=yes", url)

    def test_adds_preview_no(self):
        url = digest.with_preview(self.BASE, enabled=False)
        self.assertIn("preview=no", url)

    def test_replaces_existing_param(self):
        url = digest.with_preview(self.BASE + "?preview=yes", enabled=False)
        self.assertIn("preview=no", url)
        self.assertEqual(url.count("preview="), 1)

    def test_preserves_other_params(self):
        url = digest.with_preview(self.BASE + "?token=xyz", enabled=True)
        self.assertIn("token=xyz", url)
        self.assertIn("preview=yes", url)


class TestFormatSpan(unittest.TestCase):
    # Anchored to local noon (not a fixed epoch) so a +1h same-day check
    # can't flake across machines in different timezones near midnight.
    _NOW_MS = int(datetime.combine(date.today(), time(12, 0)).timestamp() * 1000)

    def test_same_day_shows_hh_mm_range(self):
        span = digest.format_span(self._NOW_MS, self._NOW_MS + 3_600_000)
        self.assertIn("–", span)
        self.assertNotRegex(span, r"\d{2} \w{3}")  # no date component

    def test_equal_times_show_single_value(self):
        span = digest.format_span(self._NOW_MS, self._NOW_MS)
        self.assertNotIn("–", span)

    def test_different_days_include_dates(self):
        two_weeks_ms = 14 * 24 * 3_600_000
        span = digest.format_span(self._NOW_MS, self._NOW_MS + two_weeks_ms)
        self.assertRegex(span, r"\d{2} \w{3} \d{2}:\d{2}–\d{2} \w{3} \d{2}:\d{2}")

    def test_cross_day_span_is_never_misread_as_reversed(self):
        # A first/last pair where the *later* timestamp happens to have an
        # earlier-looking HH:MM must still be presented unambiguously (i.e.
        # not just "14:01–13:56" with no date to disambiguate).
        first_ms = self._NOW_MS
        last_ms = self._NOW_MS + 14 * 24 * 3_600_000 - 5 * 60_000
        span = digest.format_span(first_ms, last_ms)
        self.assertRegex(span, r"\d{2} \w{3}")


class TestFormatChannelSection(unittest.TestCase):
    _NOW_MS = 1_700_000_000_000  # fixed base timestamp

    def _msg(self, nick="alice", text="hello", type_="message", self_=False, offset_ms=0):
        return {
            "id": 1,
            "time": self._NOW_MS + offset_ms,
            "type": type_,
            "target": "#test",
            "nick": nick,
            "text": text,
            "self": self_,
        }

    def _render(self, messages, nick_colors=None):
        if nick_colors is None:
            nick_colors = {"alice": "🔴", "bob": "🟢"}
        return digest.format_channel_section("Net", "#test", messages, nick_colors)

    def test_regular_message_contains_nick_and_text(self):
        out = self._render([self._msg()])
        self.assertIn("<b>alice</b>", out)
        self.assertIn("hello", out)

    def test_action_is_italicised(self):
        out = self._render([self._msg(type_="action", text="waves")])
        self.assertIn("<i>", out)
        self.assertIn("waves", out)

    def test_notice_is_italicised(self):
        out = self._render([self._msg(type_="notice", text="system notice")])
        self.assertIn("<i>", out)
        self.assertIn("system notice", out)

    def test_notice_nick_is_bold(self):
        out = self._render([self._msg(type_="notice")])
        self.assertIn("<b>alice</b>", out)

    def test_channel_header_has_time_range(self):
        msgs = [
            self._msg(text="first", offset_ms=0),
            self._msg(text="last", offset_ms=3_600_000),  # 1 hour later
        ]
        out = self._render(msgs)
        # Header must contain an en-dash span (two different times)
        self.assertIn("–", out)

    def test_channel_header_single_time_when_equal(self):
        out = self._render([self._msg(text="only")])
        # Only one timestamp value (no en-dash span needed)
        self.assertNotIn("–", out)

    def test_topic_shows_pin_emoji(self):
        out = self._render([self._msg(type_="topic", text="new topic")])
        self.assertIn("📌", out)
        self.assertIn("new topic", out)

    def test_kick_shows_boot_emoji(self):
        out = self._render([self._msg(type_="kick", text="baduser")])
        self.assertIn("👢", out)
        self.assertIn("baduser", out)

    def test_self_message_has_arrow(self):
        out = self._render([self._msg(self_=True)])
        self.assertIn("→", out)

    def test_html_special_chars_escaped(self):
        out = self._render([self._msg(text="<script>alert(1)</script>")])
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_gap_repeats_timestamp(self):
        gap = digest._GAP_MS + 1
        msgs = [
            self._msg(text="first"),
            self._msg(text="second", offset_ms=gap),
        ]
        out = self._render(msgs)
        # The timestamp line appears for both the group header and the gap.
        self.assertGreaterEqual(out.count("<code>"), 2)

    def test_no_gap_no_extra_timestamp(self):
        msgs = [
            self._msg(text="first"),
            self._msg(text="second", offset_ms=60_000),  # 1 minute — no gap
        ]
        out = self._render(msgs)
        # Only one timestamp in the group header.
        self.assertEqual(out.count("<code>"), 1)

    def test_bot_uses_robot_emoji_and_code_nick(self):
        import os

        old = os.environ.get("DIGEST_BOTS", "")
        try:
            os.environ["DIGEST_BOTS"] = "mybot"
            importlib.reload(digest)
            out = self._render([self._msg(nick="mybot", text="I am a bot")])
            self.assertIn("🤖", out)
            self.assertIn("<code>mybot</code>", out)
            # Bot nick must NOT be bold
            self.assertNotIn("<b>mybot</b>", out)
        finally:
            os.environ["DIGEST_BOTS"] = old
            importlib.reload(digest)

    def test_bot_body_is_italic(self):
        import os

        old = os.environ.get("DIGEST_BOTS", "")
        try:
            os.environ["DIGEST_BOTS"] = "mybot"
            importlib.reload(digest)
            out = self._render([self._msg(nick="mybot", text="bot output")])
            self.assertIn("<i>bot output</i>", out)
        finally:
            os.environ["DIGEST_BOTS"] = old
            importlib.reload(digest)


class TestFormatTrendingSection(unittest.TestCase):
    def test_single_url_rendered_as_link(self):
        out = digest.format_trending_section({"https://example.com": {"Net/#a", "Net/#b"}})
        self.assertIn('<a href="https://example.com">https://example.com</a>', out)
        self.assertIn("#a", out)
        self.assertIn("#b", out)

    def test_channel_names_are_sorted(self):
        out = digest.format_trending_section({"https://example.com": {"Net/#z", "Net/#a"}})
        a_pos, z_pos = out.index("#a"), out.index("#z")
        self.assertLess(a_pos, z_pos)

    def test_urls_are_sorted(self):
        out = digest.format_trending_section(
            {
                "https://z.com": {"Net/#a", "Net/#b"},
                "https://a.com": {"Net/#a", "Net/#b"},
            }
        )
        self.assertLess(out.index("a.com"), out.index("z.com"))


class TestFormatKeywordMatchesSection(unittest.TestCase):
    def _msg(self, nick="alice", text="hello python", target="#test", time_ms=1_700_000_000_000):
        return {"nick": nick, "text": text, "target": target, "time": time_ms}

    def test_includes_channel_nick_and_highlighted_keyword(self):
        out = digest.format_keyword_matches_section([self._msg()], ["python"], {"alice": "🔴"})
        self.assertIn("#test", out)
        self.assertIn("<b>alice</b>", out)
        self.assertIn("<b>python</b>", out)

    def test_uses_channel_avatar_for_consistency(self):
        out = digest.format_keyword_matches_section([self._msg(nick="bob")], ["python"], {"bob": "🟢"})
        self.assertIn("🟢", out)

    def test_falls_back_to_default_avatar_for_unknown_nick(self):
        out = digest.format_keyword_matches_section([self._msg(nick="ghost")], ["python"], {})
        self.assertIn("⚪", out)


class TestHighlightKeywords(unittest.TestCase):
    def test_single_keyword_bolded(self):
        result = digest.highlight_keywords("python is great", ["python"])
        self.assertEqual(result, "<b>python</b> is great")

    def test_case_insensitive(self):
        result = digest.highlight_keywords("Python is great", ["python"])
        self.assertEqual(result, "<b>Python</b> is great")

    def test_multiple_keywords(self):
        result = digest.highlight_keywords("foo and bar", ["foo", "bar"])
        self.assertEqual(result, "<b>foo</b> and <b>bar</b>")

    def test_no_match_unchanged(self):
        result = digest.highlight_keywords("hello world", ["python"])
        self.assertEqual(result, "hello world")

    def test_keyword_mid_word_is_highlighted(self):
        result = digest.highlight_keywords("foobar", ["foo"])
        self.assertEqual(result, "<b>foo</b>bar")

    def test_html_escaped_keyword_matched(self):
        # Input text is already html-escaped; keyword contains a special char
        result = digest.highlight_keywords("say &lt;hello&gt;", ["<hello>"])
        self.assertEqual(result, "say <b>&lt;hello&gt;</b>")


class TestFindTrendingUrls(unittest.TestCase):
    def _msg(self, nick, text, self_=False):
        return {"nick": nick, "text": text, "self": self_}

    def test_url_in_two_channels_is_trending(self):
        channel_messages = {
            "Net/#a": [self._msg("alice", "see https://example.com/x")],
            "Net/#b": [self._msg("bob", "see https://example.com/x")],
        }
        trending = digest.find_trending_urls(channel_messages)
        self.assertIn("https://example.com/x", trending)
        self.assertEqual(trending["https://example.com/x"], {"Net/#a", "Net/#b"})

    def test_url_in_one_channel_is_not_trending(self):
        channel_messages = {
            "Net/#a": [self._msg("alice", "see https://example.com/x")],
        }
        self.assertEqual(digest.find_trending_urls(channel_messages), {})

    def test_own_messages_excluded(self):
        channel_messages = {
            "Net/#a": [self._msg("me", "https://example.com/x", self_=True)],
            "Net/#b": [self._msg("me", "https://example.com/x", self_=True)],
        }
        self.assertEqual(digest.find_trending_urls(channel_messages), {})

    def test_bot_cross_posts_not_counted_as_trending(self):
        old = os.environ.get("DIGEST_BOTS", "")
        try:
            os.environ["DIGEST_BOTS"] = "relaybot"
            importlib.reload(digest)
            channel_messages = {
                "Net/#a": [self._msg("relaybot", "now playing https://example.com/track")],
                "Net/#b": [self._msg("relaybot", "now playing https://example.com/track")],
            }
            self.assertEqual(digest.find_trending_urls(channel_messages), {})
        finally:
            os.environ["DIGEST_BOTS"] = old
            importlib.reload(digest)

    def test_bot_and_human_mix_only_human_channel_counted(self):
        # Bot posts in #a, a human independently posts the same link in #b —
        # still only one qualifying (non-bot) channel, so not trending.
        old = os.environ.get("DIGEST_BOTS", "")
        try:
            os.environ["DIGEST_BOTS"] = "relaybot"
            importlib.reload(digest)
            channel_messages = {
                "Net/#a": [self._msg("relaybot", "https://example.com/track")],
                "Net/#b": [self._msg("carol", "https://example.com/track")],
            }
            self.assertEqual(digest.find_trending_urls(channel_messages), {})
        finally:
            os.environ["DIGEST_BOTS"] = old
            importlib.reload(digest)


class TestFormatSummaryLine(unittest.TestCase):
    def test_single_active_channel_returns_empty(self):
        channel_messages = {"Net/#a": [{"nick": "x"}], "Net/#b": []}
        self.assertEqual(digest.format_summary_line(channel_messages, {}, []), "")

    def test_two_active_channels_shows_total_and_count(self):
        channel_messages = {"Net/#a": [{"nick": "x"}, {"nick": "y"}], "Net/#b": [{"nick": "z"}]}
        summary = digest.format_summary_line(channel_messages, {}, [])
        self.assertIn("3 messages", summary)
        self.assertIn("2 channels", summary)

    def test_singular_message_count(self):
        channel_messages = {"Net/#a": [{"nick": "x"}], "Net/#b": [{"nick": "z"}]}
        summary = digest.format_summary_line(channel_messages, {}, [])
        self.assertIn("2 messages", summary)

    def test_includes_trending_count(self):
        channel_messages = {"Net/#a": [{"nick": "x"}], "Net/#b": [{"nick": "z"}]}
        summary = digest.format_summary_line(channel_messages, {"http://x": {"a", "b"}}, [])
        self.assertIn("1 trending", summary)

    def test_includes_keyword_match_count(self):
        channel_messages = {"Net/#a": [{"nick": "x"}], "Net/#b": [{"nick": "z"}]}
        summary = digest.format_summary_line(channel_messages, {}, [{"nick": "x"}, {"nick": "y"}])
        self.assertIn("2 keyword matches", summary)

    def test_single_keyword_match_is_singular(self):
        channel_messages = {"Net/#a": [{"nick": "x"}], "Net/#b": [{"nick": "z"}]}
        summary = digest.format_summary_line(channel_messages, {}, [{"nick": "x"}])
        self.assertIn("1 keyword match", summary)
        self.assertNotIn("1 keyword matches", summary)


class TestLookbackHoursValidation(unittest.TestCase):
    """DIGEST_LOOKBACK_HOURS is validated at module import time, so it must be
    checked out-of-process to avoid leaving the shared `digest` module (used
    by every other test in this file) in a half-reloaded state."""

    def _import_with_env(self, value):
        env = dict(os.environ, DIGEST_LOOKBACK_HOURS=value, DIGEST_CHANNELS="Net/#chan")
        return subprocess.run(
            [sys.executable, "-c", "import digest"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=env,
            capture_output=True,
            text=True,
        )

    def test_zero_is_rejected(self):
        result = self._import_with_env("0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a positive integer", result.stderr)

    def test_negative_is_rejected(self):
        result = self._import_with_env("-5")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a positive integer", result.stderr)

    def test_positive_is_accepted(self):
        result = self._import_with_env("48")
        self.assertEqual(result.returncode, 0, result.stderr)


class TestPostToAppriseRetry(unittest.TestCase):
    def setUp(self):
        self._sleep_patch = mock.patch.object(digest.time, "sleep")
        self._sleep_patch.start()

    def tearDown(self):
        self._sleep_patch.stop()

    def test_succeeds_without_retry(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        with mock.patch.object(digest.urllib.request, "urlopen", return_value=resp) as m:
            self.assertTrue(digest.post_to_apprise("http://apprise/notify", "t", "b"))
            self.assertEqual(m.call_count, 1)

    def test_retries_on_transient_network_error_then_succeeds(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.__enter__.return_value = resp
        with mock.patch.object(
            digest.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError("cold path"), resp],
        ) as m:
            self.assertTrue(digest.post_to_apprise("http://apprise/notify", "t", "b"))
            self.assertEqual(m.call_count, 2)

    def test_gives_up_after_max_attempts(self):
        with mock.patch.object(
            digest.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("still cold"),
        ) as m:
            self.assertFalse(digest.post_to_apprise("http://apprise/notify", "t", "b"))
            self.assertEqual(m.call_count, digest._APPRISE_POST_MAX_ATTEMPTS)

    def test_http_error_is_not_retried(self):
        with mock.patch.object(
            digest.urllib.request,
            "urlopen",
            side_effect=urllib.error.HTTPError("url", 500, "Internal Server Error", {}, None),
        ) as m:
            self.assertFalse(digest.post_to_apprise("http://apprise/notify", "t", "b"))
            self.assertEqual(m.call_count, 1)


class TestSnapshotDb(unittest.TestCase):
    def test_snapshot_is_independent_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            live_path = os.path.join(tmpdir, "live.sqlite3")
            live = sqlite3.connect(live_path)
            live.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, msg TEXT)")
            live.execute("INSERT INTO messages VALUES (1, 'hello')")
            live.commit()

            snap_path = digest.snapshot_db(live_path)
            try:
                # Rows written after the snapshot must not appear in it
                live.execute("INSERT INTO messages VALUES (2, 'after snapshot')")
                live.commit()
                live.close()

                snap = sqlite3.connect(snap_path)
                rows = snap.execute("SELECT id, msg FROM messages ORDER BY id").fetchall()
                snap.close()
                self.assertEqual(rows, [(1, "hello")])
            finally:
                os.unlink(snap_path)

    def test_missing_db_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = os.path.join(tmpdir, "does-not-exist.sqlite3")
            with self.assertRaises(sqlite3.OperationalError):
                digest.snapshot_db(missing)


if __name__ == "__main__":
    unittest.main()
