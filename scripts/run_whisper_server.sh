#!/usr/bin/env bash
# GPU STT server (whisper.cpp, CUDA) for Aria.
#
# Built from source for the GB10 (Blackwell sm_121) — the upstream
# whisper.cpp CUDA Docker image is amd64-only, and the ctranslate2 PyPI
# wheel for aarch64 is CPU-only, so this host build is the GPU path.
#
# Backend /stt forwards audio to http://127.0.0.1:8090/inference and falls
# back to CPU faster-whisper if this server is down.
set -euo pipefail

WHISPER_DIR="${WHISPER_DIR:-$HOME/whisper.cpp}"
MODEL="${WHISPER_MODEL_FILE:-$WHISPER_DIR/models/ggml-base.en.bin}"
PORT="${WHISPER_CPP_PORT:-8090}"

exec "$WHISPER_DIR/build/bin/whisper-server" \
  -m "$MODEL" \
  --host 127.0.0.1 \
  --port "$PORT" \
  -bs 1 \
  --no-timestamps \
  --convert
