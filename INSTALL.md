# Install / Run (DGX Spark)

The canonical, code-grounded migration walkthrough lives in
[`project_overview/DGX_MIGRATION.md`](project_overview/DGX_MIGRATION.md). This file is the
short operational checklist.

## System packages (once)

```bash
sudo apt update
sudo apt install -y espeak-ng ffmpeg          # Kokoro TTS + Whisper STT
sudo usermod -aG docker $USER && newgrp docker
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

`libreoffice` is already present on the DGX (used by `integrations/libreoffice_converter.py`).

## Python env

```bash
cd ~/aganetiAi
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip wheel
pip install -r requirements.txt
```

## Config

```bash
cp .env.example .env     # then fill in secrets (or reuse the migrated .env)
```

`LLM_MODEL_PATH` should point at the first shard of the smart model:
`qwen2.5-14b-instruct-q4_k_m-00001-of-00003.gguf`.

## Models

Place GGUF files under `./models/`:
- `qwen2.5-1.5b-instruct-q4_k_m.gguf` (fast)
- `qwen2.5-14b-instruct-q4_k_m-0000{1,2,3}-of-00003.gguf` (smart, split — llama.cpp
  auto-loads shards 2/3 from shard 1).

## Bring up

```bash
docker compose up -d                       # llama_server (GPU), llama_fast (GPU), qdrant
source .venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000
# optional dashboard:
streamlit run frontend/app.py --server.port 8501 --server.address 0.0.0.0
```

## GPU STT (whisper.cpp, CUDA) — optional but ~10x faster

The `ctranslate2` PyPI wheel for aarch64 is CPU-only and the upstream
whisper.cpp CUDA Docker image is amd64-only, so on the GB10 we build
whisper.cpp from source (same ggml CUDA backend that already powers
`local_llm_core`). Backend `/stt` auto-uses it when reachable and falls
back to CPU faster-whisper otherwise — nothing breaks if it's not running.

Build once:

```bash
cd ~ && git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=121 \
      -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)" --target whisper-server whisper-cli
bash ./models/download-ggml-model.sh base.en      # or small.en for more accuracy
```

Run (foreground): `bash ~/aganetiAi/scripts/run_whisper_server.sh`
Or as a service:

```bash
sudo cp deploy/aria-whisper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aria-whisper
curl -F file=@~/whisper.cpp/samples/jfk.wav http://127.0.0.1:8090/inference
```

Measured on the GB10: ~150 ms for an 11 s clip with the model resident
(vs ~1–3 s on CPU `small`). Override the endpoint with `WHISPER_CPP_URL`
in `.env` (empty string disables the GPU path).

## Run as a service (optional)

```bash
sudo cp deploy/aganeti-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aganeti-api
```

## Smoke tests

```bash
curl http://localhost:8000/health
curl http://localhost:8080/v1/models
curl http://localhost:6333/collections
```

> ⚠️ The Telegram bot uses long polling. Run it on **one** host at a time — starting a
> second instance with the same `TELEGRAM_BOT_TOKEN` causes a 409 conflict. Stop the
> cloud VM's backend before bringing the bot up on the DGX.
