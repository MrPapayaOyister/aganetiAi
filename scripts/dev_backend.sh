#!/usr/bin/env bash
# Run a LOCAL dev backend for testing the OAuth provider flows end to end,
# without touching the production aganeti-api service (port 8000, cloud URLs).
#
# Why the URL overrides matter: /auth/{provider}/connect builds its redirect_uri
# from BACKEND_URL and the callback bounces the browser to FRONTEND_URL. With the
# production values, consent finishes on papayaoyster.com instead of your dev UI.
#
#   BACKEND_URL  -> where the provider sends the user back  (must be registered
#                   in Azure / Google console EXACTLY as it appears here)
#   FRONTEND_URL -> where the backend sends the user after storing the tokens
#
# Azure only accepts http:// redirect URIs for localhost, so browse through an
# SSH tunnel rather than the LAN IP:
#   ssh -L 3000:localhost:3000 -L 8001:localhost:8001 matrix@<this-host>
#
# Usage:  ./scripts/dev_backend.sh [port]
set -euo pipefail

cd "$(dirname "$0")/.."
PORT="${1:-8001}"

export BACKEND_URL="http://localhost:${PORT}"
export FRONTEND_URL="http://localhost:3000"
# No inbox polling / Telegram bot in dev — the production instance already does
# that, and two pollers on one mailbox double the Graph calls and the triage LLM.
export RUN_BACKGROUND="${RUN_BACKGROUND:-false}"

# This host has unified memory (CPU and GPU share one pool) and the vLLM engines
# hold the bulk of it, so a dev instance beside production has only a few GB of
# headroom — production has already been OOM-killed and restarted by systemd once.
# Neither of these is needed to develop or test the HTTP API, and together they
# are ~1.4 GB of the dev process's ~2.2 GB. Override on the command line if you
# are specifically working on voice or RAG.
export PREWARM_MODELS="${PREWARM_MODELS:-false}"    # Whisper + TTS  (~1.15 GB)
export LOAD_EMBED_MODEL="${LOAD_EMBED_MODEL:-false}" # bge-small      (~0.23 GB)

# Real Supabase login is required by default. To bypass it for local testing run
#   DEV_AUTH_BYPASS=true ./scripts/dev_backend.sh
# and set VITE_DEV_AUTH_BYPASS=true in frontend/.env.development.local. It is
# honoured ONLY for loopback requests, and deliberately lives here rather than in
# .env so the systemd unit — which reads .env — can never inherit it.
export DEV_AUTH_BYPASS="${DEV_AUTH_BYPASS:-false}"
# No default. "user_1" used to be one, and it stopped naming a real account when
# identity moved to Supabase subs — the bypass would have acted as a user that no
# longer exists. Set it to the sub of whoever you want to be:
#   DEV_AUTH_BYPASS=true DEV_AUTH_USER=<supabase-sub> ./scripts/dev_backend.sh
export DEV_AUTH_USER="${DEV_AUTH_USER:-}"

# ── Attribution state ────────────────────────────────────────────────────────
# Three exits of this dev backend were investigated and none could be attributed,
# because the old restart path killed every pid holding the port AND walked up to
# its parent. That produces the same clean "Stopping reloader process" shutdown as
# a deliberate stop, so an exit nobody asked for looked identical to one we caused
# — and an unexplained exit mid-verification eventually gets blamed on the code.
#
# So: record who we start, record when WE stop it, and shout when a previous
# instance vanished without such a record.
RUN_DIR="${RUN_DIR:-run}"
mkdir -p "$RUN_DIR"
PIDFILE="${RUN_DIR}/dev_backend.${PORT}.pid"
JOURNAL="${RUN_DIR}/dev_backend.${PORT}.journal"

# SERIALISE STARTUP. Two invocations a second apart raced during testing: both
# read the same pid file, both killed it, both wrote their own pid, and the one
# that LOST the bind wrote its pid last — so the file named a process that was not
# serving. The port then had one owner and the pid file another, which is exactly
# the ambiguity this whole change exists to remove.
#
# The lock covers free_port + the pid-file write only, and is released before exec
# so a later restart is never blocked by the instance it is replacing.
LOCKFILE="${RUN_DIR}/dev_backend.${PORT}.lock"
exec 9>"$LOCKFILE"
if ! flock -w 30 9; then
  echo "ERROR: another dev_backend.sh has held the startup lock for 30s" >&2
  echo "       (${LOCKFILE}) — refusing to race it." >&2
  exit 1
fi

note() {   # note <EVENT> <detail...>
  printf '%s %-18s %s\n' "$(date -Is)" "$1" "${*:2}" >> "$JOURNAL"
}

# Did the instance named by the PID file exit on its own?
#
# "On its own" means: the pid file exists, that process is gone, and the journal
# has no STOPPED record for it. The journal is what makes the distinction
# possible; without it every exit reads as intentional.
check_previous_exit() {
  [ -f "$PIDFILE" ] || return 0
  local prev
  prev=$(cat "$PIDFILE" 2>/dev/null || true)
  [ -n "$prev" ] || return 0
  if kill -0 "$prev" 2>/dev/null; then
    return 0                      # still running; free_port deals with it
  fi
  if grep -q "STOPPED_BY_SCRIPT .*pid=${prev}\b" "$JOURNAL" 2>/dev/null; then
    echo "previous     : pid ${prev} stopped by this script (expected)"
  else
    note "UNATTRIBUTED_EXIT" "pid=${prev} port=${PORT}"
    echo "───────────────────────────────────────────────────────────────────────"
    echo "WARNING: the previous dev backend (pid ${prev}) EXITED ON ITS OWN."
    echo "         Nothing in this script stopped it. Not a code failure by"
    echo "         default — check ${JOURNAL} and the log tail before assuming."
    echo "───────────────────────────────────────────────────────────────────────"
  fi
}
check_previous_exit

echo "dev backend  : http://localhost:${PORT}"
echo "callback URIs: ${BACKEND_URL}/auth/microsoft/callback"
echo "               ${BACKEND_URL}/auth/google/callback"
echo "returns to   : ${FRONTEND_URL}"
echo "pid file     : ${PIDFILE}"
echo "journal      : ${JOURNAL}"
if [ "${DEV_AUTH_BYPASS}" = "true" ]; then
  if [ -z "${DEV_AUTH_USER}" ]; then
    echo "auth         : BYPASS REQUESTED but DEV_AUTH_USER is unset — every request will 500"
  else
    echo "auth         : BYPASSED (loopback only) as ${DEV_AUTH_USER}"
  fi
fi
echo

# ── Free the port before binding ─────────────────────────────────────────────
# Match on WHAT IS LISTENING, never on a cmdline grep. A uvicorn --reload worker
# is a multiprocessing spawn child whose argv no longer contains "uvicorn", so
# `ps | grep uvicorn | kill` silently misses it. The socket stays held, the new
# instance fails to bind, and the STALE process keeps serving — health checks
# pass, and you spend an hour testing code that isn't running. That has happened
# twice. `ss -lptn 'sport = :PORT'` names the actual holder.
free_port() {
  local port="$1" pids pid ppid pcomm
  # `|| true`: grep exits non-zero when nothing is listening, and under
  # `set -e` a failing command substitution would abort the whole script.
  pids=$(ss -lptnH "sport = :${port}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u || true)
  [ -z "$pids" ] && { echo "port ${port} is free"; return 0; }
  echo "port ${port} is held by pid(s): $(echo "$pids" | tr '\n' ' ')"

  # SIGNAL ONLY THE RELOADER, and prefer the one we recorded.
  #
  # uvicorn's reloader shuts its worker down on SIGTERM, so one signal to the
  # parent is enough. The old path signalled every listening pid and then walked
  # up to each parent, which worked but made every stop look the same in the log.
  # Naming the pid we started is what lets an exit be attributed later.
  local recorded=""
  [ -f "$PIDFILE" ] && recorded=$(cat "$PIDFILE" 2>/dev/null || true)

  if [ -n "$recorded" ] && kill -0 "$recorded" 2>/dev/null \
     && echo "$pids" | grep -qx "$recorded"; then
    echo "  -> ours (pid ${recorded}); sending SIGTERM to the reloader only"
    note "STOPPED_BY_SCRIPT" "pid=${recorded} port=${port} reason=restart"
    kill -TERM "$recorded" 2>/dev/null || true
  else
    # Someone else's process, or ours started outside this script. Say so — the
    # port is shared with another project's @reboot cron, so "not ours" is a real
    # possibility rather than a theoretical one.
    echo "  -> NOT started by this script (no matching pid file)."
    for pid in $pids; do
      echo "     pid ${pid}: $(readlink /proc/$pid/cwd 2>/dev/null || echo '?')"
    done
    note "STOPPED_FOREIGN" "pids=$(echo "$pids" | tr '\n' ' ') port=${port}"
    for pid in $pids; do
      ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
      if [ -n "$ppid" ] && [ "$ppid" != "1" ]; then
        pcomm=$(ps -o comm= -p "$ppid" 2>/dev/null | tr -d ' ')
        case "$pcomm" in python*|uvicorn*) kill "$ppid" 2>/dev/null || true ;; esac
      fi
      kill "$pid" 2>/dev/null || true
    done
  fi
  for _ in $(seq 1 20); do
    ss -lptnH "sport = :${port}" 2>/dev/null | grep -q . || { echo "port ${port} free"; return 0; }
    sleep 0.5
  done
  echo "port ${port} still held after SIGTERM — escalating to SIGKILL"
  for pid in $(ss -lptnH "sport = :${port}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u || true); do
    kill -9 "$pid" 2>/dev/null || true
  done
  sleep 1
  if ss -lptnH "sport = :${port}" 2>/dev/null | grep -q .; then
    echo "ERROR: port ${port} is still held; refusing to start a second instance" >&2
    exit 1
  fi
  echo "port ${port} free"
}
free_port "$PORT"

# --reload defaults to watching the whole working directory, which here means
# .venv/, node_modules/, qdrant_data/ and memory/ — tens of thousands of inotify
# watches, enough to starve vite ("ENOSPC: System limit for number of file
# watchers reached"). Watch only the Python source we actually edit.
# $$ survives exec: the shell's pid becomes the uvicorn reloader's pid, so this
# file names the process that must be signalled to stop the whole thing.
echo "$$" > "$PIDFILE"
note "STARTED" "pid=$$ port=${PORT}"
echo "reloader pid : $$"
echo

# Release the startup lock; from here uvicorn owns the port and the pid file
# names this process. Closing the fd is what releases it — `exec` would inherit
# an open one and hold the lock for the life of the server, blocking restarts.
exec 9>&-

exec .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port "${PORT}" --reload \
  --reload-dir backend --reload-dir config --reload-dir integrations \
  --reload-dir reports --reload-dir tasks --reload-dir scheduler
