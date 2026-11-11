FROM python:3.13-slim-trixie

ENV DEBIAN_FRONTEND=noninteractive
ENV EXIFTOOL_PATH=/usr/bin/exiftool
ENV FFMPEG_PATH=/usr/bin/ffmpeg
ENV MARKITFORGE_ENABLE_PLUGINS=True
ENV ORT_DISABLE_TELEMETRY=1

# Runtime dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

# Build from the repository root so the core package comes from this checkout,
# never from PyPI:
#   docker build -f packages/markitforge-mcp/Dockerfile -t markitforge-mcp .
COPY packages/markitforge /app/packages/markitforge
COPY packages/markitforge-mcp /app/packages/markitforge-mcp
COPY LICENSE LICENSE-APACHE NOTICE /app/

RUN pip install --no-cache-dir "/app/packages/markitforge[all]" /app/packages/markitforge-mcp

WORKDIR /workdir

ARG USERID=nobody
ARG GROUPID=nogroup

USER ${USERID}:${GROUPID}

ENTRYPOINT ["markitforge-mcp"]
