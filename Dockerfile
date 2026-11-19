FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd AS build

# uv from PyPI, hash-checked (requirements-uv.txt says why).
COPY requirements-uv.txt /tmp/
RUN pip install --no-cache-dir --root-user-action=ignore --require-hashes -r /tmp/requirements-uv.txt
# Install exactly what uv.lock pins, checking its hashes; --locked fails the
# build if uv.lock no longer matches pyproject.toml. The project itself is not
# a package (package = false), so only its dependencies land in the venv.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON=/usr/local/bin/python3 UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1
WORKDIR /tmp/build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-editable

FROM python:3.15-alpine@sha256:0f9552bb28711333da16915ec146f5e6bb44a7fdac9ced15dc17c2e0206346dd
RUN apk add --no-cache tzdata
# The base image's pip (with the packages it vendors) and ensurepip's
# bundled pip wheel are never used at run time; removing them keeps their
# CVEs out of the image.
RUN /usr/local/bin/python3 -m pip uninstall -y --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3*/ensurepip
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app
COPY digest.py .
# The Lounge's own UID in its official image, so the read-only mount of its
# home is readable; /data (the cursor) must be writable by it. Snapshots go
# to /tmp.
# The distribution's security fixes published since the base image was
# built: Docker's official images are rebuilt only every week or few.
# auto-release's fresh rebuild counts the base image's findings because of
# this line, so a fix ships within a week without a base image update.
RUN apk upgrade --no-cache
USER 1000:1000
CMD ["python", "/app/digest.py"]
