# Build stage: a pinned uv installs exactly what uv.lock pins into
# /opt/venv. Same base image and digest as the runtime stage, so compiled
# wheels match the Python they run on.
FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd AS build

# uv from PyPI, hash-checked (requirements-uv.txt says why).
COPY requirements-uv.txt /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r /tmp/requirements-uv.txt
# Install exactly what uv.lock pins, checking its hashes, rather than
# re-resolving pyproject.toml at build time; --locked fails the build when
# uv.lock is out of date. Dependencies first, so their layer survives a change
# to the code or the docs; then the app itself, as a regular (non-editable)
# package. Bytecode is compiled here because the runtime image sets
# PYTHONDONTWRITEBYTECODE and runs read-only.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON=/usr/local/bin/python3 UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /tmp/build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable --no-install-project
COPY README.md LICENSE ./
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.15.2-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
# The CI pipeline adds org.opencontainers.image.source and .revision, and
# .version from the release tag (pyproject.toml's version is a placeholder).
LABEL org.opencontainers.image.title="thelounge-digest" \
      org.opencontainers.image.description="Scheduled IRC digests for TheLounge: reads its SQLite logs and sends nick-coloured HTML digests with trending links and keyword highlights through Apprise" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /opt/venv /opt/venv
# Run-time writes go to /tmp (HOME too), so the root filesystem can be
# read-only.
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
# The base image's pip (with the packages it vendors) and ensurepip's
# bundled pip wheel are never used at run time; removing them keeps their
# CVEs out of the image.
RUN /usr/local/bin/python3 -m pip uninstall -y --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3*/ensurepip
# The distribution's security fixes published since the base image was
# built: Docker's official images are rebuilt only every week or few.
# auto-release's fresh rebuild counts the base image's findings because of
# this line, so a fix ships within a week without a base image update.
# Then the packages the app needs at run time, unpinned: the base image
# digest fixes the distribution release they come from.
# hadolint ignore=DL3018
RUN apk upgrade --no-cache \
    && apk add --no-cache tzdata
# Transitional: deployments written before the src/ layout still run
# `python /app/digest.py`; compat/ runs the package's module for them.
# Remove both lines once they call thelounge-digest.
WORKDIR /app
COPY compat/digest.py ./
# The Lounge's own UID in its official image, so the read-only mount of its
# home is readable; /data (the cursor) must be writable by it. Snapshots go
# to /tmp.
USER 1000:1000
ENTRYPOINT ["thelounge-digest"]
