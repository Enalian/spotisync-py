FROM python:3.14-slim

ARG PUID=1000
ARG PGID=1000

LABEL org.opencontainers.image.source="https://github.com/Enalian/spotisync-py"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_INSTALL=/usr/local \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    LANG=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    LC_ALL=en_US.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    PYTHONUTF8=1 \
    TERM=xterm-256color \
    EDITOR=nano

# 1. Системные пакеты + Deno (для JS-челленджей YouTube)
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    ca-certificates \
    nano \
    lnav \
    locales \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen \
    && curl -fsSL https://deno.land/install.sh | sh \
    && chmod 755 /usr/local/bin/deno \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Создаем отдельного пользователя spotisync (1000:1000) с домашней директорией (под кэш yt-dlp)
RUN groupadd -g ${PGID} spotisync && \
    useradd -u ${PUID} -g spotisync -m -d /home/spotisync -s /bin/bash spotisync

# 3. Создаем изолированный venv, чтобы юзер spotisync мог сам обновлять yt-dlp при старте
RUN python -m venv ${VIRTUAL_ENV} && \
    pip install --upgrade pip && \
    pip install \
    httpx \
    "yt-dlp[default]" \
    bgutil-ytdlp-pot-provider \
    mutagen \
    python-dotenv

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app
COPY sync_spotify.py /app/sync_spotify.py

# 4. Создаем рабочие директории и отдаем права пользователю spotisync
RUN mkdir -p /app/data /tmp/spotisync_staging /music && \
    chown -R spotisync:spotisync ${VIRTUAL_ENV} /app /tmp/spotisync_staging /music /home/spotisync

# 5. Переключаемся на непривилегированного пользователя
USER spotisync

# 6. Запуск приложения
ENTRYPOINT ["/entrypoint.sh"]
CMD []
