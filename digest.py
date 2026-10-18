#!/usr/bin/env python3
"""
thelounge-digest: IRC digest notifier for thelounge sqlite logs.

Reads new messages from configured channels since the last cursor position,
finds trending links (URLs seen in >=2 channels), keyword matches, and
POSTs a formatted digest to an Apprise endpoint.

TheLounge SQLite schema (logs/michele.sqlite3):
  messages(id, network TEXT, channel TEXT, time INTEGER, type TEXT, msg TEXT)
  where msg is JSON: {"from": {"nick": "..."}, "text": "...", "self": bool, ...}
  network is a UUID assigned by TheLounge, not a human-readable name.
  UUID→name mapping is loaded from THELOUNGE_USERS_JSON (users/michele.json)
  so that identically-named channels on different networks stay separate.
"""

import html as html_module
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ---------------------------------------------------------------------------
# Configuration from environment variables
# ---------------------------------------------------------------------------

DIGEST_CHANNELS = os.environ.get("DIGEST_CHANNELS", "")
DIGEST_KEYWORDS = os.environ.get("DIGEST_KEYWORDS", "")
DIGEST_BOTS = {n.strip() for n in os.environ.get("DIGEST_BOTS", "").split(",") if n.strip()}
APPRISE_URL = os.environ.get("APPRISE_URL", "")
THELOUNGE_DB = os.environ.get("THELOUNGE_DB", "/thelounge/logs/michele.sqlite3")
THELOUNGE_USERS_JSON = os.environ.get("THELOUNGE_USERS_JSON", "/thelounge/users/michele.json")
CURSOR_FILE = os.environ.get("CURSOR_FILE", "/data/cursor.json")
try:
    DIGEST_LOOKBACK_HOURS = int(os.environ.get("DIGEST_LOOKBACK_HOURS", "24"))
except ValueError:
    print("Error: DIGEST_LOOKBACK_HOURS must be an integer", file=sys.stderr)
    sys.exit(1)
if DIGEST_LOOKBACK_HOURS <= 0:
    # A zero/negative window pushes the lookback floor to now-or-later,
    # silently excluding every message and making the digest look "empty"
    # with no error — fail loudly instead.
    print("Error: DIGEST_LOOKBACK_HOURS must be a positive integer", file=sys.stderr)
    sys.exit(1)
try:
    # Delay between consecutive Apprise POSTs, so a large digest (many chunks)
    # doesn't trip Telegram's flood control mid-batch.
    APPRISE_POST_DELAY_SECONDS = float(os.environ.get("APPRISE_POST_DELAY_SECONDS", "1.5"))
except ValueError:
    print("Error: APPRISE_POST_DELAY_SECONDS must be a number", file=sys.stderr)
    sys.exit(1)


def load_network_uuids():
    """
    Read users/michele.json and return a dict mapping network name → UUID.

    TheLounge stores messages with the UUID in the `network` column, not the
    human-readable name. We need this mapping to filter by (uuid, channel)
    so that two networks with identically-named channels (e.g. #linux on Libera
    AND on OFTC) stay separate.
    """
    try:
        with open(THELOUNGE_USERS_JSON) as f:
            data = json.load(f)
        return {n["name"]: n["uuid"] for n in data.get("networks", []) if "uuid" in n}
    except (OSError, json.JSONDecodeError, KeyError) as e:
        print(f"Warning: could not read network UUIDs from {THELOUNGE_USERS_JSON}: {e}", file=sys.stderr)
        return {}


def load_cursor():
    """Load cursor dict from CURSOR_FILE, return {} if missing or invalid."""
    if os.path.exists(CURSOR_FILE):
        try:
            with open(CURSOR_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read cursor file {CURSOR_FILE}: {e}", file=sys.stderr)
    return {}


def save_cursor(cursor):
    """Atomically save cursor dict to CURSOR_FILE."""
    cursor_dir = os.path.dirname(CURSOR_FILE) or "."
    os.makedirs(cursor_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=cursor_dir, prefix=".cursor_tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cursor, f)
        os.rename(tmp_path, CURSOR_FILE)
        print(f"Cursor saved to {CURSOR_FILE}")
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise e


def snapshot_db(src_path):
    """
    Copy the live TheLounge DB to a private snapshot file and return its path.

    TheLounge keeps the DB in rollback-journal mode, so any read transaction on
    the live file blocks its writes ("database is locked" in TheLounge's log),
    and blocked writes queue up in TheLounge's heap until it OOMs. A digest run
    holds its connection open for minutes (Apprise POSTs included), so querying
    the live file directly starves TheLounge for the whole run. The sqlite
    backup API instead holds the read lock only for the copy itself (seconds),
    after which all queries hit the snapshot and the live DB is untouched.
    """
    fd, snap_path = tempfile.mkstemp(prefix="thelounge-snapshot-", suffix=".sqlite3")
    os.close(fd)
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(snap_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return snap_path


def fetch_messages(conn, channel, last_id, network_uuid=None, since_ms=0):
    """
    Fetch new messages for a channel since last_id.

    If network_uuid is provided (looked up from users/michele.json), filters by
    both (network, channel) so that identically-named channels on different networks
    are kept separate (e.g. #linux on Libera vs #linux on OFTC).
    Falls back to channel-only filtering when the UUID is unknown.
    since_ms: optional millisecond timestamp floor — messages older than this are
    excluded even if their id > last_id. Used to cap how much backlog a single
    run can pull per channel (DIGEST_LOOKBACK_HOURS). Pass 0 to disable (fetch
    all since last_id).
    Returns all messages including own (self=True) — digest shows full context.
    """
    if network_uuid:
        query = (
            "SELECT id, time, type, channel, msg "
            "FROM messages "
            "WHERE network=? AND channel=? AND type IN ('message','notice','action','topic','kick') "
            "AND id > ? AND time > ? "
            "ORDER BY id ASC"
        )
        cursor = conn.execute(query, (network_uuid, channel, last_id, since_ms))
    else:
        query = (
            "SELECT id, time, type, channel, msg "
            "FROM messages "
            "WHERE channel=? AND type IN ('message','notice','action','topic','kick') "
            "AND id > ? AND time > ? "
            "ORDER BY id ASC"
        )
        cursor = conn.execute(query, (channel, last_id, since_ms))
    rows = cursor.fetchall()
    result = []
    for row in rows:
        try:
            msg_data = json.loads(row[4])
        except (json.JSONDecodeError, TypeError) as e:
            print(f"Warning: skipping message id={row[0]} with invalid JSON: {e}", file=sys.stderr)
            continue
        result.append(
            {
                "id": row[0],
                "time": row[1],
                "type": row[2],
                "target": row[3],
                "nick": msg_data.get("from", {}).get("nick", "?"),
                "text": strip_irc_formatting(msg_data.get("text", "")),
                "self": msg_data.get("self", False),
            }
        )
    return result


def max_id_after(conn, channel, last_id, network_uuid=None):
    """
    Return the highest message id > last_id for a channel, ignoring the
    since_ms lookback floor. Used to advance the cursor past messages that
    fall outside the lookback window, so a stuck/late cursor catches up to
    the current head in one run instead of re-scanning a growing backlog
    (and re-chunking it into an ever-larger digest) every run.
    """
    if network_uuid:
        row = conn.execute(
            "SELECT MAX(id) FROM messages WHERE network=? AND channel=? "
            "AND type IN ('message','notice','action','topic','kick') AND id > ?",
            (network_uuid, channel, last_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(id) FROM messages WHERE channel=? "
            "AND type IN ('message','notice','action','topic','kick') AND id > ?",
            (channel, last_id),
        ).fetchone()
    return row[0]


def extract_urls(text):
    """Extract all http/https URLs from a string."""
    return [u.rstrip(".,;:!?)\"'") for u in re.findall(r"https?://\S+", text)]


def format_time(time_ms):
    """Convert millisecond timestamp to HH:MM string (TZ from environment)."""
    return datetime.fromtimestamp(time_ms / 1000).strftime("%H:%M")


def format_span(t_first_ms, t_last_ms):
    """
    Format a first–last time range, including the date whenever the range
    crosses a calendar day (otherwise "HH:MM–HH:MM" alone would misleadingly
    look reversed, e.g. an 18-day backlog showing as "14:01–13:56").
    """
    first_dt = datetime.fromtimestamp(t_first_ms / 1000)
    last_dt = datetime.fromtimestamp(t_last_ms / 1000)
    if first_dt.date() == last_dt.date():
        t_first, t_last = first_dt.strftime("%H:%M"), last_dt.strftime("%H:%M")
        return t_first if t_first == t_last else f"{t_first}–{t_last}"
    return f"{first_dt.strftime('%d %b %H:%M')}–{last_dt.strftime('%d %b %H:%M')}"


_NICK_COLORS = [
    # color-1…9   → circles
    "🔴",
    "🟠",
    "🟡",
    "🟢",
    "🔵",
    "🟣",
    "🟤",
    "⚫",
    "⚪",
    # color-10…18 → squares
    "🟥",
    "🟧",
    "🟨",
    "🟩",
    "🟦",
    "🟪",
    "🟫",
    "⬛",
    "⬜",
    # color-19…30 → hearts (all 12 distinct heart emoji)
    "❤️",
    "🧡",
    "💛",
    "💚",
    "💙",
    "💜",
    "🖤",
    "🤍",
    "🤎",
    "🩷",
    "🩵",
    "🩶",
    # color-31…32 → diamonds
    "🔶",
    "🔷",
]
_GAP_MS = 10 * 60 * 1000  # repeat timestamp after 10-minute gap within a group

_IRC_CTRL = re.compile(r"\x03(?:\d{1,2}(?:,\d{1,2})?)?|[\x02\x0f\x11\x1d\x1e\x1f]")


def strip_irc_formatting(text):
    """Remove IRC formatting control characters (bold, colour, italic, etc.)."""
    return _IRC_CTRL.sub("", text)


def nick_color(nick):
    """Return the emoji for nick, mirroring TheLounge's color-1…32 assignment.

    TheLounge: 1 + (sum_of_charCodes % 32)  (client/js/helpers/colorClass.ts)
    Python:    sum(ord(c) for c in nick) % 32  → index into _NICK_COLORS
    For ASCII nicks (standard IRC), ord() == charCodeAt() exactly.
    """
    return _NICK_COLORS[sum(ord(c) for c in nick) % 32]


def build_nick_colors(channel_pairs, channel_messages):
    """Build nick→emoji map for all non-bot nicks seen across channels.

    Each nick prefers the emoji given by its TheLounge-parity hash
    (nick_color), but if that slot is already taken by another nick in this
    digest, the next free slot in _NICK_COLORS is used instead so that no two
    nicks share an emoji (unless there are more than 32 distinct nicks).
    """
    colors = {}
    used_indices = set()
    palette_size = len(_NICK_COLORS)
    for network, channel in channel_pairs:
        for msg in channel_messages.get(f"{network}/{channel}", []):
            nick = msg["nick"]
            if nick in colors or nick in DIGEST_BOTS:
                continue
            preferred = sum(ord(c) for c in nick) % palette_size
            index = preferred
            for _ in range(palette_size):
                if index not in used_indices:
                    break
                index = (index + 1) % palette_size
            else:
                index = preferred
            used_indices.add(index)
            colors[nick] = _NICK_COLORS[index]
    return colors


def find_trending_urls(channel_messages, min_channels=2):
    """
    Return {url: {channel_keys}} for URLs seen in >=min_channels channels.

    Excludes own messages and bot accounts (DIGEST_BOTS): a link posted by a
    bot into several channels (e.g. a now-playing/relay bot) isn't organic
    cross-channel interest, just the same bot posting in both.
    """
    url_channels = {}
    for key, msgs in channel_messages.items():
        for msg in msgs:
            if msg["self"] or msg["nick"] in DIGEST_BOTS:
                continue
            for url in extract_urls(msg["text"]):
                url_channels.setdefault(url, set()).add(key)
    return {url: channels for url, channels in url_channels.items() if len(channels) >= min_channels}


def format_summary_line(channel_messages, trending_urls, keyword_matches):
    """
    Build an at-a-glance "N messages across M channels" summary line.

    Returns "" when there's only one active channel — that channel's own
    header already shows its count and time span, so a summary would be
    redundant.
    """
    active_channels = sum(1 for msgs in channel_messages.values() if msgs)
    if active_channels < 2:
        return ""
    total_messages = sum(len(msgs) for msgs in channel_messages.values())
    summary = f"📊 {total_messages} message{'s' if total_messages != 1 else ''} across {active_channels} channels"
    if trending_urls:
        summary += f" · 🔗 {len(trending_urls)} trending"
    if keyword_matches:
        summary += f" · 🔍 {len(keyword_matches)} keyword match{'es' if len(keyword_matches) != 1 else ''}"
    return summary


def format_trending_section(trending_urls):
    """Format the "Trending links" section from {url: {channel_keys}}."""
    lines = ["<b>🔗 Trending links</b>"]
    for url, chans in sorted(trending_urls.items()):
        chan_names = html_module.escape(", ".join(sorted(chans)))
        lines.append(f'• <a href="{url}">{html_module.escape(url)}</a>  <i>({chan_names})</i>')
    return "\n".join(lines)


def format_keyword_matches_section(keyword_matches, keywords, nick_colors):
    """Format the "Keyword matches" section, reusing each nick's channel avatar for consistency."""
    lines = ["<b>🔍 Keyword matches</b>"]
    for msg in keyword_matches:
        time_str = format_time(msg["time"])
        target = html_module.escape(msg["target"])
        nick = html_module.escape(msg["nick"])
        avatar = nick_colors.get(msg["nick"], "⚪")
        text = highlight_keywords(html_module.escape(msg["text"]), keywords)
        lines.append(f"<code>[{target} {time_str}]</code> {avatar} <b>{nick}</b> {text}")
    return "\n".join(lines)


def highlight_keywords(text_escaped, keywords):
    """Bold each matched keyword (case-insensitive) within an already-HTML-escaped string."""
    for kw in keywords:
        text_escaped = re.sub(
            f"({re.escape(html_module.escape(kw))})",
            r"<b>\1</b>",
            text_escaped,
            flags=re.IGNORECASE,
        )
    return text_escaped


def _append_group_body(lines, group, italic):
    """Append a group's message lines, repeating the timestamp after a gap (see _GAP_MS)."""
    for i, msg in enumerate(group):
        if i > 0 and msg["time"] - group[i - 1]["time"] >= _GAP_MS:
            gap = f"<code>{format_time(msg['time'])}</code>"
            lines.append(f"<i>{gap}</i>" if italic else gap)
        text = html_module.escape(msg["text"])
        lines.append(f"<i>{text}</i>" if italic else text)


def format_channel_section(network, channel, messages, nick_colors):
    """Format a channel's messages into an HTML section."""
    count = len(messages)
    span = format_span(min(m["time"] for m in messages), max(m["time"] for m in messages))
    header = f"<b>📢 {html_module.escape(network)}/{html_module.escape(channel)}</b> · <i>{count} message{'s' if count != 1 else ''} · {span}</i>"

    # Group consecutive messages from the same author/type into blocks
    groups = []
    for msg in messages:
        if (
            groups
            and groups[-1][0]["nick"] == msg["nick"]
            and groups[-1][0]["type"] == msg["type"]
            and groups[-1][0]["self"] == msg["self"]
        ):
            groups[-1].append(msg)
        else:
            groups.append([msg])

    lines = [header, ""]
    for group in groups:
        first = group[0]
        nick = html_module.escape(first["nick"])
        time_str = format_time(first["time"])

        is_bot = first["nick"] in DIGEST_BOTS
        avatar = nick_colors.get(first["nick"], "⚪")
        if first["type"] == "topic":
            for msg in group:
                t = format_time(msg["time"])
                lines.append(f"📌 <b>{nick}</b> set the topic · <code>{t}</code>")
                lines.append(html_module.escape(msg["text"]))
        elif first["type"] == "kick":
            for msg in group:
                t = format_time(msg["time"])
                kicked = html_module.escape(msg["text"])
                lines.append(f"👢 <b>{nick}</b> kicked <b>{kicked}</b> · <code>{t}</code>")
        elif is_bot:
            # Bot messages: monospace nick (machine identity), italic body (automated output)
            lines.append(f"🤖 <code>{nick}</code> · <code>{time_str}</code>")
            _append_group_body(lines, group, italic=True)
        elif first["type"] == "action":
            lines.append(f"<i>{avatar} <b>* {nick}</b> · <code>{time_str}</code></i>")
            _append_group_body(lines, group, italic=True)
        elif first["type"] == "notice":
            lines.append(f"<i>{avatar} <b>{nick}</b> · <code>{time_str}</code></i>")
            _append_group_body(lines, group, italic=True)
        elif first["self"]:
            lines.append(f"{avatar} <b>→ {nick}</b> · <code>{time_str}</code>")
            _append_group_body(lines, group, italic=True)
        else:
            lines.append(f"{avatar} <b>{nick}</b> · <code>{time_str}</code>")
            _append_group_body(lines, group, italic=False)

        lines.append("")  # blank line between groups

    return "\n".join(lines).rstrip()


def with_preview(url, enabled: bool) -> str:
    """Return the Apprise URL with the ?preview= parameter forced to yes/no."""
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    params["preview"] = ["yes" if enabled else "no"]
    new_query = urllib.parse.urlencode({k: v[0] for k, v in params.items()})
    return parsed._replace(query=new_query).geturl()


def chunk_body(body, max_len=4000):
    """Split body into chunks of at most max_len characters, breaking on newlines."""
    if len(body) <= max_len:
        return [body]
    chunks = []
    current_lines = []
    current_len = 0
    for line in body.split("\n"):
        line_len = len(line) + 1  # +1 for the joining newline
        if current_len + line_len > max_len and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = line_len
        else:
            current_lines.append(line)
            current_len += line_len
    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


# Retries for transient network errors only (e.g. a cold cross-cluster path).
# An HTTP error response means Apprise was reached and rejected the request,
# so those aren't retried.
_APPRISE_POST_MAX_ATTEMPTS = 3
_APPRISE_POST_RETRY_BACKOFF_SECONDS = 3.0


def post_to_apprise(url, title, body):
    """POST digest to Apprise, retrying on transient network errors. Returns True on success."""
    payload = json.dumps({"title": title, "body": body, "type": "info", "format": "html"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(f"POSTing digest to {url} ...")
    for attempt in range(1, _APPRISE_POST_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                status = resp.status
                print(f"Apprise responded with HTTP {status}")
                return status == 200
        except urllib.error.HTTPError as e:
            print(f"Error: Apprise HTTP {e.code}: {e.reason}", file=sys.stderr)
            return False
        except urllib.error.URLError as e:
            print(
                f"Error: Could not reach Apprise (attempt {attempt}/{_APPRISE_POST_MAX_ATTEMPTS}): {e.reason}",
                file=sys.stderr,
            )
            if attempt < _APPRISE_POST_MAX_ATTEMPTS:
                time.sleep(_APPRISE_POST_RETRY_BACKOFF_SECONDS)
    return False


def main():
    dry_run = "--dry-run" in sys.argv
    init_mode = "--init" in sys.argv

    # --- Parse configuration ---
    if not DIGEST_CHANNELS:
        print("Error: DIGEST_CHANNELS is not set", file=sys.stderr)
        sys.exit(1)
    if not APPRISE_URL and not init_mode and not dry_run:
        print("Error: APPRISE_URL is not set", file=sys.stderr)
        sys.exit(1)

    # Parse channels: "Network/#channel,Network2/#channel2"
    channel_pairs = []
    for pair in DIGEST_CHANNELS.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "/" not in pair:
            print(f"Warning: skipping malformed channel pair: {pair!r}", file=sys.stderr)
            continue
        network, channel = pair.split("/", 1)
        channel_pairs.append((network.strip(), channel.strip()))

    if not channel_pairs:
        print("Error: no valid channel pairs found in DIGEST_CHANNELS", file=sys.stderr)
        sys.exit(1)

    print(f"Configured channels: {channel_pairs}")

    # Parse keywords
    keywords = [k.strip() for k in DIGEST_KEYWORDS.split(",") if k.strip()] if DIGEST_KEYWORDS else []
    print(f"Keywords: {keywords}")

    # --- Load network UUID map ---
    network_uuids = load_network_uuids()
    print(f"Network UUIDs loaded: {list(network_uuids.keys())}")

    # --- Load cursor ---
    cursor = load_cursor()
    print(f"Loaded cursor: {cursor}")

    # --- Snapshot DB and open the snapshot read-only ---
    print(f"Snapshotting database: {THELOUNGE_DB}")
    try:
        snap_path = snapshot_db(THELOUNGE_DB)
    except sqlite3.OperationalError as e:
        print(f"Error: cannot snapshot database {THELOUNGE_DB}: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Snapshot ready: {snap_path}")
    conn = sqlite3.connect(f"file:{snap_path}?mode=ro", uri=True)

    try:
        new_cursor = dict(cursor)  # will be updated on success

        # --- Init mode: prime cursor to current head without sending anything ---
        if init_mode:
            print("Init mode: setting cursor to current head for all channels.")
            for network, channel in channel_pairs:
                key = f"{network}/{channel}"
                uuid = network_uuids.get(network)
                if uuid:
                    row = conn.execute(
                        "SELECT MAX(id) FROM messages WHERE network=? AND channel=?",
                        (uuid, channel),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT MAX(id) FROM messages WHERE channel=?",
                        (channel,),
                    ).fetchone()
                max_id = row[0] or 0
                new_cursor[key] = max_id
                print(f"  {key}: cursor → {max_id}")
            save_cursor(new_cursor)
            print("Done. Next run will start from current position.")
            return

        # --- Fetch messages per channel ---
        channel_messages = {}  # key: "Network/channel", value: list of msg dicts

        # Lookback floor: always applied, so a channel can never dump more than
        # DIGEST_LOOKBACK_HOURS worth of backlog into a single digest — whether
        # that's a brand-new channel's whole history, or a cursor that fell
        # behind because past runs kept failing to deliver (e.g. hit Apprise/
        # Telegram flood limits mid-batch). Anything older than the window is
        # silently skipped and the cursor still advances past it below.
        _lookback_ms = int(datetime.now().timestamp() * 1000) - DIGEST_LOOKBACK_HOURS * 3600 * 1000

        for network, channel in channel_pairs:
            key = f"{network}/{channel}"
            last_id = cursor.get(key, 0)
            uuid = network_uuids.get(network)
            if not uuid:
                print(
                    f"  Warning: no UUID found for network {network!r}, falling back to channel-only filter",
                    file=sys.stderr,
                )
            print(
                f"Fetching {key} (channel={channel!r}, uuid={uuid}) since id={last_id}, lookback {DIGEST_LOOKBACK_HOURS}h ..."
            )
            msgs = fetch_messages(conn, channel, last_id, network_uuid=uuid, since_ms=_lookback_ms)
            print(f"  → {len(msgs)} new messages")
            channel_messages[key] = msgs
            ceiling_id = max_id_after(conn, channel, last_id, network_uuid=uuid)
            if ceiling_id is not None:
                if ceiling_id > (msgs[-1]["id"] if msgs else 0):
                    print(f"  → older-than-lookback backlog present, cursor advances to {ceiling_id}")
                new_cursor[key] = ceiling_id

        # --- Link deduplication (trending) ---
        trending_urls = find_trending_urls(channel_messages)

        # --- Keyword matching ---
        keyword_matches = []
        if keywords:
            for msgs in channel_messages.values():
                for msg in msgs:
                    text_lower = msg["text"].lower()
                    for kw in keywords:
                        if kw.lower() in text_lower:
                            keyword_matches.append(msg)
                            break

        # --- Check if there's anything to report ---
        total_messages = sum(len(msgs) for msgs in channel_messages.values())
        if total_messages == 0 and not trending_urls and not keyword_matches:
            print("Nothing new, skipping")
            sys.exit(0)

        # --- Format digest body ---
        nick_colors = build_nick_colors(channel_pairs, channel_messages)
        sections = []

        summary = format_summary_line(channel_messages, trending_urls, keyword_matches)
        if summary:
            sections.append(summary)

        for network, channel in channel_pairs:
            key = f"{network}/{channel}"
            msgs = channel_messages.get(key, [])
            if msgs:
                sections.append(format_channel_section(network, channel, msgs, nick_colors))

        if trending_urls:
            sections.append(format_trending_section(trending_urls))

        if keyword_matches:
            sections.append(format_keyword_matches_section(keyword_matches, keywords, nick_colors))

        body = "\n\n".join(sections)
        print("--- Digest body ---")
        print(body)
        print("-------------------")

        # --- POST to Apprise (skipped in dry-run mode) ---
        if dry_run:
            print("Dry-run mode: digest not sent and cursor not updated.")
            return

        # Send the digest without link previews (body may contain many URLs and
        # Telegram would pick an arbitrary one). If there are trending links, a
        # second message follows with just the top link so Telegram previews it.
        digest_url = with_preview(APPRISE_URL, enabled=False) if trending_urls else APPRISE_URL
        body_chunks = chunk_body(body)
        for i, chunk in enumerate(body_chunks):
            if i > 0:
                time.sleep(APPRISE_POST_DELAY_SECONDS)
            chunk_title = (
                "The Lounge Digest" if len(body_chunks) == 1 else f"The Lounge Digest ({i + 1}/{len(body_chunks)})"
            )
            success = post_to_apprise(digest_url, chunk_title, chunk)
            if not success:
                print("Error: failed to deliver digest", file=sys.stderr)
                sys.exit(1)

        for url, chans in trending_urls.items():
            time.sleep(APPRISE_POST_DELAY_SECONDS)
            chan_names = ", ".join(sorted(chans))
            preview_body = f'<a href="{url}">{html_module.escape(url)}</a>\n<i>({html_module.escape(chan_names)})</i>'
            preview_success = post_to_apprise(
                with_preview(APPRISE_URL, enabled=True),
                "Trending link",
                preview_body,
            )
            if not preview_success:
                print(f"Warning: failed to deliver trending link preview for {url}", file=sys.stderr)

        # --- Update cursor only on success ---
        save_cursor(new_cursor)
        print("Done.")

    finally:
        conn.close()
        try:
            os.unlink(snap_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
