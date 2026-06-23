# Aganeti AI — Project Overview

This folder is an **independent, code-grounded read** of the `aganetiAi` repository (branch
`main`, sampled 2026-06-23). Nothing here is aspirational; every claim points at a real
file/line in the codebase. The goal: anyone can pick up the project cold and know what's
built, what's broken, and what to do next.

## Read in order

1. `SCOPE.md` — what the product actually is, in plain English.
2. `ARCHITECTURE.md` — the technical map: services, modules, data flow, endpoints.
3. `CURRENT_GAPS.md` — audit of things that don't work or won't survive a fresh clone.
   **Read this before any deployment.**
4. `DGX_MIGRATION.md` — concrete plan to move the project from the Google cloud VM onto
   the NVIDIA DGX Spark (`matrix@192.168.1.155`).
5. `IMPROVEMENTS.md` — prioritized cleanup and hardening for the existing codebase.
6. `ROADMAP.md` — proposed *additional* features, grouped by ambition tier.

## TL;DR

`aganetiAi` is a **self-hosted, multi-user personal AI office assistant** ("Aria") that
sits between the user and their day: Microsoft 365 mail + calendar, a task list, an
internal agent-to-agent delegation channel, voice transcription, and PDF report
generation — all driven by a local llama.cpp LLM with Qdrant for RAG and APScheduler
for cron-style background jobs. The primary UI is a Telegram bot; the Streamlit screen
is a legacy email-approval queue.
