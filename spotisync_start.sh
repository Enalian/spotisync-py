#!/bin/bash
if [ "$1" == "--soft" ]; then
    docker compose down spotisync --remove-orphans
    docker compose up -d --build spotisync
    docker compose logs -f -t spotisync
    exit 0
fi

docker compose down spotisync --remove-orphans
docker compose run -it spotisync -- --remove-cache
docker compose down spotisync --remove-orphans
docker compose up -d --build spotisync
docker compose logs -f -t spotisync
