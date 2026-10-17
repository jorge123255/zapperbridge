# syntax=docker/dockerfile:1
# zapperbridge: stdlib-only Python, no pip packages.
# Base pinned to a tag AND its multi-arch (amd64+arm64) index digest, resolved
# 2026-10-10 from Docker Hub. Dependabot (docker ecosystem) proposes bumps.
FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

ARG VERSION=dev
ARG REVISION=unknown
ARG CREATED=unknown
LABEL org.opencontainers.image.title="zapperbridge" \
      org.opencontainers.image.description="Presents a ZapperBox M1 as an HDHomeRun-compatible network tuner (unofficial)" \
      org.opencontainers.image.source="https://github.com/jorge123255/zapperbridge" \
      org.opencontainers.image.documentation="https://github.com/jorge123255/zapperbridge#readme" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${CREATED}" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="unofficial community project"

# libcap2-bin: lets the unprivileged user bind :80 via a file capability.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libcap2-bin \
 && rm -rf /var/lib/apt/lists/* \
 && setcap cap_net_bind_service=+ep "$(readlink -f "$(command -v python3)")" \
 && useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin zapper \
 && mkdir -p /data /config && chown zapper /data

WORKDIR /app
COPY *.py zapperbridge.example.toml /app/
COPY ui /app/ui
COPY docker/entrypoint.py docker/healthcheck.py /app/docker/
COPY docs /app/docs

ENV PYTHONUNBUFFERED=1 \
    ZB_CONFIG=/config/zapperbridge.toml \
    HDHR_DAEMON=0
USER zapper
VOLUME ["/data"]
# TCP 80 JSON (+ /ui when enabled), 5004 TS, 31061 push; UDP 31064 announce, 1900 SSDP, 65001 HDHR
EXPOSE 80/tcp 5004/tcp 31061/tcp 31064/udp 1900/udp 65001/udp
HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
  CMD ["python3", "/app/docker/healthcheck.py"]
ENTRYPOINT ["python3", "/app/docker/entrypoint.py"]
CMD ["run"]
