FROM python:3.12-alpine@sha256:4c47124a8391cb7a9f571164147d154777cf012a4ece5f86097130d7a4478111
RUN apk add --no-cache tzdata
WORKDIR /app
COPY digest.py .
CMD ["python", "/app/digest.py"]
