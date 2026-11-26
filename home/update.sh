#!/bin/sh
# Unattended security update for the home stack: fetch patched images, rebuild the relay on a
# freshly pulled base image, and restart only the containers whose image changed.
# Run it from the timer in systemd/ (README.md, "Automatic security updates").
#
# It does not `git pull`: a new version of the pager software is installed by hand.
# Profiles and overrides come from home/.env (COMPOSE_PROFILES, COMPOSE_FILE), as for every
# `docker compose` command in this directory.
set -eu
cd "$(dirname "$0")"

docker compose pull --ignore-buildable
docker compose build --pull
docker compose up -d
# Image layers the pulls and the rebuild left untagged.
docker image prune -f
