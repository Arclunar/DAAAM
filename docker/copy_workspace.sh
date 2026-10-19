#!/usr/bin/env bash
# Assemble a deployment snapshot from the active source tree and dependency volume.
set -euo pipefail
volume=$1
sources=$2
repo=$3
target=$4
test -d "$volume/.venv" && test -d "$sources/daaam" && test -f "$repo/pyproject.toml"
mkdir -p "$target/src/daaam" "$target/cache/huggingface" "$target/cache/torch"
tar -C "$volume" -cf - --exclude=./src --exclude=./cache --exclude=./log --exclude=./vendor . \
  | tar -xf - -C "$target"
tar -C "$sources" -cf - --exclude=./daaam --exclude=.git --exclude=__pycache__ . \
  | tar -xf - -C "$target/src"
tar -C "$repo" -cf - --exclude=./.git --exclude=./output --exclude=./datasets --exclude=./logs \
  --exclude=./checkpoints --exclude=./.claude --exclude=./dist --exclude=__pycache__ \
  --exclude=./.env --exclude='*apikey*' --exclude='*api_key*' . \
  | tar -xf - -C "$target/src/daaam"
mkdir -p "$target/src/daaam/checkpoints" "$target/src/daaam/datasets" "$target/src/daaam/output"
