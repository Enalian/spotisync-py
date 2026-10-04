#!/usr/bin/env bash
set -e

# Если первым аргументом пришел разделитель "--" (например: docker compose run -it spotisync -- --remove-cache),
# просто сдвигаем его, чтобы передать флаги напрямую в sync_spotify.py
if [ "${1:-}" = "--" ]; then
    shift
fi

# Если первый аргумент начинается с дефиса (- или -- или длинное тире —) или аргументов нет вовсе -> запускаем sync_spotify.py
if [ "$#" -eq 0 ] || [[ "${1}" == -* ]] || [[ "${1}" == —* ]]; then
    exec python3 -u /app/sync_spotify.py "$@"
else
    # Иначе позволяем выполнить любую системную команду (например: bash, sh, ls)
    exec "$@"
fi
