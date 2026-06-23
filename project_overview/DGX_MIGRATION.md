# DGX Spark — Migration Plan

**Target host:** `matrix@192.168.1.155`
**Source:** the Google cloud VM where the working `memory/` Python package + populated
Qdrant collections + per-user tokens currently live.

## DGX Spark hardware inventory (captured 2026-06-23)

| Item | Value |
|---|---|
| Hostname | `spark-9a32` |
| OS | Ubuntu 24.04.4 LTS (Noble) |
| Kernel | `6.17.0-1021-nvidia` |
| Architecture | **aarch64 (ARM64)** ← matters for Docker images |
| CPU | 20 cores |
| RAM | 121 GiB (15 GiB swap) |
| Disk | 3.6 TB on `/` (45 GB used) |
| GPU | NVIDIA GB10, driver 580.159.03, CUDA 13.0, idle at 11 W |
| Docker | 29.2.1, Compose v5.0.2 |
| Python | 3.12.3 |
| Already installed | LibreOffice |
| **Missing system pkgs** | `espeak-ng` (Kokoro TTS), `ffmpeg` (Whisper STT pipeline) |
| **Group fix needed** | `matrix` is **not** in the `docker` group |

## Critical-path notes

1. **ARM64.** The project was likely developed on x86_64. Container images and any
   wheels with native code (`onnxruntime` for fastembed, `weasyprint` cairo bindings,
   `faster-whisper`, `kokoro`) must be ARM64-compatible. The `ghcr.io/ggml-org/llama.cpp:server*`
   images publish multi-arch tags, so those should pull fine. Watch fastembed/onnxruntime
   on first run.
2. **GPU.** A GB10 sitting at 11 W is the headline hardware. The current
   `docker-compose.yml` is CPU-only. **Migration is not done until both LLM containers
   run on the GPU.**
3. **`memory/` package.** Do **not** rely on `git clone` (see CURRENT_GAPS.md §1).
   Pull from the cloud VM with rsync so the Python modules in `memory/` come with us.

## Step-by-step migration

### 0. Pre-flight on the DGX

```bash
ssh matrix@192.168.1.155
sudo usermod -aG docker matrix
sudo apt update
sudo apt install -y espeak-ng ffmpeg rsync git
newgrp docker        # or log out + back in
docker ps            # confirms socket access
```

### 1. Pull from the cloud VM with rsync (preferred)

From the cloud VM (let's call it `cloud-vm`), assuming the project lives at
`/home/my_vm_google/projects/aganetiAi`:

```bash
# From the cloud VM:
rsync -avzP --exclude='__pycache__' --exclude='.git' \
  /home/my_vm_google/projects/aganetiAi/ \
  matrix@192.168.1.155:/home/matrix/aganetiAi/
```

This brings the working `memory/` Python package, populated `tokens/`, `email_store/`,
`tasks/tasks.db`, `logs/`, and any local `.env` along. The `.git` exclusion keeps
the directory smaller — you can re-attach a new remote later.

**If the cloud VM is unreachable**, fall back to `git clone
https://github.com/MrPapayaOyister/aganetiAi.git` **plus** you will need to hand-port
or recreate the `memory/` Python modules — they are not on GitHub.

### 2. Fix paths and gitignore in the migrated tree

On the DGX, in `/home/matrix/aganetiAi/`:

- Edit `backend/main.py:1409` and `frontend/app.py:20` to read
  `DRAFTS_FILE = str(BASE_DIR / "frontend" / "email_drafts.json")` (importing
  `BASE_DIR` from `config.settings`).
- Edit `.gitignore`: change `memory/` to `memory/qdrant_data/` (or whatever the actual
  Qdrant storage subdir is) so the Python package is tracked.

### 3. Python virtual-env + dependency fix

```bash
cd /home/matrix/aganetiAi
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip wheel
```

`requirements.txt` is incomplete (CURRENT_GAPS.md §2). Either install the missing
packages explicitly:

```bash
pip install -r requirements.txt
pip install fastapi 'uvicorn[standard]' langgraph openai qdrant-client fastembed \
            streamlit requests jinja2 pydantic
```

…or — better — replace `requirements.txt` with the consolidated list in
`IMPROVEMENTS.md` §A1 before installing.

### 4. `.env` setup

If `.env` came across via rsync, audit it. Otherwise create one from the variables
listed in `config/settings.py`. **Update every URL** that referenced the old VM:
- `LLM_BASE_URL`, `LLM_SMART_URL`, `LLM_FAST_URL` → `http://localhost:8080`/`8081`
  (Docker on the same host).
- `QDRANT_URL` → `http://localhost:6333`.
- `EMAIL_ACCOUNT`, `APP_PASSWORD`, `SMTP_HOST`, `IMAP_SERVER` may still be present
  but are legacy — Microsoft Graph is the real path now.

### 5. Re-author tokens (only if rsync was incomplete)

```bash
python3 integrations/m365_auth.py --auth user_1
python3 integrations/m365_auth.py --auth user_2
python3 integrations/m365_auth.py --status
```

If `tokens/` rsynced cleanly, skip this — MSAL silent refresh will keep working.

### 6. GPU-enabled docker-compose

Replace `docker-compose.yml` with a GPU-aware version:

```yaml
services:
  llama_server:
    image: ghcr.io/ggml-org/llama.cpp:server-cuda
    container_name: local_llm_core
    runtime: nvidia
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    ports: ["8080:8080"]
    volumes: ["./models:/models"]
    command: >
      -m /models/${LLM_MODEL_PATH}
      --host 0.0.0.0 --port 8080
      -c 8192
      --n-gpu-layers 999       # offload everything we can
      --embeddings --pooling last
    restart: unless-stopped

  qdrant:
    image: qdrant/qdrant:latest
    container_name: qdrant_memory
    ports: ["6333:6333", "6334:6334"]
    volumes: ["./memory/qdrant_data:/qdrant/storage"]
    restart: unless-stopped

  llama_fast:
    image: ghcr.io/ggml-org/llama.cpp:server-cuda
    container_name: llama_fast
    runtime: nvidia
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    ports: ["8081:8080"]
    volumes: ["./models:/models"]
    command: >
      -m /models/qwen2.5-1.5b-instruct-q4_k_m.gguf
      --host 0.0.0.0 --port 8080
      -c 4096 --n-gpu-layers 999 --n-predict 512 --no-mmap
    restart: unless-stopped
```

**Confirm before applying.** If the NVIDIA Container Toolkit isn't already
configured on the DGX, install it:

```bash
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:13.0.0-base-ubuntu24.04 nvidia-smi
```

If that last command shows the GB10, the GPU stack is good.

### 7. Models

`./models/` is gitignored. You need to:
- Either rsync the GGUFs from the cloud VM,
- Or download them on the DGX:

```bash
mkdir -p models && cd models
# Example — replace with the model the cloud VM used:
curl -L -o qwen2.5-1.5b-instruct-q4_k_m.gguf \
  https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf
# Then a smarter model for LLM_MODEL_PATH (the .env variable picks this).
```

Given 121 GiB system RAM and the GB10 GPU, you can comfortably run a much larger
"smart" model than the cloud VM did — see ROADMAP.md §1 for suggestions.

### 8. Seed Qdrant

If `memory/qdrant_data/` rsynced cleanly, the collections come with it. Otherwise:

```bash
docker compose up -d qdrant
python3 backend/ingest.py   # repopulates corporate_memory from data_vault/company_handbook.txt
```

### 9. Bring up

```bash
docker compose up -d                  # llama_server, llama_fast, qdrant
source .venv/bin/activate
uvicorn backend.main:app --host 0.0.0.0 --port 8000
# In a separate shell:
streamlit run frontend/app.py --server.port 8501 --server.address 0.0.0.0
```

### 10. Smoke tests

```bash
curl http://localhost:8000/health
curl http://localhost:8080/v1/models
curl http://localhost:6333/collections
curl -X POST http://localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message":"hello","session_id":"smoke","user_id":"user_1","stream":false}'
```

Telegram side: hit `/start` from a registered chat id — it should reply
"✨ Workspace Assistant is online".

### 11. Process supervision (optional but recommended)

Add a `systemd` user unit for the FastAPI process so it survives reboots — sample
unit lives in IMPROVEMENTS.md §A2.

## Rollback

The cloud VM stays running until the DGX is verified, so rollback is "stop using the
DGX bot token / point users back at the cloud Telegram chat". If you don't want
duplicate replies during the cut-over, **set `TELEGRAM_BOT_TOKEN=` (empty) on the cloud
VM's `.env` and restart its FastAPI** for the cut-over window — the bot will simply
not initialize there.
