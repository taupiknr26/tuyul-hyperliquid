# syntax=docker/dockerfile:1
# Reproducible image: dependencies are installed from the hash-pinned
# lockfile (SR-12, NFR-019). No secret is baked in; secrets and the
# Telethon session are mounted at run time.
FROM python:3.13-slim AS build
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock
COPY pyproject.toml README.md ./
COPY hlbot ./hlbot
RUN pip install --no-deps .

FROM python:3.13-slim
RUN useradd --create-home --uid 10001 hlbot \
    && mkdir -p /app/data /app/logs /secrets \
    && chown -R hlbot:hlbot /app /secrets \
    && chmod 700 /app/data /secrets
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data LOG_DIR=/app/logs
WORKDIR /app
USER hlbot
VOLUME ["/app/data", "/app/logs"]
# The local API stays on loopback inside the container by default (SR-09).
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('API_PORT','8787'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4).status == 200 else 1)"
STOPSIGNAL SIGTERM
ENTRYPOINT ["botctl"]
CMD ["run", "--quiet"]
