# ==========================================
# STAGE 1: Сборка ELF файла через Nuitka
# ==========================================
FROM python:slim AS builder

# Устанавливаем тяжелые зависимости для компиляции C-кода
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    patchelf \
    ccache \
    libc6-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "nuitka[onefile]" zstandard

COPY src/ ./src/

# Компилируем!
# --onefile делает единый бинарник
# --enable-plugin=pydantic критически важен для работы Pydantic
RUN python -m nuitka \
    --onefile \
    --jobs=4 \
    --output-filename=spotisync_bin \
    --include-package=pydantic \
    --include-package=pydantic_core \
    --include-package=pydantic_settings \
    --include-package=httpx \
    --include-package=mutagen \
    src/main.py

# ==========================================
# STAGE 2: Финальный образ (только бинарник и FFmpeg)
# ==========================================
FROM debian:bookworm-slim AS runner

ARG PUID=1000
ARG PGID=1000
ENV DENO_INSTALL=/usr/local \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    TERM=xterm-256color \
    EDITOR=nano

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    tzdata \
    nano \
    lnav \
    locales \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen \
    && curl -fsSL https://deno.land/install.sh | sh \
    && chmod 755 /usr/local/bin/deno \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g ${PGID} spotisync && \
    useradd -u ${PUID} -g spotisync -m -d /home/spotisync -s /bin/bash spotisync

WORKDIR /app
COPY --from=builder /build/spotisync_bin /usr/local/bin/spotisync
RUN mkdir -p /app/data /music /tmp/spotisync_staging \
    && chmod +x /usr/local/bin/spotisync \
    && chown -R spotisync:spotisync /app /music /tmp/spotisync_staging /usr/local/bin/spotisync

USER spotisync
ENTRYPOINT ["spotisync"]
