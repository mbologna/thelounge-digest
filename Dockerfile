FROM python:3.15-alpine@sha256:a0f2e268f2ce8f3709611a90054ec6a8ae31f6251f5896b8648b9bb99835e30a
RUN apk add --no-cache tzdata
# The base image's pip (with the packages it vendors) and ensurepip's
# bundled pip wheel are never used at run time; removing them keeps their
# CVEs out of the image.
RUN /usr/local/bin/python3 -m pip uninstall -y --root-user-action=ignore pip \
    && rm -rf /usr/local/lib/python3*/ensurepip
WORKDIR /app
COPY digest.py .
# The Lounge's own UID in its official image, so the read-only mount of its
# home is readable; /data (the cursor) must be writable by it. Snapshots go
# to /tmp.
USER 1000:1000
CMD ["python", "/app/digest.py"]
