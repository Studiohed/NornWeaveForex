# NornWeaveForex bot image (Linux-side strategy + MT5 RPyC client).
#
# Build:  docker build -t nornweave-forex .
# Run:    docker compose up -d --build
#
# The MetaTrader 5 terminal is NOT in this image: it runs on a Windows host,
# reachable through MT5_HOST/MT5_PORT.

FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md MANIFEST.in ./
COPY nornweave_forex ./nornweave_forex
RUN python -m pip install --upgrade pip setuptools wheel \
 && python -m pip install .

# Shipped defaults; mount your own config over this file in production.
COPY configs ./configs

RUN useradd --system --uid 10001 --home-dir /var/lib/nornweave nornweave \
 && mkdir -p /var/lib/nornweave \
 && chown -R nornweave:nornweave /var/lib/nornweave

USER nornweave
WORKDIR /var/lib/nornweave

# SIGTERM is handled by the CLI (it persists state and exits).
STOPSIGNAL SIGTERM

ENTRYPOINT ["nornweave-forex"]
CMD ["live", \
     "--config", "/app/configs/forex.yaml", \
     "--state", "/var/lib/nornweave/live.json", \
     "--interval-seconds", "30"]
