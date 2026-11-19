# AGENTS.md — thelounge-digest

Scheduled IRC digests for TheLounge: reads its SQLite logs and sends nick-coloured HTML digests with trending links and keyword highlights through Apprise. User-facing docs: `README.md`.

## Layout

- `src/thelounge_digest/digest.py`: the whole digest run; its `main()` is
  the console script `thelounge-digest` (`--init`, `--dry-run`), the
  image's entry point. Configuration is environment variables only (the
  README lists them).
- `compat/digest.py`: transitional `python /app/digest.py` shim for
  deployments written before the src/ layout; remove when unused.
- `tests/`: pytest. Keep I/O out of the logic so it tests without mocks.
- `Dockerfile`: two stages, `uv sync --locked` into `/opt/venv`, then a
  runtime without pip.

## Conventions

- Conventional Commits, header at most 72 characters. The commit subjects
  that reach main are the release notes: `feat`/`fix`/`perf` release, `refactor`,
  `test`, `docs`, `ci`, `chore` don't (a dependency update to runtime files
  ships in the weekly maintenance release). CI lints every commit with
  [commitlint-config-mbologna](https://github.com/mbologna/commitlint-config)
  and rejects `fixup!` commits; pull requests merge with a merge commit.
- Releases are tags; `pyproject.toml`'s version stays `0.0.0`.
- Coverage may not drop below `fail_under` in `pyproject.toml`; raise it
  when tests are added.
- Dependencies: `uv add <pkg>`; uv.lock is committed and the image installs
  it with `--locked`. `exclude-newer` keeps releases younger than 7 days out.
- Notifications go through `apprise_notify.notify()` with the `APPRISE_URL`
  environment variable: one URL or several space-separated ones, tried in
  order. It never raises; check its return value when sending matters.
- The image: non-root (uid 1000), no pip, nothing written outside `/tmp`
  or a mounted volume.

## Template

The skeleton comes from https://github.com/mbologna/python-app-template
(`.copier-answers.yml`). Renovate's copier manager opens a pull request
for each template release; resolve conflicts in favour of this project's
code. By hand: `uvx copier update`, then `uv lock` if dependencies changed.
