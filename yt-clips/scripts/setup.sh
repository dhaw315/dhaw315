#!/usr/bin/env bash
# Prépare un environnement neuf (bac à sable Hoplite ou Linux Debian/Ubuntu) : FFmpeg, Python, dépendances.
# Peut être relancé sans risque. Ne lit et n'affiche aucun secret.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "setup: ffmpeg absent, installation depuis les dépôts de la distribution"
  if [ "$(id -u)" -eq 0 ]; then SUDO=""; else SUDO="sudo"; fi
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq --no-install-recommends ffmpeg fonts-dejavu-core
fi

if ! command -v uv >/dev/null; then
  echo "setup: uv est requis (https://docs.astral.sh/uv/)" >&2
  exit 1
fi

# Versions exactes de uv.lock, Python de .python-version ; échoue si le verrou n'est plus à jour.
uv sync --frozen --extra dev

echo "setup: $(ffmpeg -version | head -n 1)"
echo "setup: $(.venv/bin/python --version)"
# Échoue si FFmpeg n'a pas les filtres et encodeurs requis.
.venv/bin/ytclips check --offline
