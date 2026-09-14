FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MAILSENTRY_CONFIG=/config/config.yaml

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --upgrade pip && python -m pip install .

RUN addgroup --system --gid 10001 mailsentry \
    && adduser --system --uid 10001 --ingroup mailsentry mailsentry \
    && mkdir -p /config /app/data \
    && chown -R mailsentry:mailsentry /config /app/data

USER mailsentry
VOLUME ["/app/data"]

HEALTHCHECK --interval=1m --timeout=10s --start-period=20s --retries=3 \
  CMD ["mail-sentinel", "doctor", "--config", "/config/config.yaml", "--no-connect"]

ENTRYPOINT ["mail-sentinel"]
CMD ["run", "--config", "/config/config.yaml"]
