# Minimal image — the project has zero runtime dependencies.
FROM python:3.12-slim

LABEL org.opencontainers.image.title="loomy2api" \
      org.opencontainers.image.description="Loomy (iFlytek) quota → OpenAI/Anthropic compatible gateway with a multi-account pool" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY loomy2api ./loomy2api
RUN pip install --no-cache-dir .

# Runtime state (accounts.json, logs) lives outside the image.
ENV LOOMY_HOST=0.0.0.0 \
    LOOMY_PORT=17890 \
    LOOMY_ACCOUNTS_FILE=/data/accounts.json \
    LOOMY_LOG_DIR=/data/logs
VOLUME ["/data"]
EXPOSE 17890

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:17890/health',timeout=4).status==200 else 1)"

CMD ["loomy2api", "serve"]
