FROM python:3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
RUN apk add --no-cache tzdata
WORKDIR /app
COPY digest.py .
CMD ["python", "/app/digest.py"]
