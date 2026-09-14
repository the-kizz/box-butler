#!/usr/bin/env bash
# Generates the same three fixtures tests/audio/test_ffmpeg_real.py builds
# itself at test time, for manual/ad-hoc use. Outputs are gitignored —
# never commit audio files to this repo.
set -euo pipefail
cd "$(dirname "$0")"

ffmpeg -y -loglevel error -f lavfi -i "sine=frequency=440:duration=12" -ar 44100 -ac 2 -c:a aac long.m4a
ffmpeg -y -loglevel error -f lavfi -i "sine=frequency=440:duration=3" -c:a libopus short.webm
ffmpeg -y -loglevel error -f lavfi -i "sine=frequency=440:duration=5" -c:a libmp3lame mid.mp3
