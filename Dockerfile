# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
# SPDX-License-Identifier: MIT AND Apache-2.0
# Derived from MarkItDown (MIT). MarkitForge changes are Apache-2.0; see NOTICE.
#
# Two images from one file:
#
#   docker build -t markitforge .                         # target "cli" (default)
#   docker run --rm -i markitforge < report.pdf > report.md
#
#   docker build --target gui -t markitforge-gui .
#   docker run --rm -p 127.0.0.1:8765:8765 markitforge-gui
#   # then open http://localhost:8765/ on this computer
#
# Build arguments:
#   WITH_LIBREOFFICE=false  leave out LibreOffice (legacy .doc/.xls/.ppt, ODF,
#                           RTF, ... then cannot be converted; image ~400 MB smaller)
#   USERID / GROUPID        numeric ids to run as (default 10001)

ARG PYTHON_IMAGE=python:3.13-slim-trixie

# ---------------------------------------------------------------------------
# Build stages: install the local packages into a virtualenv. Nothing from
# here except /opt/venv reaches the final images.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build-core
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
COPY packages/markitforge /src/packages/markitforge
# openai: client for the optional AI features (an OpenAI-compatible service
# configured in config.toml, or typed into the GUI).
RUN /opt/venv/bin/pip install \
        "/src/packages/markitforge[all]" \
        openai==3.49.0

FROM build-core AS build-gui
COPY packages/markitforge-gui /src/packages/markitforge-gui
RUN /opt/venv/bin/pip install /src/packages/markitforge-gui

# ---------------------------------------------------------------------------
# Runtime base: Python, media tools, optional LibreOffice, unprivileged user.
# ---------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime
ARG WITH_LIBREOFFICE=true
ARG USERID=10001
ARG GROUPID=10001

LABEL org.opencontainers.image.title="MarkitForge" \
      org.opencontainers.image.description="Convert PDFs, Office files, web pages and more to Markdown" \
      org.opencontainers.image.source="https://github.com/Marcel-Bonifacio/MarkitForge" \
      org.opencontainers.image.licenses="MIT AND Apache-2.0"

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EXIFTOOL_PATH=/usr/bin/exiftool \
    FFMPEG_PATH=/usr/bin/ffmpeg \
    ORT_DISABLE_TELEMETRY=1 \
    HOME=/home/markitforge

# exiftool + ffmpeg as upstream, flac for audio transcription (SpeechRecognition
# bundles no encoder for arm64 Linux); LibreOffice without the desktop UI (the
# *-nogui packages) for the formats MarkitForge converts through it.
# Debian stable package versions are not pinned (security updates come in);
# $packages is split on purpose.
# hadolint ignore=DL3008,SC2086
RUN set -eux; \
    apt-get update; \
    packages="ffmpeg flac libimage-exiftool-perl"; \
    if [ "$WITH_LIBREOFFICE" = "true" ]; then \
        packages="$packages libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui"; \
    fi; \
    apt-get install -y --no-install-recommends $packages; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*; \
    groupadd --gid "$GROUPID" markitforge; \
    useradd --uid "$USERID" --gid "$GROUPID" --home-dir /home/markitforge \
        --create-home --shell /usr/sbin/nologin markitforge; \
    install -d -o markitforge -g markitforge /data

COPY LICENSE LICENSE-APACHE NOTICE /usr/share/doc/markitforge/

WORKDIR /data

# ---------------------------------------------------------------------------
# gui: the web app. It accepts loopback Host names only (plus the container's
# own name and addresses), so publish the port on loopback and open
# http://localhost:8765/ or http://127.0.0.1:8765/ on the host. Results are
# kept in /home/markitforge/.local/share/markitforge (mount a volume there to
# keep them, or add --no-history to the command).
# ---------------------------------------------------------------------------
FROM runtime AS gui
ARG USERID=10001
ARG GROUPID=10001
COPY --from=build-gui /opt/venv /opt/venv
USER ${USERID}:${GROUPID}
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/', timeout=4).status == 200 else 1)"]
ENTRYPOINT ["markitforge-gui"]
CMD ["--host", "0.0.0.0", "--no-browser", "--port", "8765"]

# ---------------------------------------------------------------------------
# cli (default): the markitforge command.
# ---------------------------------------------------------------------------
FROM runtime AS cli
ARG USERID=10001
ARG GROUPID=10001
COPY --from=build-core /opt/venv /opt/venv
USER ${USERID}:${GROUPID}
ENTRYPOINT ["markitforge"]
