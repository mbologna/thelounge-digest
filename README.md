# thelounge-digest

[![CI](https://github.com/mbologna/thelounge-digest/actions/workflows/ci.yml/badge.svg)](https://github.com/mbologna/thelounge-digest/actions/workflows/ci.yml)

thelounge-digest sends a periodic digest of IRC channels logged by
[The Lounge](https://thelounge.chat/), for its users who would rather read
a summary than follow the channels live. Each run reads the messages posted
since the previous run from The Lounge's [SQLite](https://sqlite.org/) log
and posts them as one HTML message to an
[Apprise API](https://github.com/caronc/apprise-api) server: nicks
coloured, consecutive lines from one author grouped, links seen in several
channels and keyword matches pulled out into their own sections. Run it on
a schedule.

## What a run does

1. Copies the live database to a snapshot in `/tmp` with SQLite's backup
   API and queries the snapshot. The official The Lounge image keeps its
   database in rollback-journal mode, where a long read on the live file
   would block its writes for the whole run; the copy holds the lock for
   seconds. [thelounge-patched](https://github.com/mbologna/thelounge-patched)
   switches the database to WAL mode, where readers don't block the writer,
   and the run takes the snapshot all the same.
2. For each `Network/#channel` in `DIGEST_CHANNELS`, reads the messages,
   notices, actions, topic changes and kicks newer than that channel's saved
   cursor and no older than `DIGEST_LOOKBACK_HOURS`. The window stops a new
   channel or a cursor that fell behind from dumping its whole backlog into
   one digest; older messages are skipped and the cursor moves past them.
3. Collects trending links: URLs posted in at least two channels, ignoring
   your own messages and those of the nicks in `DIGEST_BOTS`.
4. Collects messages containing any of `DIGEST_KEYWORDS` as a whole word
   (case-insensitive, with the word boundaries of The Lounge's own custom
   highlights, so `go` matches "go!" but not "google") and highlights the
   keyword in bold.
5. Builds the digest: a one-line count of messages, channels, trending links
   and keyword matches when more than one channel has news, then one section
   per channel, then the trending links (those shared in the most channels
   first) and keyword matches. Nick colours
   follow The Lounge's own colour assignment; bots, your own messages,
   actions and notices are set apart. If nothing is new, the run sends
   nothing and exits 0.
6. Posts the digest, titled `The Lounge Digest`, split into chunks of at
   most 4,000 characters (under [Telegram](https://telegram.org/)'s message
   limit) with `APPRISE_POST_DELAY_SECONDS` between posts to avoid flood
   control. Chunks break between one author's groups of lines, and a
   channel that runs on into the next chunk starts it again with its name
   marked "continued". Each trending link follows as its own `Trending link` message,
   so it is the message's first link and Telegram can preview it. Posting
   goes through the [apprise](https://github.com/caronc/apprise) library:
   each URL in `APPRISE_URL` is its own priority group, so they are tried in
   order, each retried once before the next.
7. Saves the new cursors, and only if every digest chunk was delivered: a
   failed delivery exits 1 and the next run sends the same messages again. A
   failed trending-link message is only a warning.

The Lounge stores each message's network as a UUID. The digest maps network
names to UUIDs through The Lounge's `users/<user>.json`, so channels with the
same name on two networks stay apart. If a network name is not found there,
the run warns and matches on the channel name alone.

## Configuration

All settings are environment variables.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `DIGEST_CHANNELS` | yes | | Comma-separated `Network/#channel` pairs, with the network names The Lounge shows, such as `Libera/#chan,Libera/##other` |
| `APPRISE_URL` | yes, except with `--init` or `--dry-run` | | Apprise API notify URL, such as `http://apprise:8000/notify/<key>`. Several whitespace-separated URLs for the same key are tried in order until one delivers, so a second Apprise server can be the fallback |
| `DIGEST_BOTS` | no | empty | Comma-separated bot nicks: shown as bots, given no nick colour and ignored for trending links |
| `DIGEST_KEYWORDS` | no | empty | Comma-separated keywords, matched as whole words, to highlight and list in their own section |
| `THELOUNGE_USER` | yes, unless both paths below are set | | Your user name in The Lounge, which names its files in its home (mounted on `/thelounge`) |
| `THELOUNGE_DB` | no | `/thelounge/logs/<user>.sqlite3` | The Lounge's SQLite log for your user |
| `THELOUNGE_USERS_JSON` | no | `/thelounge/users/<user>.json` | The Lounge's configuration for your user, read for the network UUIDs |
| `CURSOR_FILE` | no | `/data/cursor.json` | Last message ID seen per channel, kept between runs |
| `DIGEST_LOOKBACK_HOURS` | no | `24` | Oldest message a digest may include, in hours (a positive integer) |
| `APPRISE_POST_DELAY_SECONDS` | no | `1.5` | Pause between consecutive Apprise posts |
| `TZ` | no | `UTC` | Time zone of the timestamps in the digest |

## Command-line flags

- `--init` sets every configured channel's cursor to its newest message and
  sends nothing. Run it once before the first scheduled run, or the first
  digest holds up to `DIGEST_LOOKBACK_HOURS` of history.
- `--dry-run` builds the digest and prints it to stdout without sending it
  or saving the cursor.

## Running it

The image is `ghcr.io/mbologna/thelounge-digest`, built for linux/amd64 and
linux/arm64 and tagged `X.Y.Z` and `X.Y` for each release and `latest` for
the newest. Its entry point is the `thelounge-digest` console script, so
flags go after the image name:

```sh
docker run --rm \
  -e DIGEST_CHANNELS="Libera/#chan" \
  -e THELOUNGE_USER=alice \
  -e APPRISE_URL="http://apprise:8000/notify/irc" \
  -e TZ=Europe/London \
  -v /path/to/thelounge:/thelounge:ro \
  -v /path/to/data:/data \
  ghcr.io/mbologna/thelounge-digest:latest --init
```

Drop `--init` for the scheduled runs. A [systemd](https://systemd.io/) timer
that runs the container with [Podman](https://podman.io/), or a Kubernetes
[CronJob](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/),
both work.

The container runs as uid and gid 1000, The Lounge's user in its official
image, so it can read The Lounge's home mounted read-only. The `/data`
directory must be writable by that user (`chown 1000:1000 /path/to/data`),
because the cursor is written to a temporary file there and renamed;
`--user` picks another user. With a read-only root filesystem, mount a
writable `/tmp` for the snapshot.

Images up to 2.1.0 ran `python /app/digest.py`. Later images keep a small
script at that path that runs the same code, so a deployment that still
calls it works; switch it to `thelounge-digest`, as the shim will be removed.

## Development

The project uses [uv](https://docs.astral.sh/uv/) and Python 3.15, the
version in the image. The code is in `src/thelounge_digest/`.

```sh
uv sync
uv run pytest --cov
uv run ruff check . && uv run ruff format --check .
uv run pre-commit install        # once
uv run pre-commit run --all-files
uv run thelounge-digest --dry-run   # needs DIGEST_CHANNELS and THELOUNGE_DB/THELOUNGE_USERS_JSON
```

`pytest --cov` fails below the coverage floor, `fail_under` in
`pyproject.toml`. The [pre-commit](https://pre-commit.com/) hooks add
[ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org/),
[bandit](https://bandit.readthedocs.io/) and
[hadolint](https://github.com/hadolint/hadolint) to the usual file checks.

Dependencies resolve only to releases at least 7 days old (`exclude-newer`
in `pyproject.toml`), and the image installs exactly what `uv.lock` pins.
To take a CVE fix released less than 7 days ago, add the package to
`exclude-newer-package` as `<name> = false`, run `uv lock`, and remove the
entry once the release is a week old.

CI runs the reusable `python-ci` and `docker-ci` workflows from
[mbologna/.github](https://github.com/mbologna/.github): ruff, the tests
with the coverage gate, pip-audit, then the image build and a smoke test.
Pull requests build the image without pushing it; only a release tag
publishes it ([Versioning](#versioning)).

The project skeleton comes from
[python-app-template](https://github.com/mbologna/python-app-template)
through [Copier](https://copier.readthedocs.io/) (`.copier-answers.yml`).

## Why not an existing tool

- The Lounge keeps channel logs, but has no scheduled digest or export of
  them.
- No The Lounge plugin on [npm](https://www.npmjs.com/) adds one.

Checked 2026-11-23.

## Related projects

- [thelounge-patched](https://github.com/mbologna/thelounge-patched): the
  official The Lounge image with fixes waiting to land upstream, among them
  the WAL mode that lets this digest read the database without blocking it.
- [thelounge-plugin-apprise-push](https://github.com/mbologna/thelounge-plugin-apprise-push):
  push notifications for highlights and private messages, through the same
  Apprise API.
- [thelounge-theme-chat](https://github.com/mbologna/thelounge-theme-chat):
  a light and dark theme for The Lounge.

## Versioning

Releases follow SemVer: [auto-release](https://github.com/mbologna/auto-release)
tags `vX.Y.Z` on Mondays from the [Conventional Commit](https://www.conventionalcommits.org/)
types merged since the last tag (`feat` minor; `fix`, `perf`, `revert`
patch; a breaking change major), and runtime dependency updates ship in a
weekly maintenance release. Each tag publishes the image as `X.Y.Z`, `X.Y`
and `latest`; the `version` in `pyproject.toml` stays `0.0.0`. See the
[versioning standard](https://github.com/mbologna/.github/blob/main/VERSIONING.md).

## Licence

[MIT](LICENSE)
