FROM denoland/deno:bin-2.3.0 AS deno

# --- build stage: compile any wheels that need a toolchain -------------------
FROM python:3.12-slim AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock .
RUN pip install --no-cache-dir --prefix=/install -r requirements.lock

# --- runtime stage: no toolchain, just the installed env + system tools -----
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY --from=deno /deno /usr/local/bin/deno
COPY --from=builder /install /usr/local

RUN sed -i 's/Components: main/Components: main non-free/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        7zip \
        unrar \
        qbittorrent-nox \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY . .
RUN chmod +x start.sh

CMD ["bash", "start.sh"]
