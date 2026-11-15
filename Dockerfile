# Use Ubuntu base
FROM lscr.io/linuxserver/baseimage-ubuntu:noble

ENV PUID=99 \
    PGID=100 \
    UMASK=002

# Layer 1: System dependencies (rarely changes)
# Single layer to avoid file duplication across layers
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Python build tools
    python3 \
    python3-pip \
    python3-dev \
    python3-venv \
    gcc \
    g++ \
    make \
    git \
    # Audio validation tools
    flac \
    ffmpeg \
    # Permission handling
    gosu \
    # Cron for scheduling
    cron \
    && rm -rf /var/lib/apt/lists/*

# Layer 2: Python dependencies only (rebuilds when the lockfile changes)
# Copy only dependency declaration, not the entire codebase
COPY pyproject.toml poetry.lock /app/
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN python3 -m venv "$VIRTUAL_ENV" && \
    cd /app && \
    python3 -m venv /tmp/poetry && \
    /tmp/poetry/bin/pip install --no-cache-dir 'poetry>=2.2,<3' && \
    POETRY_VIRTUALENVS_CREATE=false /tmp/poetry/bin/poetry install --only main --no-root && \
    rm -rf /tmp/poetry

# Layer 3: Streamrip code (changes frequently, rebuilds quickly)
COPY . /app/
RUN cd /app && \
    pip3 install --no-cache-dir --no-deps . && \
    # Cleanup build dependencies to reduce image size
    apt-get purge -y gcc g++ python3-dev && \
    apt-get autoremove -y && \
    rm -rf /tmp/* /root/.cache /var/lib/apt/lists/*

# Layer 4: Runtime configuration
# Save the real rip binary and create wrapper for permission handling
RUN mv /opt/venv/bin/rip /opt/venv/bin/rip.bin && \
    printf '%s\n' \
      '#!/bin/bash' \
      'set -e' \
      'export HOME=/config' \
      'cd /downloads' \
      'exec gosu ${PUID:-99}:${PGID:-100} env HOME=/config /opt/venv/bin/rip.bin "$@"' \
      > /usr/local/bin/rip && \
    chmod +x /usr/local/bin/rip && \
    ln -s /usr/local/bin/rip /opt/venv/bin/rip

# single job script used both at startup and daily; imports env via s6
RUN mkdir -p /etc/periodic/daily && \
    printf '%s\n' \
      '#!/usr/bin/with-contenv bash' \
      'set -e' \
      'export HOME=/config' \
      '/usr/local/bin/rip url https://tidal.com/my-collection/albums https://tidal.com/my-collection/artists https://tidal.com/my-collection/tracks https://play.qobuz.com/user/library/favorites/albums https://play.qobuz.com/user/library/favorites/artists https://play.qobuz.com/user/library/favorites/tracks' \
      'date' \
      > /etc/periodic/daily/rip-tidal && \
    chmod +x /etc/periodic/daily/rip-tidal

# run once on container start
RUN mkdir -p /etc/cont-init.d && \
    ln -s /etc/periodic/daily/rip-tidal /etc/cont-init.d/99-run-rip-once

# cron setup (4 AM EDT daily) - Ubuntu uses /etc/cron.d/
RUN printf '%s\n' \
      '0 4 * * * root /etc/periodic/daily/rip-tidal' \
      > /etc/cron.d/rip-tidal && \
    chmod 0644 /etc/cron.d/rip-tidal

# s6 service keeps cron in foreground (container stays alive)
RUN mkdir -p /etc/services.d/crond && \
    printf '%s\n' \
      '#!/usr/bin/with-contenv bash' \
      'exec cron -f' \
      > /etc/services.d/crond/run && \
    chmod +x /etc/services.d/crond/run

ENV HOME=/config

VOLUME /config /downloads /library
WORKDIR /downloads
# no CMD (LSIO uses /init)
