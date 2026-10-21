# thelounge-digest

[![CI](https://github.com/mbologna/thelounge-digest/actions/workflows/ci.yml/badge.svg)](https://github.com/mbologna/thelounge-digest/actions/workflows/ci.yml)

Periodic IRC digest notifier for [TheLounge](https://thelounge.chat). Reads
new messages directly from TheLounge's sqlite logs since the last run, and
POSTs a formatted HTML digest to an [Apprise](https://github.com/caronc/apprise-api)
endpoint — nick-colored, grouped by consecutive author, with trending links
and keyword highlights pulled out into their own sections.

## How it works

Each run:

1. Takes a sqlite backup-API snapshot of the live TheLounge DB (so it never
   holds a read lock on the file TheLounge itself is writing to).
2. For each configured `Network/#channel`, fetches messages newer than that
   channel's saved cursor, capped to the last `DIGEST_LOOKBACK_HOURS` so a
   stuck cursor or a brand-new channel can never dump its whole history into
   one digest.
3. Finds links posted in ≥2 different channels by a human (not the bot
   accounts, not your own messages) and surfaces them as "trending".
4. Finds messages matching any configured keyword.
5. When more than one channel has new activity, prepends a one-line summary
   (message/channel/trending/keyword counts) so the digest is skimmable at a
   glance.
6. POSTs the digest to Apprise, chunked to stay under Telegram's message size
   limit, with a short delay between chunks to avoid flood control, and
   retries a few times on transient network errors (e.g. a cold cross-cluster
   path) before giving up.
7. Saves the new cursor — only after a successful delivery.

It reads TheLounge's `messages` table directly:

```
messages(id, network TEXT, channel TEXT, time INTEGER, type TEXT, msg TEXT)
```

where `network` is a UUID (not a human-readable name) and `msg` is a JSON
blob. The UUID → network-name mapping comes from TheLounge's own
`users/<user>.json`, so that identically-named channels on different networks
(e.g. `#linux` on two different IRC networks) are kept separate.

## Configuration

All configuration is via environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `DIGEST_CHANNELS` | yes | — | Comma-separated `Network/#channel` pairs, e.g. `Libera/#chan,Libera/##other` |
| `APPRISE_URL` | yes* | — | Apprise notify endpoint to POST the digest to (*not required for `--dry-run`/`--init`) |
| `DIGEST_BOTS` | no | `""` | Comma-separated bot nicks — rendered distinctly and excluded from nick colors and trending-link detection |
| `DIGEST_KEYWORDS` | no | `""` | Comma-separated keywords to highlight/surface in a dedicated section |
| `THELOUNGE_DB` | no | `/thelounge/logs/<user>.sqlite3` | Path to TheLounge's sqlite log DB |
| `THELOUNGE_USERS_JSON` | no | `/thelounge/users/<user>.json` | Path to TheLounge's per-user network config (for the UUID → name mapping) |
| `CURSOR_FILE` | no | `/data/cursor.json` | Where per-channel last-seen message IDs are persisted between runs |
| `DIGEST_LOOKBACK_HOURS` | no | `24` | Max backlog age included in a single digest |
| `APPRISE_POST_DELAY_SECONDS` | no | `1.5` | Delay between consecutive Apprise POSTs (chunked digests, trending-link previews) |

## CLI flags

- `--init` — seed the cursor to the current head of every configured channel
  without sending anything. Run this once before the first scheduled run so
  the first real digest doesn't include the entire channel history.
- `--dry-run` — fetch and render the digest, print it to stdout, but skip the
  Apprise POST and skip saving the cursor.

## Running

```sh
docker run --rm \
  -e DIGEST_CHANNELS="Libera/#chan" \
  -e APPRISE_URL="http://apprise:8000/notify/thelounge_notifications" \
  -v /path/to/thelounge:/thelounge:ro \
  -v /path/to/data:/data \
  ghcr.io/mbologna/thelounge-digest:latest
```

Images are built for linux/amd64 and linux/arm64. A systemd timer running the container with podman
works well for periodic digests.

## Tests

```sh
uv run pytest
```

## Dependency updates

`uv` resolves only package releases at least 7 days old (`exclude-newer` in
`pyproject.toml`), so a compromised release has time to be caught before it
lands in `uv.lock`. A CVE fix released less than 7 days ago needs a temporary
override, e.g. `exclude-newer-package = { pytest = false }` under
`[tool.uv]`; remove it once the release is a week old.
