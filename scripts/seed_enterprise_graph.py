#!/usr/bin/env python3
"""
Seed Neo4j with a realistic enterprise knowledge graph for an AI company.

Why this exists: the graph shipped with 12 fixture nodes, which is enough to prove
the plumbing works and useless for judging retrieval. Ranking, fusion weights and
multi-hop expansion only misbehave at scale — a two-hop query on a 12-node graph
reaches everything, so nothing is ever wrongly pruned. This builds a graph with
realistic fan-out, hubs, aliases and conflicting sources so those failures can
actually surface.

    python scripts/seed_enterprise_graph.py            # seed, then verify
    python scripts/seed_enterprise_graph.py --dry-run  # plan only, no writes
    python scripts/seed_enterprise_graph.py --verify-only
    python scripts/seed_enterprise_graph.py --json     # machine-readable summary

Design constraints, all load-bearing:

  * **MERGE only.** Every write goes through BatchKnowledgeGraphBuilder, which
    MERGEs on (:Entity {id}). No CREATE anywhere, so re-running is a no-op on
    structure — it only bumps `observations` and refreshes `last_seen`.
  * **Stable ids, no UUIDs.** A node's id is `slugify(canonical_name)`, exactly
    what the ingestion pipeline would mint for the same name. That is what lets
    the seed merge onto entities the real extractor already created (the eleven
    fixture nodes are absorbed, not duplicated) and what makes the run repeatable.
  * **Deterministic fan-out.** All variation comes from `Random(SEED)` and fixed
    date arithmetic. No `datetime.now()`, no `uuid4()` — two runs produce byte
    identical ids, timestamps and edge sets.
  * **No new Cypher.** This script calls the builder, which calls GraphService.
    Retrieval and GraphService are untouched.

Provenance is real rather than decorative: facts are asserted by ~60 distinct
KnowledgeSources (architecture docs, meeting notes, OCR pages, tender packs,
emails), and the important ones are asserted by several. That is what populates
`source_ids`, drives `observations`, and makes `confidence` a max-over-sources
instead of a constant — which in turn is what the corroboration signal reads.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from random import Random
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.knowledge_graph import bootstrap                       # noqa: E402
from backend.knowledge_graph.builder import (                       # noqa: E402
    BatchKnowledgeGraphBuilder,
    entity_id_for,
)
from backend.knowledge_graph.models import NodeLabel, RelType       # noqa: E402
from backend.knowledge_graph.provenance import Provenance           # noqa: E402
from backend.knowledge_graph.retrieval.registry import (            # noqa: E402
    CanonicalEntityRegistry,
)
from backend.knowledge_graph.service import get_graph_service       # noqa: E402
from backend.knowledge_graph.sources import KnowledgeSource         # noqa: E402
from backend.knowledge_graph.types import (                         # noqa: E402
    ExtractedEntity,
    ExtractedRelationship,
)

SEED = 20260804
SEED_TAG = "enterprise-seed-v1"
BASE_DAY = date(2026, 1, 5)          # a Monday; the fiscal year the content sits in
EXTRACTOR_MODEL = "qwen-fast"

rng = Random(SEED)


def stamp(offset_days: int, hour: int = 9) -> str:
    """A fixed ISO timestamp `offset_days` after BASE_DAY.

    Derived rather than sampled so the same run always writes the same
    first_seen/last_seen values — otherwise every re-run would look like new
    evidence and `observations` would stop meaning anything.
    """
    d = BASE_DAY + timedelta(days=offset_days)
    return f"{d.isoformat()}T{hour:02d}:00:00+00:00"


# ── vocabulary shorthand ──────────────────────────────────────────────────────

PERSON, PROJECT, TECH = NodeLabel.PERSON.value, NodeLabel.PROJECT.value, NodeLabel.TECHNOLOGY.value
MODEL, SERVER, DOC = NodeLabel.MODEL.value, NodeLabel.SERVER.value, NodeLabel.DOCUMENT.value
EMAIL, MEETING, TASK = NodeLabel.EMAIL.value, NodeLabel.MEETING.value, NodeLabel.TASK.value
ORG, COMPANY, SERVICE = NodeLabel.ORGANIZATION.value, NodeLabel.COMPANY.value, NodeLabel.SERVICE.value
API, PROVIDER, CALENDAR = NodeLabel.API.value, NodeLabel.PROVIDER.value, NodeLabel.CALENDAR.value


@dataclass(slots=True)
class Spec:
    """A node the world contains, declared once and materialised per source."""

    name: str
    label: str
    secondary: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    props: dict[str, Any] = field(default_factory=dict)


CATALOG: dict[str, Spec] = {}


def ent(name: str, label: str, *, secondary: Iterable[str] = (),
        aliases: Iterable[str] = (), **props: Any) -> str:
    """Declare an entity and return its name, so declarations read as expressions.

    Re-declaring a name merges the extra aliases/props instead of replacing the
    spec — several sections legitimately mention the same entity.
    """
    existing = CATALOG.get(name)
    if existing:
        existing.aliases = tuple(dict.fromkeys(existing.aliases + tuple(aliases)))
        existing.props.update(props)
        return name
    CATALOG[name] = Spec(name=name, label=label, secondary=tuple(secondary),
                         aliases=tuple(aliases), props=dict(props))
    return name


# ══════════════════════════════════════════════════════════════════════════════
# The company
# ══════════════════════════════════════════════════════════════════════════════

MEERANA = ent("Meerana", COMPANY, aliases=("Meerana AI", "Meerana Technologies"),
              industry="Applied AI", headquarters="Dubai, UAE", founded="2021")

# ── clients ───────────────────────────────────────────────────────────────────
CLIENTS = [
    ent("Ajman Police", ORG, aliases=("Ajman Police GHQ", "AJP"),
        sector="Public Safety", country="UAE", relationship="client"),
    ent("Dubai Parliament", ORG, aliases=("Dubai Parliament Secretariat",),
        sector="Government", country="UAE", relationship="client"),
    ent("Ministry of Economy & Tourism", ORG,
        aliases=("Ministry of Economy and Tourism", "MoET", "Ministry of Economy"),
        sector="Government", country="UAE", relationship="client"),
    ent("Dar Al Ber Society", ORG, aliases=("Dar Al Ber", "DAB"),
        sector="Non-profit", country="UAE", relationship="client"),
    ent("Dubai Municipality", ORG, aliases=("DM",),
        sector="Government", country="UAE", relationship="client"),
    ent("Sharjah Airport Authority", ORG, aliases=("SAA", "Sharjah Airport"),
        sector="Aviation", country="UAE", relationship="client"),
]

# ── vendors ───────────────────────────────────────────────────────────────────
VENDORS = [
    ent("NVIDIA", COMPANY, secondary=(PROVIDER,), aliases=("Nvidia Corporation",),
        category="hardware", relationship="vendor"),
    ent("Huawei", COMPANY, secondary=(PROVIDER,), aliases=("Huawei Technologies",),
        category="hardware", relationship="vendor"),
    ent("Microsoft", COMPANY, secondary=(PROVIDER,), aliases=("Microsoft Corporation", "MSFT"),
        category="cloud", relationship="vendor"),
    ent("Google", COMPANY, secondary=(PROVIDER,), aliases=("Google LLC",),
        category="cloud", relationship="vendor"),
    ent("Anthropic", COMPANY, secondary=(PROVIDER,), category="model-provider", relationship="vendor"),
    ent("OpenAI", COMPANY, secondary=(PROVIDER,), category="model-provider", relationship="vendor"),
    ent("Alibaba Cloud", COMPANY, secondary=(PROVIDER,), aliases=("Alibaba", "Aliyun"),
        category="model-provider", relationship="vendor"),
    ent("Meta AI", COMPANY, secondary=(PROVIDER,), aliases=("Meta", "FAIR"),
        category="model-provider", relationship="vendor"),
    ent("Neo4j Inc.", COMPANY, secondary=(PROVIDER,), aliases=("Neo4j Incorporated",),
        category="database", relationship="vendor"),
    ent("Qdrant Solutions", COMPANY, secondary=(PROVIDER,), aliases=("Qdrant Inc",),
        category="database", relationship="vendor"),
    ent("Supabase Inc.", COMPANY, secondary=(PROVIDER,), aliases=("Supabase Incorporated",),
        category="database", relationship="vendor"),
]

# ══════════════════════════════════════════════════════════════════════════════
# People
# ══════════════════════════════════════════════════════════════════════════════

LEAD = ent("Akshay", PERSON, aliases=("Akshay N", "Akshay Nath"),
           role="Head of AI Engineering", department="AI Platform", employer="Meerana",
           seniority="lead")

_DEVS = [
    ("Rohit Menon", "Backend Engineer", "AI Platform"),
    ("Sneha Pillai", "Backend Engineer", "AI Platform"),
    ("Vishnu Raj", "Platform Engineer", "Infrastructure"),
    ("Fatima Al Marzooqi", "Frontend Engineer", "Product"),
    ("Karthik Iyer", "ML Engineer", "Applied Research"),
    ("Deepa Nair", "Data Engineer", "AI Platform"),
    ("Anas Kareem", "Computer Vision Engineer", "Vision"),
    ("Priya Sundaram", "ML Engineer", "Applied Research"),
    ("Mohammed Rashid", "DevOps Engineer", "Infrastructure"),
    ("Tanvi Desai", "Frontend Engineer", "Product"),
    ("Joseph Mathew", "Backend Engineer", "Integrations"),
    ("Lakshmi Warrier", "QA Engineer", "Quality"),
    ("Arun Prakash", "ML Engineer", "Applied Research"),
    ("Nikhil Verma", "Computer Vision Engineer", "Vision"),
    ("Sara Haddad", "NLP Engineer", "Applied Research"),
    ("Yusuf Ahmed", "Security Engineer", "Infrastructure"),
]
DEVELOPERS = [ent(n, PERSON, role=r, department=d, employer="Meerana", seniority="engineer")
              for n, r, d in _DEVS]

_PMS = [
    ("Meera Satheesan", "Delivery Manager"),
    ("Abhinav Kurup", "Technical Program Manager"),
    ("Ayesha Khan", "Project Manager"),
    ("Ravi Shankar", "Project Manager"),
    ("Hend Al Suwaidi", "Client Partner"),
    ("Sanjay Pillai", "Engineering Manager"),
]
MANAGERS = [ent(n, PERSON, role=r, department="Delivery", employer="Meerana", seniority="manager")
            for n, r in _PMS]

_CLIENT_CONTACTS = [
    ("Col. Saeed Al Nuaimi", "Ajman Police", "Director of Operations"),
    ("Maj. Khalid Al Shamsi", "Ajman Police", "Head of Command Centre"),
    ("Lt. Mariam Al Zaabi", "Ajman Police", "Analytics Officer"),
    ("Dr. Noura Al Hammadi", "Dubai Parliament", "Director of Digital Transformation"),
    ("Omar Bin Sulayem", "Dubai Parliament", "Head of Records"),
    ("Hessa Al Falasi", "Ministry of Economy & Tourism", "Head of Innovation"),
    ("Sultan Al Mheiri", "Ministry of Economy & Tourism", "Data Governance Lead"),
    ("Ibrahim Yousuf", "Dar Al Ber Society", "IT Director"),
    ("Latifa Al Ali", "Dar Al Ber Society", "Programme Manager"),
    ("Rashid Al Blooshi", "Dubai Municipality", "Smart City Lead"),
    ("Aisha Al Suwaidi", "Sharjah Airport Authority", "Head of Security Systems"),
    ("Faisal Al Qassimi", "Sharjah Airport Authority", "CIO"),
]
CLIENT_CONTACTS = [ent(n, PERSON, role=r, employer=o, seniority="client")
                   for n, o, r in _CLIENT_CONTACTS]

_VENDOR_CONTACTS = [
    ("Daniel Kim", "NVIDIA", "Solutions Architect"),
    ("Wei Zhang", "Huawei", "Field Engineer"),
    ("Elena Petrova", "Microsoft", "Cloud Solution Architect"),
    ("Tom Baker", "Qdrant Solutions", "Developer Advocate"),
    ("Ingrid Larsen", "Neo4j Inc.", "Graph Consultant"),
    ("Marco Rossi", "Supabase Inc.", "Support Engineer"),
    ("Chen Liu", "Alibaba Cloud", "Model Partnerships"),
    ("Sofia Almeida", "Google", "Customer Engineer"),
]
VENDOR_CONTACTS = [ent(n, PERSON, role=r, employer=o, seniority="vendor")
                   for n, o, r in _VENDOR_CONTACTS]

STAFF = [LEAD] + DEVELOPERS + MANAGERS
ALL_PEOPLE = STAFF + CLIENT_CONTACTS + VENDOR_CONTACTS

# ══════════════════════════════════════════════════════════════════════════════
# Projects
# ══════════════════════════════════════════════════════════════════════════════

# `Ajman Police` and `Dubai Parliament` are named above as client organisations
# and here as projects. That is deliberate and it is what the multi-label model is
# for: one node, two labels, one id — not two nodes that never resolve to each
# other. Re-declaring adds the Project label via `secondary` below.
AGENTIC = ent("Agentic AI", PROJECT, aliases=("Agentic AI Platform", "AganetiAI", "Aganeti AI"),
              status="active", client="Meerana", started="2025-11-03", tier="flagship")
NAZO = ent("Nazo", PROJECT, aliases=("Nazo Assistant", "Project Nazo"),
           status="active", client="Meerana", started="2026-01-12", tier="flagship")
CCTV = ent("CCTV Analytics", PROJECT, aliases=("CCTV Analytics Platform", "Video Analytics"),
           status="active", client="Ajman Police", started="2025-06-02", tier="flagship")
AJMAN_P = ent("Ajman Police", ORG, secondary=(PROJECT,), status="active", tier="flagship",
              started="2025-06-02", engagement="command centre modernisation")
PARLIAMENT = ent("Dubai Parliament", ORG, secondary=(PROJECT,), status="active", tier="flagship",
                 started="2025-09-15", engagement="records digitisation")
VIDEO_IDX = ent("Video Indexing", PROJECT, aliases=("Video Indexing Pipeline", "VidIndex"),
                status="active", client="Sharjah Airport Authority", started="2025-08-11",
                tier="flagship")

SUBPROJECTS = [
    ent("Hermes Gateway", PROJECT, aliases=("Hermes",), status="active", parent="Agentic AI"),
    ent("Dar Al Ber Dashboard", PROJECT, aliases=("DAB Dashboard",), status="active",
        client="Dar Al Ber Society"),
    ent("Knowledge Graph", PROJECT, aliases=("KG", "Knowledge Graph Layer"),
        status="active", parent="Agentic AI"),
    ent("Context Engine", PROJECT, aliases=("Hybrid Context Engine",), status="active",
        parent="Agentic AI"),
    ent("Evaluation Framework", PROJECT, aliases=("Eval Framework", "Evals"),
        status="active", parent="Agentic AI"),
    ent("Voice Assistant", PROJECT, aliases=("Voice Agent",), status="active", parent="Nazo"),
    ent("OCR Pipeline", PROJECT, aliases=("Document OCR",), status="active",
        parent="Dubai Parliament"),
    ent("Tender Automation", PROJECT, aliases=("Tender Bot",), status="planning",
        client="Ministry of Economy & Tourism"),
]

HEADLINE_PROJECTS = [AGENTIC, NAZO, CCTV, AJMAN_P, PARLIAMENT, VIDEO_IDX]
PROJECTS = HEADLINE_PROJECTS + SUBPROJECTS

# ══════════════════════════════════════════════════════════════════════════════
# Technologies, models, infrastructure
# ══════════════════════════════════════════════════════════════════════════════

_TECH = [
    ("Neo4j", ("Neo4j Graph Database", "neo4j"), "graph-database"),
    ("Qdrant", ("Qdrant Vector Database", "qdrant"), "vector-database"),
    ("PostgreSQL", ("Postgres", "postgres", "PG"), "relational-database"),
    ("Redis", ("redis",), "cache"),
    ("LiteLLM", ("LiteLLM Proxy", "LiteLLM Gateway", "litellm"), "llm-gateway"),
    ("vLLM", ("vllm", "VLLM"), "inference-server"),
    ("FastAPI", ("fastapi",), "web-framework"),
    ("Django", ("django",), "web-framework"),
    ("YOLO", ("Ultralytics YOLO",), "vision-framework"),
    ("WhisperX", ("Whisper X", "whisperx"), "speech-framework"),
    ("Microsoft Graph", ("MS Graph", "Graph API", "msgraph"), "api"),
    ("LangGraph", ("Lang Graph",), "orchestration"),
    ("Supabase", ("supabase",), "backend-as-a-service"),
    ("Docker", ("docker",), "containerisation"),
    ("Kubernetes", ("K8s", "k8s"), "orchestration"),
    ("Tailscale", ("tailscale",), "networking"),
    ("Nginx", ("nginx",), "reverse-proxy"),
    ("Celery", ("celery",), "task-queue"),
    ("APScheduler", ("Advanced Python Scheduler",), "scheduler"),
    ("Azure SQL", ("Azure SQL Database", "MSSQL"), "relational-database"),
    ("MinIO", ("minio",), "object-storage"),
    ("Prometheus", ("prometheus",), "observability"),
    ("Grafana", ("grafana",), "observability"),
    ("fastembed", ("Fastembed",), "embedding-library"),
    ("ONNX Runtime", ("onnxruntime", "ORT"), "inference-runtime"),
    ("TensorRT", ("TensorRT-LLM",), "inference-runtime"),
    ("DeepStream", ("NVIDIA DeepStream",), "video-pipeline"),
    ("RTSP", ("Real Time Streaming Protocol",), "protocol"),
    ("React", ("ReactJS",), "frontend-framework"),
    ("Vite", ("vite",), "build-tool"),
    ("TypeScript", ("TS",), "language"),
    ("SQLAlchemy", ("sqlalchemy",), "orm"),
    ("Alembic", ("alembic",), "migrations"),
    ("Pydantic", ("pydantic",), "validation"),
    ("Gemma", ("Google Gemma",), "model-family"),
    ("Qwen", ("Alibaba Qwen", "QWen"), "model-family"),
    ("Llama", ("LLaMA", "Meta Llama"), "model-family"),
]
TECHNOLOGIES = [ent(n, TECH, aliases=a, category=c) for n, a, c in _TECH]

# Microsoft Graph is an API as well as a technology — the same multi-label point.
CATALOG["Microsoft Graph"].secondary = (API,)

_MODELS = [
    ("Gemma-3", ("Gemma 3", "gemma3"), "Google", "text", "27B"),
    ("Qwen3", ("Qwen 3", "qwen3"), "Alibaba Cloud", "text", "32B"),
    ("Qwen3-VL", ("Qwen3 VL", "Qwen-VL", "qwen-vl"), "Alibaba Cloud", "vision-language", "32B"),
    ("WhisperX Large-v3", ("Whisper Large v3", "whisperx-large-v3"), "OpenAI", "speech", "1.5B"),
    ("BGE-M3", ("bge-m3", "BGE M3"), "Meerana", "embedding", "568M"),
    ("YOLOv8", ("YOLO v8", "yolov8"), "Meerana", "detection", "68M"),
    ("NLLB", ("NLLB-200", "No Language Left Behind"), "Meta AI", "translation", "3.3B"),
    ("qwen-fast", ("Qwen Fast", "qwen_fast"), "Alibaba Cloud", "text", "32B"),
    ("BGE-small-en-v1.5", ("bge-small-en", "BAAI/bge-small-en-v1.5"), "Meerana", "embedding", "33M"),
    ("Llama-3.3-70B", ("Llama 3.3", "llama-3.3-70b"), "Meta AI", "text", "70B"),
    ("Gemma-3-4B", ("Gemma 3 4B",), "Google", "text", "4B"),
    ("YOLOv8-Pose", ("YOLO Pose",), "Meerana", "pose-estimation", "68M"),
    ("PaddleOCR", ("Paddle OCR",), "Alibaba Cloud", "ocr", "8M"),
    ("Qwen2.5-VL", ("Qwen 2.5 VL",), "Alibaba Cloud", "vision-language", "7B"),
]
MODELS = [ent(n, MODEL, aliases=a, vendor=v, modality=m, parameters=p)
          for n, a, v, m, p in _MODELS]

_SERVERS = [
    ("DGX Spark", ("DGX", "NVIDIA DGX Spark"), "gpu", "NVIDIA GB10", "128 GB"),
    ("RTX PRO 6000", ("RTX Pro 6000", "RTX6000"), "gpu", "NVIDIA Blackwell", "96 GB"),
    ("Huawei Ascend 910B", ("Ascend 910B", "Ascend"), "npu", "Huawei Ascend", "64 GB"),
    ("gpu-node-a", ("gpu node a",), "gpu", "NVIDIA A100", "80 GB"),
    ("gpu-node-b", ("gpu node b",), "gpu", "NVIDIA A100", "80 GB"),
    ("gcp-relay-vm", ("GCP Relay VM", "relay-vm"), "vm", "n2-standard-4", "16 GB"),
    ("azure-bridge-vm", ("Azure Bridge VM",), "vm", "Standard_D4s_v5", "16 GB"),
    ("cctv-ingest-01", ("CCTV Ingest 01",), "vm", "Standard_D8s_v5", "32 GB"),
    ("cctv-ingest-02", ("CCTV Ingest 02",), "vm", "Standard_D8s_v5", "32 GB"),
    ("edge-node-ajman-01", ("Ajman Edge 01",), "edge", "Jetson Orin", "32 GB"),
    ("edge-node-ajman-02", ("Ajman Edge 02",), "edge", "Jetson Orin", "32 GB"),
    ("edge-node-sharjah-01", ("Sharjah Edge 01",), "edge", "Jetson Orin", "32 GB"),
    ("db-primary-01", ("DB Primary",), "vm", "Standard_E8s_v5", "64 GB"),
    ("db-replica-01", ("DB Replica",), "vm", "Standard_E8s_v5", "64 GB"),
]
SERVERS = [ent(n, SERVER, aliases=a, kind=k, hardware=h, memory=m)
           for n, a, k, h, m in _SERVERS]

_SERVICES = [
    ("Hermes API Server", ("Hermes API",), "internal"),
    ("Chat Orchestrator", ("Orchestrator",), "internal"),
    ("Retrieval Service", ("Retriever",), "internal"),
    ("Ingestion Worker", ("Ingest Worker",), "internal"),
    ("Microsoft Entra ID", ("Entra ID", "Azure AD", "AAD"), "identity"),
    ("Google OAuth", ("Google Sign-In",), "identity"),
    ("Notification Service", ("Notifier",), "internal"),
    ("Report Generator", ("Reporting Service",), "internal"),
]
SERVICES = [ent(n, SERVICE, aliases=a, scope=s) for n, a, s in _SERVICES]

# ══════════════════════════════════════════════════════════════════════════════
# Generated content: documents, emails, meetings, tasks, calendar
# ══════════════════════════════════════════════════════════════════════════════

_DOC_KINDS = [
    ("architecture", "Architecture", ["Architecture Overview", "Service Topology",
                                      "Data Flow Design", "Scaling Plan"]),
    ("meeting_notes", "Meeting Notes", ["Kickoff Notes", "Sprint Review Notes",
                                        "Steering Committee Notes", "Retro Notes"]),
    ("ocr", "OCR Extract", ["Scanned Minutes", "Scanned Circular", "Scanned Form",
                            "Scanned Register"]),
    ("deployment", "Deployment", ["Deployment Runbook", "Rollback Procedure",
                                  "Environment Matrix", "Release Checklist"]),
    ("tender", "Tender", ["Tender Response", "Bill of Quantities", "Compliance Matrix",
                          "Commercial Proposal"]),
    ("research", "Research", ["Benchmark Report", "Model Evaluation Study",
                              "Literature Review", "Feasibility Study"]),
]

DOCUMENTS: list[tuple[str, str, str]] = []      # (name, kind, project)
for kind, label, titles in _DOC_KINDS:
    for project in PROJECTS:
        title = titles[len(DOCUMENTS) % len(titles)]
        name = f"{project} — {title}"
        ent(name, DOC, doc_type=kind, project=project, format="pdf" if kind == "ocr" else "docx",
            confidentiality="internal" if kind != "tender" else "restricted")
        DOCUMENTS.append((name, kind, project))

EMAILS: list[tuple[str, str, str]] = []         # (name, sender, project)
_EMAIL_SUBJECTS = [
    "Re: {p} status update", "{p} — action items", "Re: {p} deployment window",
    "{p} invoice and milestones", "Re: {p} access request", "{p} incident summary",
    "Re: {p} model performance", "{p} weekly digest",
]
for i in range(72):
    project = PROJECTS[i % len(PROJECTS)]
    sender = ALL_PEOPLE[(i * 7) % len(ALL_PEOPLE)]
    subject = _EMAIL_SUBJECTS[i % len(_EMAIL_SUBJECTS)].format(p=project)
    name = f"Email: {subject} [{i:03d}]"
    ent(name, EMAIL, subject=subject, sender=sender, project=project,
        sent_at=stamp(i * 3, 8 + i % 9))
    EMAILS.append((name, sender, project))

MEETINGS: list[tuple[str, str]] = []            # (name, project)
_MEETING_KINDS = ["Sprint Planning", "Steering Committee", "Technical Deep Dive",
                  "Client Demo", "Architecture Review", "Incident Review", "Retrospective"]
for i in range(48):
    project = PROJECTS[i % len(PROJECTS)]
    kind = _MEETING_KINDS[i % len(_MEETING_KINDS)]
    name = f"{kind}: {project} (W{i % 26 + 1:02d})"
    ent(name, MEETING, meeting_type=kind, project=project,
        scheduled_at=stamp(i * 5, 10 + i % 6), duration_minutes=30 + (i % 4) * 15)
    MEETINGS.append((name, project))

TASKS: list[tuple[str, str, str]] = []          # (name, project, assignee)
_TASK_VERBS = ["Implement", "Fix", "Benchmark", "Document", "Migrate", "Harden",
               "Refactor", "Deploy", "Investigate", "Optimise"]
_TASK_OBJECTS = ["ingestion retries", "token refresh", "graph expansion depth",
                 "vector recall", "OCR accuracy", "streaming latency", "alert routing",
                 "backup schedule", "index rebuild", "rate limiting", "audit logging",
                 "model failover", "context budget", "session isolation"]
for i in range(96):
    project = PROJECTS[i % len(PROJECTS)]
    assignee = STAFF[(i * 5) % len(STAFF)]
    name = f"{_TASK_VERBS[i % len(_TASK_VERBS)]} {_TASK_OBJECTS[i % len(_TASK_OBJECTS)]} ({project})"
    ent(name, TASK, project=project, assignee=assignee,
        status=["open", "in_progress", "blocked", "done"][i % 4],
        priority=["low", "medium", "high", "urgent"][i % 4],
        due_at=stamp(20 + i * 2, 17))
    TASKS.append((name, project, assignee))

CALENDAR_EVENTS: list[tuple[str, str]] = []
for i, project in enumerate(PROJECTS):
    name = f"Calendar: {project} delivery milestone"
    ent(name, CALENDAR, project=project, starts_at=stamp(30 + i * 11, 11))
    CALENDAR_EVENTS.append((name, project))


# ══════════════════════════════════════════════════════════════════════════════
# Facts — (subject, RelType, object) triples, grouped by the source asserting them
# ══════════════════════════════════════════════════════════════════════════════

Triple = tuple[str, str, str]


@dataclass(slots=True)
class Assertion:
    """One source and everything it claims. Confidence is per-source, not global."""

    source: KnowledgeSource
    triples: list[Triple]
    confidence: float = 0.9


def _tech_stack() -> list[Triple]:
    """The platform architecture — the densest, most-corroborated part of the graph."""
    t: list[Triple] = []
    core = {
        AGENTIC: ["LiteLLM", "Qdrant", "Neo4j", "PostgreSQL", "FastAPI", "LangGraph",
                  "Redis", "Supabase", "Microsoft Graph", "APScheduler", "Pydantic",
                  "SQLAlchemy", "fastembed"],
        NAZO: ["FastAPI", "WhisperX", "LiteLLM", "Redis", "React", "TypeScript", "Vite"],
        CCTV: ["YOLO", "DeepStream", "RTSP", "Docker", "Kubernetes", "MinIO", "PostgreSQL"],
        AJMAN_P: ["YOLO", "DeepStream", "Kubernetes", "Grafana", "Prometheus"],
        PARLIAMENT: ["Django", "PostgreSQL", "MinIO", "Celery", "Nginx"],
        VIDEO_IDX: ["WhisperX", "Qdrant", "MinIO", "Celery", "ONNX Runtime"],
        "Hermes Gateway": ["FastAPI", "LiteLLM", "Redis", "Nginx"],
        "Dar Al Ber Dashboard": ["React", "Vite", "TypeScript", "Azure SQL", "FastAPI"],
        "Knowledge Graph": ["Neo4j", "LiteLLM", "fastembed", "Pydantic"],
        "Context Engine": ["Qdrant", "Neo4j", "PostgreSQL", "Redis"],
        "Evaluation Framework": ["PostgreSQL", "Qdrant", "Neo4j"],
        "Voice Assistant": ["WhisperX", "LiteLLM", "FastAPI"],
        "OCR Pipeline": ["PaddleOCR", "MinIO", "Celery", "Django"],
        "Tender Automation": ["LiteLLM", "Qdrant", "FastAPI"],
    }
    for project, stack in core.items():
        for tech in stack:
            t.append((project, RelType.USES.value, tech))

    # Model routing: the gateway is the only thing that talks to a model.
    for m in ["qwen-fast", "Qwen3", "Qwen3-VL", "Gemma-3", "Llama-3.3-70B", "Gemma-3-4B"]:
        t.append(("LiteLLM", RelType.ROUTES_TO.value, m))
        t.append((m, RelType.HOSTED_ON.value, "vLLM"))
    t.append(("vLLM", RelType.DEPLOYED_ON.value, "DGX Spark"))
    t.append(("vLLM", RelType.DEPLOYED_ON.value, "RTX PRO 6000"))
    t.append(("LiteLLM", RelType.DEPLOYED_ON.value, "gpu-node-a"))

    # Dependencies between components.
    deps = [
        ("LiteLLM", "vLLM"), ("Knowledge Graph", "Neo4j"), ("Context Engine", "Knowledge Graph"),
        ("Evaluation Framework", "Context Engine"), ("Hermes Gateway", "LiteLLM"),
        ("Agentic AI", "Hermes Gateway"), ("Agentic AI", "Knowledge Graph"),
        ("Agentic AI", "Context Engine"), ("Nazo", "Agentic AI"),
        ("Voice Assistant", "WhisperX"), ("OCR Pipeline", "PaddleOCR"),
        ("CCTV Analytics", "YOLO"), ("Video Indexing", "WhisperX"),
        ("Ajman Police", "CCTV Analytics"), ("Dubai Parliament", "OCR Pipeline"),
        ("Tender Automation", "Agentic AI"), ("Dar Al Ber Dashboard", "Azure SQL"),
        ("fastembed", "ONNX Runtime"), ("DeepStream", "TensorRT"),
        ("Qdrant", "fastembed"), ("Celery", "Redis"), ("Alembic", "SQLAlchemy"),
        ("SQLAlchemy", "PostgreSQL"), ("Grafana", "Prometheus"),
    ]
    t += [(a, RelType.DEPENDS_ON.value, b) for a, b in deps]

    # Storage and data movement.
    stores = [("Agentic AI", "PostgreSQL"), ("Context Engine", "Qdrant"),
              ("Knowledge Graph", "Neo4j"), ("Video Indexing", "MinIO"),
              ("OCR Pipeline", "MinIO"), ("Dar Al Ber Dashboard", "Azure SQL"),
              ("CCTV Analytics", "PostgreSQL")]
    t += [(a, RelType.STORES.value, b) for a, b in stores]
    t += [("Retrieval Service", RelType.READS.value, x) for x in ("Qdrant", "Neo4j", "PostgreSQL")]
    t += [("Ingestion Worker", RelType.WRITES.value, x) for x in ("Qdrant", "Neo4j", "MinIO")]
    t.append(("Chat Orchestrator", RelType.READS.value, "Redis"))
    t.append(("Report Generator", RelType.READS.value, "Azure SQL"))
    t.append(("Notification Service", RelType.WRITES.value, "PostgreSQL"))

    # Capability implementation — what a model actually does.
    implements = [("YOLOv8", "CCTV Analytics"), ("YOLOv8-Pose", "CCTV Analytics"),
                  ("WhisperX Large-v3", "Video Indexing"), ("PaddleOCR", "OCR Pipeline"),
                  ("BGE-M3", "Context Engine"), ("BGE-small-en-v1.5", "Context Engine"),
                  ("Qwen3-VL", "Video Indexing"), ("NLLB", "OCR Pipeline"),
                  ("qwen-fast", "Agentic AI"), ("Qwen2.5-VL", "CCTV Analytics")]
    t += [(m, RelType.IMPLEMENTS.value, p) for m, p in implements]

    # Processing chains — what consumes what.
    processes = [("YOLOv8", "RTSP"), ("DeepStream", "RTSP"), ("WhisperX Large-v3", "MinIO"),
                 ("PaddleOCR", "MinIO"), ("Ingestion Worker", "MinIO"),
                 ("fastembed", "PostgreSQL"), ("Qwen3-VL", "MinIO")]
    t += [(a, RelType.PROCESSES.value, b) for a, b in processes]

    # Services and identity.
    t += [("Agentic AI", RelType.USES.value, s) for s in
          ("Hermes API Server", "Chat Orchestrator", "Retrieval Service", "Ingestion Worker")]
    t.append(("Agentic AI", RelType.AUTHENTICATES_WITH.value, "Microsoft Entra ID"))
    t.append(("Agentic AI", RelType.AUTHENTICATES_WITH.value, "Google OAuth"))
    t.append(("Microsoft Graph", RelType.CONNECTED_TO.value, "Microsoft Entra ID"))
    t.append(("Nazo", RelType.USES.value, "Notification Service"))
    t.append(("Dar Al Ber Dashboard", RelType.USES.value, "Report Generator"))

    # Infrastructure topology.
    hosts = [("Hermes API Server", "gpu-node-a"), ("Chat Orchestrator", "gpu-node-a"),
             ("Retrieval Service", "gpu-node-b"), ("Ingestion Worker", "gpu-node-b"),
             ("Neo4j", "db-primary-01"), ("PostgreSQL", "db-primary-01"),
             ("Qdrant", "db-replica-01"), ("Redis", "db-replica-01"),
             ("MinIO", "db-replica-01"), ("Report Generator", "azure-bridge-vm")]
    t += [(a, RelType.HOSTED_ON.value, b) for a, b in hosts]
    t += [("YOLOv8", RelType.DEPLOYED_ON.value, e) for e in
          ("edge-node-ajman-01", "edge-node-ajman-02", "edge-node-sharjah-01")]
    t += [("DeepStream", RelType.DEPLOYED_ON.value, s) for s in ("cctv-ingest-01", "cctv-ingest-02")]
    t.append(("WhisperX Large-v3", RelType.DEPLOYED_ON.value, "Huawei Ascend 910B"))
    t.append(("Gemma-3", RelType.DEPLOYED_ON.value, "Huawei Ascend 910B"))

    net = [("gcp-relay-vm", "azure-bridge-vm"), ("azure-bridge-vm", "db-primary-01"),
           ("db-primary-01", "db-replica-01"), ("edge-node-ajman-01", "cctv-ingest-01"),
           ("edge-node-ajman-02", "cctv-ingest-01"), ("edge-node-sharjah-01", "cctv-ingest-02"),
           ("cctv-ingest-01", "gpu-node-a"), ("cctv-ingest-02", "gpu-node-b"),
           ("gpu-node-a", "DGX Spark"), ("gpu-node-b", "RTX PRO 6000"),
           ("gcp-relay-vm", "gpu-node-a")]
    t += [(a, RelType.CONNECTED_TO.value, b) for a, b in net]
    t += [(a, RelType.USES.value, "Tailscale") for a, b in net[:6]]

    # Model families and their vendors.
    family = [("Gemma-3", "Gemma"), ("Gemma-3-4B", "Gemma"), ("Qwen3", "Qwen"),
              ("Qwen3-VL", "Qwen"), ("qwen-fast", "Qwen"), ("Qwen2.5-VL", "Qwen"),
              ("Llama-3.3-70B", "Llama"), ("YOLOv8", "YOLO"), ("YOLOv8-Pose", "YOLO"),
              ("WhisperX Large-v3", "WhisperX")]
    t += [(m, RelType.PART_OF.value, f) for m, f in family]
    for name, _a, vendor, _m, _p in _MODELS:
        t.append((vendor, RelType.OWNS.value, name))
    t += [("NVIDIA", RelType.OWNS.value, x) for x in ("DeepStream", "TensorRT")]
    t += [("Microsoft", RelType.OWNS.value, x) for x in ("Microsoft Graph", "Azure SQL",
                                                         "Microsoft Entra ID")]
    t += [("Google", RelType.OWNS.value, "Google OAuth"), ("Neo4j Inc.", RelType.OWNS.value, "Neo4j"),
          ("Qdrant Solutions", RelType.OWNS.value, "Qdrant"),
          ("Supabase Inc.", RelType.OWNS.value, "Supabase")]
    t += [("NVIDIA", RelType.OWNS.value, s) for s in ("DGX Spark", "RTX PRO 6000")]
    t.append(("Huawei", RelType.OWNS.value, "Huawei Ascend 910B"))
    return t


def _org_chart() -> list[Triple]:
    """People, reporting lines, and who is on what."""
    t: list[Triple] = []
    for m in MANAGERS:
        t.append((m, RelType.REPORTS_TO.value, LEAD))
    for i, dev in enumerate(DEVELOPERS):
        t.append((dev, RelType.REPORTS_TO.value, MANAGERS[i % len(MANAGERS)]))
    for p in STAFF:
        t.append((p, RelType.MEMBER_OF.value, MEERANA))
    for name, org, _role in _CLIENT_CONTACTS:
        t.append((name, RelType.MEMBER_OF.value, org))
    for name, org, _role in _VENDOR_CONTACTS:
        t.append((name, RelType.MEMBER_OF.value, org))

    # Assignments: every engineer on 2–3 projects, deterministically chosen.
    for i, dev in enumerate(DEVELOPERS):
        for j in range(2 + i % 2):
            t.append((dev, RelType.WORKS_ON.value, PROJECTS[(i * 3 + j * 5) % len(PROJECTS)]))
    for i, m in enumerate(MANAGERS):
        for j in range(3):
            t.append((m, RelType.WORKS_ON.value, PROJECTS[(i * 2 + j * 4) % len(PROJECTS)]))
    for p in HEADLINE_PROJECTS:
        t.append((LEAD, RelType.WORKS_ON.value, p))
        t.append((LEAD, RelType.OWNS.value, p))

    # Client ownership and sub-project containment.
    t += [("Ajman Police", RelType.OWNS.value, CCTV),
          ("Dubai Parliament", RelType.OWNS.value, "OCR Pipeline"),
          ("Ministry of Economy & Tourism", RelType.OWNS.value, "Tender Automation"),
          ("Dar Al Ber Society", RelType.OWNS.value, "Dar Al Ber Dashboard"),
          ("Sharjah Airport Authority", RelType.OWNS.value, VIDEO_IDX),
          ("Dubai Municipality", RelType.OWNS.value, NAZO),
          ("Meerana", RelType.OWNS.value, AGENTIC)]
    for sub in SUBPROJECTS:
        parent = CATALOG[sub].props.get("parent")
        if parent:
            t.append((sub, RelType.PART_OF.value, parent))
    for org in CLIENTS:
        t.append((MEERANA, RelType.RELATED_TO.value, org))
    for v in VENDORS:
        t.append((MEERANA, RelType.RELATED_TO.value, v))
    return t


def _content_graph() -> list[Triple]:
    """Documents, emails, meetings, tasks — the long tail that dominates the node count."""
    t: list[Triple] = []
    tech_pool = sorted(TECHNOLOGIES + MODELS + SERVERS)

    for i, (doc, kind, project) in enumerate(DOCUMENTS):
        t.append((doc, RelType.DOCUMENTS.value, project))
        t.append((STAFF[(i * 3) % len(STAFF)], RelType.AUTHORED.value, doc))
        t.append((project, RelType.OWNS.value, doc))
        for tech in rng.sample(tech_pool, 3):
            t.append((doc, RelType.REFERENCES.value, tech))
        if kind == "tender":
            t.append((doc, RelType.RELATED_TO.value, CATALOG[project].props.get("client") or MEERANA))
        if kind == "research":
            t.append((doc, RelType.REFERENCES.value, rng.choice(sorted(MODELS))))

    for i, (email, sender, project) in enumerate(EMAILS):
        t.append((sender, RelType.AUTHORED.value, email))
        t.append((email, RelType.MENTIONS.value, project))
        for tech in rng.sample(tech_pool, 2):
            t.append((email, RelType.MENTIONS.value, tech))
        t.append((email, RelType.RELATED_TO.value, DOCUMENTS[(i * 5) % len(DOCUMENTS)][0]))

    for i, (meeting, project) in enumerate(MEETINGS):
        t.append((project, RelType.DISCUSSED_IN.value, meeting))
        attendees = [STAFF[(i * 4 + k) % len(STAFF)] for k in range(4)]
        attendees.append(CLIENT_CONTACTS[(i * 3) % len(CLIENT_CONTACTS)])
        for person in attendees:
            t.append((person, RelType.ATTENDS.value, meeting))
        t.append((meeting, RelType.GENERATED.value, DOCUMENTS[(i * 7) % len(DOCUMENTS)][0]))
        for tech in rng.sample(tech_pool, 2):
            t.append((tech, RelType.DISCUSSED_IN.value, meeting))

    for i, (task, project, assignee) in enumerate(TASKS):
        t.append((task, RelType.ASSIGNED_TO.value, assignee))
        t.append((task, RelType.PART_OF.value, project))
        for tech in rng.sample(tech_pool, 2):
            t.append((task, RelType.RELATED_TO.value, tech))
        t.append((task, RelType.DISCUSSED_IN.value, MEETINGS[(i * 3) % len(MEETINGS)][0]))

    for i, (event, project) in enumerate(CALENDAR_EVENTS):
        t.append((event, RelType.RELATED_TO.value, project))
        t.append((LEAD, RelType.ATTENDS.value, event))
        t.append((MANAGERS[i % len(MANAGERS)], RelType.ATTENDS.value, event))
    return t


def build_world() -> list[Assertion]:
    """Split every fact across the sources that assert it.

    Corroboration is the point: the architecture triples are asserted by three
    different sources with different confidences, so `observations` climbs and
    `confidence` settles at the max. Content triples are asserted once — a
    passing mention in one email should NOT look as trustworthy as a fact that
    three architecture reviews agree on.
    """
    stack, org, content = _tech_stack(), _org_chart(), _content_graph()
    out: list[Assertion] = []

    # `created_at` is passed explicitly on every source: the convenience
    # constructors default it to now(), which would make each run write different
    # first_seen/last_seen values and destroy repeatability.
    def _doc(src_id: str, text: str, day: int) -> KnowledgeSource:
        return KnowledgeSource(id=src_id, type="document", text=text,
                               metadata={"user_id": "user_1", "document_id": src_id},
                               created_at=stamp(day))

    # Core architecture: three independent sources, deliberately overlapping.
    out.append(Assertion(
        _doc("doc-platform-architecture-v3",
             "Agentic AI platform architecture, revision 3.", 4),
        stack, confidence=0.95))
    out.append(Assertion(
        KnowledgeSource(id="meeting-architecture-review-q1", type="meeting",
                        text="Q1 architecture review minutes.",
                        metadata={"user_id": "user_1", "meeting_id": "meeting-architecture-review-q1"},
                        created_at=stamp(11)),
        stack[::2], confidence=0.88))
    out.append(Assertion(
        _doc("doc-deployment-runbook", "Production deployment runbook.", 18),
        [x for x in stack if x[1] in (RelType.DEPLOYED_ON.value, RelType.HOSTED_ON.value,
                                      RelType.CONNECTED_TO.value)],
        confidence=0.92))

    # Org chart: HR export plus a delivery plan that repeats the assignments.
    out.append(Assertion(
        _doc("doc-org-directory", "Meerana staff directory export.", 2), org, confidence=0.97))
    out.append(Assertion(
        _doc("doc-delivery-plan-2026", "2026 delivery plan and staffing.", 6),
        [x for x in org if x[1] in (RelType.WORKS_ON.value, RelType.OWNS.value)],
        confidence=0.9))

    # Content: sliced across many small sources so provenance is spread realistically.
    per_source = 40
    for i in range(0, len(content), per_source):
        chunk = content[i:i + per_source]
        idx = i // per_source
        kind = ("document", "email", "meeting", "ocr_text")[idx % 4]
        src_id = f"{kind}-batch-{idx:03d}"
        out.append(Assertion(
            KnowledgeSource(id=src_id, type=kind, text=f"Content batch {idx}.",
                            metadata={"user_id": "user_1"}, created_at=stamp(idx * 2)),
            chunk, confidence=round(0.72 + (idx % 5) * 0.05, 2)))
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Writing
# ══════════════════════════════════════════════════════════════════════════════

def _materialise(names: Iterable[str]) -> list[ExtractedEntity]:
    """Fresh ExtractedEntity objects for a source's batch.

    Fresh each time on purpose — the builder stamps `node_id` on the objects it
    writes, so sharing one instance across sources would leak state between
    batches for no benefit.
    """
    out = []
    for name in names:
        spec = CATALOG.get(name)
        if spec is None:                    # a triple naming an undeclared entity
            continue
        out.append(ExtractedEntity(
            name=spec.name, type=spec.label, canonical_name=spec.name,
            aliases=list(spec.aliases), secondary_labels=list(spec.secondary),
            properties={**spec.props, "seed_batch": SEED_TAG}))
    return out


def seed(*, dry_run: bool = False) -> dict[str, Any]:
    """Write the whole world. Idempotent — safe to run repeatedly."""
    world = build_world()
    planned_nodes: set[str] = set()
    planned_edges: set[tuple[str, str, str]] = set()
    dropped: list[str] = []

    for a in world:
        for s, r, o in a.triples:
            if s not in CATALOG or o not in CATALOG:
                dropped.append(f"{s}-{r}->{o}")
                continue
            planned_nodes.update((s, o))
            planned_edges.add((entity_id_for(s), r, entity_id_for(o)))

    plan = {"sources": len(world), "distinct_nodes": len(planned_nodes),
            "distinct_edges": len(planned_edges),
            "triple_assertions": sum(len(a.triples) for a in world),
            "dropped_triples": len(dropped)}
    if dry_run:
        plan["dropped_examples"] = dropped[:10]
        return plan

    svc = get_graph_service()
    bootstrap.bootstrap_schema(svc)

    totals = Counter()
    for a in world:
        triples = [(s, r, o) for s, r, o in a.triples if s in CATALOG and o in CATALOG]
        names = sorted({n for s, _r, o in triples for n in (s, o)})
        entities = _materialise(names)
        rels = [ExtractedRelationship(source=s, type=r, target=o) for s, r, o in triples]
        prov = Provenance.from_source(a.source, model=EXTRACTOR_MODEL, confidence=a.confidence)
        builder = BatchKnowledgeGraphBuilder(svc, source=a.source.type)
        result = builder.build(entities, rels, provenance=prov)
        totals["nodes_created"] += result.nodes_created
        totals["nodes_merged"] += result.nodes_merged
        totals["rels_created"] += result.relationships_created
        totals["rels_merged"] += result.relationships_merged
        totals["queries"] += result.graph_queries
        totals["skipped_entities"] += len(result.skipped_entities)
        totals["skipped_rels"] += len(result.skipped_relationships)

    plan.update(dict(totals))
    return plan


# ══════════════════════════════════════════════════════════════════════════════
# Verification
# ══════════════════════════════════════════════════════════════════════════════

def _components(nodes: list[str], edges: list[tuple[str, str]]) -> list[int]:
    """Connected-component sizes, treating edges as undirected.

    Done in Python rather than Cypher because GDS/APOC are not installed here and
    a few thousand edges is nothing — pulling them once beats a recursive query.
    """
    adj: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        adj[a].append(b)
        adj[b].append(a)
    seen: set[str] = set()
    sizes: list[int] = []
    for start in nodes:
        if start in seen:
            continue
        size, queue = 0, deque([start])
        seen.add(start)
        while queue:
            cur = queue.popleft()
            size += 1
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        sizes.append(size)
    return sorted(sizes, reverse=True)


def verify() -> dict[str, Any]:
    """Read the graph back and report what is actually there."""
    svc = get_graph_service()
    out: dict[str, Any] = {}

    out["node_count"] = svc.run_query("MATCH (n:Entity) RETURN count(n) AS c")[0]["c"]
    out["relationship_count"] = svc.run_query(
        "MATCH (:Entity)-[r]->(:Entity) RETURN count(r) AS c")[0]["c"]

    out["label_counts"] = {r["label"]: r["c"] for r in svc.run_query(
        "MATCH (n:Entity) UNWIND labels(n) AS label "
        "WITH label WHERE label <> 'Entity' "
        "RETURN label, count(*) AS c ORDER BY c DESC, label")}
    out["relationship_counts"] = {r["t"]: r["c"] for r in svc.run_query(
        "MATCH (:Entity)-[r]->(:Entity) RETURN type(r) AS t, count(*) AS c "
        "ORDER BY c DESC, t")}

    degrees = svc.run_query(
        "MATCH (n:Entity) RETURN n.id AS id, COUNT { (n)--(:Entity) } AS degree")
    values = sorted((d["degree"] for d in degrees), reverse=True)
    n = len(values) or 1
    buckets = Counter()
    for d in values:
        if d == 0:
            buckets["0"] += 1
        elif d <= 2:
            buckets["1-2"] += 1
        elif d <= 5:
            buckets["3-5"] += 1
        elif d <= 10:
            buckets["6-10"] += 1
        elif d <= 25:
            buckets["11-25"] += 1
        elif d <= 50:
            buckets["26-50"] += 1
        else:
            buckets["51+"] += 1
    order = ["0", "1-2", "3-5", "6-10", "11-25", "26-50", "51+"]
    out["degree_distribution"] = {k: buckets[k] for k in order if buckets[k]}
    out["average_degree"] = round(sum(values) / n, 2)
    out["median_degree"] = values[n // 2] if values else 0
    out["max_degree"] = values[0] if values else 0
    out["isolated_nodes"] = buckets["0"]
    out["hubs"] = [{"id": d["id"], "degree": d["degree"]}
                   for d in sorted(degrees, key=lambda x: (-x["degree"], x["id"]))[:10]]

    edges = [(r["a"], r["b"]) for r in svc.run_query(
        "MATCH (a:Entity)-[]->(b:Entity) RETURN a.id AS a, b.id AS b")]
    node_ids = [d["id"] for d in degrees]
    sizes = _components(node_ids, edges)
    out["components"] = len(sizes)
    out["largest_component"] = sizes[0] if sizes else 0
    out["largest_component_pct"] = round(100.0 * (sizes[0] if sizes else 0) / n, 1)

    # Provenance actually landed?
    prov = svc.run_query(
        "MATCH (n:Entity) RETURN "
        "  count(CASE WHEN size(coalesce(n.source_ids,[])) > 1 THEN 1 END) AS corroborated, "
        "  count(CASE WHEN n.first_seen IS NOT NULL THEN 1 END) AS with_first_seen, "
        "  count(CASE WHEN size(coalesce(n.aliases,[])) > 0 THEN 1 END) AS with_aliases, "
        "  avg(coalesce(n.observations,0)) AS avg_observations, "
        "  avg(coalesce(n.confidence,0.0)) AS avg_confidence")[0]
    out["provenance"] = {
        "corroborated_nodes": prov["corroborated"],
        "nodes_with_first_seen": prov["with_first_seen"],
        "nodes_with_aliases": prov["with_aliases"],
        "avg_observations": round(float(prov["avg_observations"] or 0), 2),
        "avg_confidence": round(float(prov["avg_confidence"] or 0), 3),
    }

    # Alias lookup through the real resolver, not a direct id match.
    registry = CanonicalEntityRegistry(svc)
    registry.warm(force=True)
    probes = ["MS Graph", "Postgres", "DGX", "AJP", "Qwen 3", "LiteLLM Gateway",
              "Dar Al Ber", "K8s", "Ajman Edge 01", "Aganeti AI", "bge-m3", "MoET"]
    out["alias_lookup"] = [
        {"probe": p, "resolved": r.entity_id or None,
         "canonical": r.canonical_name or None, "match": r.match_type}
        for p, r in ((p, registry.resolve(p)) for p in probes)]
    out["alias_hit_rate"] = round(
        100.0 * sum(1 for a in out["alias_lookup"] if a["resolved"]) / len(probes), 1)

    # Multi-hop: does traversal actually reach across the graph?
    hops: list[dict[str, Any]] = []
    for seed_name in ["Agentic AI", "Akshay", "Ajman Police", "DGX Spark"]:
        seed_id = entity_id_for(seed_name)
        frontier, visited = [seed_id], {seed_id}
        reach = []
        for depth in range(1, 4):
            rows = svc.expand_one_hop(frontier, sorted(visited), limit=4000)
            nxt = {r["node"].id for r in rows} - visited
            visited |= nxt
            reach.append({"depth": depth, "new_nodes": len(nxt), "cumulative": len(visited)})
            frontier = sorted(nxt)
            if not frontier:
                break
        hops.append({"seed": seed_name, "id": seed_id, "reach": reach})
    out["multi_hop"] = hops
    return out


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _print_report(plan: Optional[dict], stats: dict) -> None:
    B, D, R = "\033[1m", "\033[2m", "\033[0m"

    if plan:
        print(f"\n{B}Seed{R}")
        print(f"  sources                {plan['sources']}")
        print(f"  triple assertions      {plan['triple_assertions']}")
        print(f"  nodes created          {plan.get('nodes_created', 0)}")
        print(f"  nodes merged           {plan.get('nodes_merged', 0)}")
        print(f"  relationships created  {plan.get('rels_created', 0)}")
        print(f"  relationships merged   {plan.get('rels_merged', 0)}")
        print(f"  neo4j round trips      {plan.get('queries', 0)}")
        if plan.get("dropped_triples"):
            print(f"  {D}dropped triples        {plan['dropped_triples']}{R}")

    print(f"\n{B}Graph{R}")
    print(f"  nodes                  {stats['node_count']}")
    print(f"  relationships          {stats['relationship_count']}")
    print(f"  average degree         {stats['average_degree']}")
    print(f"  median / max degree    {stats['median_degree']} / {stats['max_degree']}")
    print(f"  components             {stats['components']}")
    print(f"  largest component      {stats['largest_component']} "
          f"({stats['largest_component_pct']}%)")
    print(f"  isolated nodes         {stats['isolated_nodes']}")

    print(f"\n{B}Labels{R}")
    for label, c in stats["label_counts"].items():
        print(f"  {label:<16} {c:>6}")

    print(f"\n{B}Relationship types{R}")
    for t, c in stats["relationship_counts"].items():
        print(f"  {t:<20} {c:>6}")

    print(f"\n{B}Degree distribution{R}")
    total = max(1, stats["node_count"])
    for bucket, c in stats["degree_distribution"].items():
        bar = "█" * max(1, round(40 * c / total))
        print(f"  {bucket:>6}  {c:>5}  {bar}")

    print(f"\n{B}Top hubs{R}")
    for h in stats["hubs"]:
        print(f"  {h['id']:<34} {h['degree']:>5}")

    p = stats["provenance"]
    print(f"\n{B}Provenance{R}")
    print(f"  corroborated nodes     {p['corroborated_nodes']}")
    print(f"  nodes with aliases     {p['nodes_with_aliases']}")
    print(f"  avg observations       {p['avg_observations']}")
    print(f"  avg confidence         {p['avg_confidence']}")

    print(f"\n{B}Alias lookup{R}  ({stats['alias_hit_rate']}% resolved)")
    for a in stats["alias_lookup"]:
        mark = "\033[32m✓\033[0m" if a["resolved"] else "\033[31m✗\033[0m"
        print(f"  {mark} {a['probe']:<18} → {str(a['canonical'] or '—'):<26} {D}{a['match']}{R}")

    print(f"\n{B}Multi-hop traversal{R}")
    for h in stats["multi_hop"]:
        legs = "  ".join(f"d{r['depth']}:+{r['new_nodes']}→{r['cumulative']}" for r in h["reach"])
        print(f"  {h['seed']:<16} {legs}")
    print()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Seed a realistic enterprise knowledge graph.")
    ap.add_argument("--dry-run", action="store_true", help="plan the write, touch nothing")
    ap.add_argument("--verify-only", action="store_true", help="report on the graph as it stands")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = ap.parse_args(argv)

    if args.dry_run:
        plan = seed(dry_run=True)
        print(json.dumps(plan, indent=2) if args.json else
              "\n".join(f"{k:<22} {v}" for k, v in plan.items()))
        return 0

    plan = None if args.verify_only else seed()
    stats = verify()

    if args.json:
        print(json.dumps({"seed": plan, "verify": stats}, indent=2, default=str))
    else:
        _print_report(plan, stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
