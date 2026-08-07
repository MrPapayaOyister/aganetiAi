#!/usr/bin/env python3
"""
Generate a realistic enterprise document corpus for RAG and retrieval evaluation.

    python scripts/generate_enterprise_documents.py            # write ~310 docs
    python scripts/generate_enterprise_documents.py --dry-run  # plan + stats only
    python scripts/generate_enterprise_documents.py --out DIR
    python scripts/generate_enterprise_documents.py --verify-only

Output lands in `data_vault/enterprise_corpus/` as Markdown with YAML frontmatter,
which is exactly what `python -m backend.ingest` already walks — so the corpus is
ingestible without touching the ingestion pipeline. **This script only writes
files. It does not ingest.** Embedding the corpus changes what the corporate RAG
provider returns, and that is a deliberate, separate decision.

The documents are grounded in the SAME entity catalogue that
`seed_enterprise_graph.py` writes to Neo4j — same people, projects, servers,
models, clients. That is the point rather than a convenience: a corpus of
plausible-but-unrelated prose cannot exercise hybrid fusion, because no answer
would ever be corroborated by both the graph and the vector store. Here "Which
GPU does WhisperX run on?" is answerable from Neo4j *and* from an infrastructure
document, so disagreement between them is a real, testable condition.

Determinism: all variation comes from `Random(SEED)` plus fixed date arithmetic.
No `uuid4()`, no `datetime.now()`. Re-running overwrites the same 310 filenames
with byte-identical content, so `backend.ingest` sees unchanged hashes and skips
them — re-generating does not silently re-embed the corpus.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from random import Random
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The entity catalogue is shared with the graph seeder on purpose — see module docstring.
from scripts.seed_enterprise_graph import (          # noqa: E402
    CATALOG,
    CLIENTS,
    CLIENT_CONTACTS,
    DEVELOPERS,
    HEADLINE_PROJECTS,
    LEAD,
    MANAGERS,
    MODELS,
    PROJECTS,
    SERVERS,
    SERVICES,
    STAFF,
    SUBPROJECTS,
    TECHNOLOGIES,
    VENDORS,
    VENDOR_CONTACTS,
)

SEED = 20260805
BASE_DAY = date(2026, 1, 5)
OUT_DIR = ROOT / "data_vault" / "enterprise_corpus"

MIN_WORDS, MAX_WORDS = 300, 1000
TARGET_LO, TARGET_HI = 380, 940      # aim inside the bounds so trimming is rare

DEPARTMENTS = ["AI Platform", "Infrastructure", "Applied Research", "Vision",
               "Product", "Delivery", "Integrations", "Quality", "Commercial"]

# Where a document came from, as a system of record. Distinct from `doc_type`:
# an incident report can live in Jira or Confluence, and retrieval filters that
# say "only things from the scanner" need the origin, not the genre.
SOURCE_SYSTEMS = ["Confluence", "SharePoint", "Jira", "Outlook", "GitLab Wiki",
                  "Google Drive", "Document Scanner", "Notion", "Tender Portal"]

# Mirrors the OWNS edges the graph seeder writes. Client-facing documents pick
# the project through this map rather than at random, so a circular from Ajman
# Police talks about the programme Ajman Police actually owns — otherwise the
# corpus and the graph disagree, and every cross-source eval case is unwinnable.
CLIENT_PROJECT = {
    "Ajman Police": "CCTV Analytics",
    "Dubai Parliament": "OCR Pipeline",
    "Ministry of Economy & Tourism": "Tender Automation",
    "Dar Al Ber Society": "Dar Al Ber Dashboard",
    "Sharjah Airport Authority": "Video Indexing",
    "Dubai Municipality": "Nazo",
}


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:70]


def words(text: str) -> int:
    return len(text.split())


class Ctx:
    """Per-document deterministic randomness plus entity pickers.

    A fresh `Random` seeded from the document index — not one shared stream —
    so inserting or removing a document type does not reshuffle every other
    document's content.
    """

    def __init__(self, index: int) -> None:
        self.rng = Random(SEED + index * 7919)
        self.index = index

    def pick(self, pool: list) -> Any:
        return self.rng.choice(sorted(pool) if isinstance(pool, (set, frozenset)) else pool)

    def sample(self, pool: list, n: int) -> list:
        pool = list(pool)
        return self.rng.sample(pool, min(n, len(pool)))

    def day(self, lo: int = 0, hi: int = 210) -> date:
        return BASE_DAY + timedelta(days=self.rng.randint(lo, hi))

    def maybe(self, p: float = 0.5) -> bool:
        return self.rng.random() < p

    def num(self, lo: int, hi: int) -> int:
        return self.rng.randint(lo, hi)

    def flt(self, lo: float, hi: float, nd: int = 2) -> float:
        return round(self.rng.uniform(lo, hi), nd)


@dataclass(slots=True)
class Doc:
    doc_id: str
    title: str
    author: str
    project: str
    date: str
    department: str
    tags: list[str]
    source: str
    doc_type: str
    body: str
    filename: str = ""

    def frontmatter(self) -> str:
        tags = ", ".join(f'"{t}"' for t in self.tags)
        return (
            "---\n"
            f'title: "{self.title}"\n'
            f'author: "{self.author}"\n'
            f'project: "{self.project}"\n'
            f"date: {self.date}\n"
            f'department: "{self.department}"\n'
            f"tags: [{tags}]\n"
            f'source: "{self.source}"\n'
            f'doc_type: "{self.doc_type}"\n'
            f'doc_id: "{self.doc_id}"\n'
            "---\n\n"
        )

    def render(self) -> str:
        return self.frontmatter() + self.body.rstrip() + "\n"


def pad_to_target(ctx: Ctx, paragraphs: list[str], filler: list[str],
                  target: int) -> list[str]:
    """Grow a document to `target` words with type-appropriate filler.

    Filler is drawn without replacement first so a short document does not repeat
    the same sentence twice; only a very long target falls back to reuse.
    """
    pool = list(filler)
    ctx.rng.shuffle(pool)
    i = 0
    while words("\n\n".join(paragraphs)) < target and pool:
        paragraphs.append(pool[i % len(pool)])
        i += 1
        if i > len(pool) * 3:
            break
    return paragraphs


def trim_to_max(text: str, limit: int = MAX_WORDS) -> str:
    """Hard cap, cutting at a paragraph boundary so nothing ends mid-sentence."""
    if words(text) <= limit:
        return text
    out: list[str] = []
    total = 0
    for para in text.split("\n\n"):
        n = words(para)
        if total + n > limit:
            break
        out.append(para)
        total += n
    return "\n\n".join(out)


# ══════════════════════════════════════════════════════════════════════════════
# Shared prose fragments
# ══════════════════════════════════════════════════════════════════════════════

RISK_LINES = [
    "The main residual risk is capacity: sustained load above the modelled peak "
    "would exhaust the current GPU allocation before autoscaling can react.",
    "Vendor lock-in on the inference layer remains an accepted risk. The gateway "
    "abstraction keeps the migration cost bounded but does not eliminate it.",
    "Data residency was reviewed and no personal data leaves the UAE region. "
    "Cross-border traffic is limited to model weights and telemetry.",
    "The rollback path has been exercised in staging but not in production. "
    "That gap is tracked and scheduled before the next major release.",
    "Key-person dependency on a single reviewer for schema changes is a known "
    "bottleneck; a second approver is being onboarded this quarter.",
]

NEXT_STEPS = [
    "Owners were assigned for each open item and will report at the next review.",
    "A follow-up session was scheduled to close the outstanding decisions.",
    "The team agreed to revisit the estimate once the benchmark numbers land.",
    "Documentation will be updated before the change is promoted to production.",
    "A short spike was approved to de-risk the approach before committing to it.",
]


def _para(ctx: Ctx, sentences: list[str], n: int = 3) -> str:
    return " ".join(ctx.sample(sentences, n))


# ══════════════════════════════════════════════════════════════════════════════
# Document type generators
# ══════════════════════════════════════════════════════════════════════════════

def gen_meeting_minutes(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    kind = ctx.pick(["Sprint Planning", "Steering Committee", "Architecture Review",
                     "Client Demo", "Incident Review", "Retrospective", "Backlog Grooming"])
    attendees = ctx.sample(STAFF, ctx.num(4, 7)) + ctx.sample(CLIENT_CONTACTS, ctx.num(0, 2))
    chair = attendees[0]
    techs = ctx.sample(TECHNOLOGIES, 3)
    when = ctx.day()

    body = [
        f"# {kind} — {project}",
        f"**Date:** {when.isoformat()}  \n**Chair:** {chair}  \n"
        f"**Attendees:** {', '.join(attendees)}",
        "## Agenda",
        "\n".join(f"{i}. {item}" for i, item in enumerate([
            f"Progress review on {project}",
            f"Open risks and blockers affecting {techs[0]}",
            f"Capacity and scheduling for the next iteration",
            "Any other business",
        ], 1)),
        "## Discussion",
        f"{chair} opened by summarising delivery status on {project}. The team "
        f"confirmed that the {techs[0]} integration is functionally complete and "
        f"has been running in staging for {ctx.num(4, 21)} days without a "
        f"regression. {ctx.pick(attendees)} raised that the {techs[1]} component "
        f"still shows intermittent latency spikes under concurrent load, measured "
        f"at roughly {ctx.num(180, 1400)} ms at the {ctx.num(90, 99)}th percentile.",
        f"A longer discussion followed on whether to bring {techs[2]} forward in the "
        f"plan. {ctx.pick(attendees)} argued that deferring it would compound the "
        f"migration cost, while {ctx.pick(attendees)} noted the team is already at "
        f"capacity for the current iteration. The compromise was to time-box a "
        f"{ctx.num(2, 5)}-day investigation and decide with data rather than "
        f"estimates.",
        f"On the client side, {ctx.pick(CLIENT_CONTACTS)} asked for clarity on the "
        f"reporting cadence. The team committed to a weekly written summary and a "
        f"fortnightly live demo, starting {(when + timedelta(days=7)).isoformat()}.",
        "## Decisions",
        "\n".join(f"- {d}" for d in [
            f"Proceed with the {techs[0]} rollout to production in the next release window.",
            f"Time-box the {techs[2]} investigation to {ctx.num(2, 5)} days.",
            f"Adopt a weekly written status report for {project}.",
        ]),
        "## Action items",
        "\n".join(
            f"- **{p}** — {a} (due {(when + timedelta(days=ctx.num(3, 21))).isoformat()})"
            for p, a in zip(ctx.sample(STAFF, 4), [
                f"produce load-test numbers for {techs[1]}",
                f"draft the migration note for {techs[2]}",
                f"update the runbook for {project}",
                "circulate the revised delivery schedule",
            ])),
        ctx.pick(NEXT_STEPS),
    ]
    title = f"{kind} Minutes — {project} ({when.isoformat()})"
    tags = ["meeting", slug(kind), slug(project), "minutes"]
    return title, "\n\n".join(body), tags, project


def gen_architecture(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    techs = ctx.sample(TECHNOLOGIES, 5)
    servers = ctx.sample(SERVERS, 2)
    svc = ctx.sample(SERVICES, 2)

    body = [
        f"# {project} — Architecture Overview",
        "## Purpose",
        f"This document describes the runtime architecture of {project}, the "
        f"boundaries between its components, and the reasoning behind the choices "
        f"that are expensive to reverse. It is the reference for anyone changing "
        f"the request path or adding a dependency.",
        "## Component topology",
        f"Requests enter through {svc[0]}, which handles authentication and rate "
        f"limiting before dispatching to {svc[1]}. Persistent state lives in "
        f"{techs[0]}; derived state that can be rebuilt from source lives in "
        f"{techs[1]} and is treated as a cache rather than a system of record. "
        f"{techs[2]} sits behind the service boundary and is never called directly "
        f"from the presentation layer.",
        f"The compute tier runs on {servers[0]} with {servers[1]} held as warm "
        f"standby. Failover is manual by design: an automatic cutover on a "
        f"stateful tier risks a split brain that is far more expensive than the "
        f"{ctx.num(3, 15)} minutes a human decision costs.",
        "## Data flow",
        f"1. The client submits a request to {svc[0]}.\n"
        f"2. {svc[0]} resolves identity and attaches the tenant context.\n"
        f"3. {svc[1]} assembles context from {techs[0]} and {techs[1]}.\n"
        f"4. The assembled context is ranked, truncated to the token budget, and "
        f"passed downstream.\n"
        f"5. The response is streamed back and the interaction is journalled to "
        f"{techs[3]}.",
        "## Key decisions",
        f"**Why {techs[0]} and not {techs[4]}.** The access pattern is "
        f"relationship-heavy rather than aggregate-heavy. {techs[4]} would serve "
        f"the current query volume comfortably, but every traversal would become a "
        f"multi-way join, and the join depth grows with the schema rather than "
        f"staying fixed.",
        f"**Single write path.** All writes go through one component. That is a "
        f"deliberate throughput ceiling: it buys the ability to reason about "
        f"ordering and to audit every mutation in one place, which is worth more "
        f"here than the extra headroom a second writer would provide.",
        "## Failure modes",
        f"If {techs[1]} is unavailable the system degrades rather than fails — "
        f"context assembly falls back to {techs[0]} alone, answers get less "
        f"specific, and a warning is logged. If {techs[0]} is unavailable the "
        f"request fails fast with a 503; serving from a stale cache would be worse "
        f"than an honest error for this workload.",
        "## Risks",
        ctx.pick(RISK_LINES),
    ]
    title = f"{project} Architecture Overview"
    tags = ["architecture", slug(project), "design", "reference"]
    return title, "\n\n".join(body), tags, project


def gen_deployment_guide(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    server = ctx.pick(SERVERS)
    techs = ctx.sample(TECHNOLOGIES, 3)
    env = ctx.pick(["production", "staging", "pre-production"])

    body = [
        f"# {project} — Deployment Guide ({env})",
        "## Prerequisites",
        f"- Access to `{slug(server)}` via the Tailscale network\n"
        f"- Deploy role on the {project} namespace\n"
        f"- {techs[0]} client version {ctx.num(2, 9)}.{ctx.num(0, 19)} or newer\n"
        f"- A current backup of {techs[1]}, verified by restore, not by existence",
        "## Procedure",
        f"```bash\n"
        f"# 1. drain traffic from the target node\n"
        f"kubectl cordon {slug(server)}\n"
        f"kubectl drain {slug(server)} --ignore-daemonsets --delete-emptydir-data\n\n"
        f"# 2. deploy\n"
        f"helm upgrade --install {slug(project)} ./charts/{slug(project)} \\\n"
        f"  --namespace {slug(project)} \\\n"
        f"  --set image.tag={ctx.num(1, 9)}.{ctx.num(0, 30)}.{ctx.num(0, 12)} \\\n"
        f"  --wait --timeout {ctx.num(5, 20)}m\n\n"
        f"# 3. verify before restoring traffic\n"
        f"kubectl rollout status deploy/{slug(project)} -n {slug(project)}\n"
        f"curl -fsS http://{slug(server)}:8000/health | jq .\n"
        f"```",
        "## Verification",
        f"The deployment is considered good when the health endpoint reports all "
        f"dependencies reachable, error rate stays below "
        f"{ctx.flt(0.1, 1.5)}% for {ctx.num(10, 45)} minutes, and p95 latency is "
        f"under {ctx.num(200, 1200)} ms. Do not restore full traffic on the health "
        f"check alone — it proves the process started, not that it works.",
        "## Rollback",
        f"```bash\n"
        f"helm rollback {slug(project)} 0 --namespace {slug(project)} --wait\n"
        f"kubectl uncordon {slug(server)}\n"
        f"```\n"
        f"Rollback is safe at any point before the schema migration step. After "
        f"that point the migration must be reversed first — {techs[2]} migrations "
        f"in this project are not automatically reversible, and running the "
        f"rollback without reversing them leaves the schema ahead of the code.",
        "## Post-deployment",
        f"Watch the dashboard for {ctx.num(30, 120)} minutes. Confirm queue depth "
        f"returns to baseline and no retry storm develops. Record the release in "
        f"the change log with the deploying engineer and the exact image tag.",
        ctx.pick(RISK_LINES),
    ]
    title = f"{project} Deployment Guide — {env}"
    tags = ["deployment", "runbook", slug(project), env]
    return title, "\n\n".join(body), tags, project


def gen_email(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    sender = ctx.pick(STAFF)
    recipients = ctx.sample(STAFF + CLIENT_CONTACTS, ctx.num(2, 4))
    tech = ctx.pick(TECHNOLOGIES)
    when = ctx.day()
    subject = ctx.pick([
        f"Re: {project} — status and next steps",
        f"{project}: deployment window confirmation",
        f"Re: {project} performance numbers",
        f"{project} — access request for {tech}",
        f"Re: {project} milestone sign-off",
    ])

    body = [
        f"**From:** {sender}  \n**To:** {', '.join(recipients)}  \n"
        f"**Date:** {when.isoformat()}  \n**Subject:** {subject}",
        f"Hi {recipients[0].split()[0]},",
        f"Following up on our conversation about {project}. The short version is "
        f"that we are on track for the {(when + timedelta(days=ctx.num(7, 40))).isoformat()} "
        f"milestone, with one caveat I want to flag early rather than at the review.",
        f"The {tech} work took longer than estimated — about {ctx.num(3, 12)} days "
        f"against a {ctx.num(2, 6)}-day estimate. The cause was not the integration "
        f"itself but the volume of edge cases in the existing data: roughly "
        f"{ctx.num(4, 22)}% of records needed manual reconciliation before they "
        f"would load cleanly. We have automated most of that now, so the cost is "
        f"one-off rather than recurring.",
        f"What this means for the schedule: the {ctx.pick(['integration', 'reporting', 'migration', 'rollout'])} "
        f"track absorbs the slip and the milestone date is unchanged. If anything "
        f"slips further I will tell you within a day rather than waiting for the "
        f"weekly.",
        f"Two things I need from your side:",
        f"1. Confirmation of the deployment window on "
        f"{(when + timedelta(days=ctx.num(5, 20))).isoformat()} — we need roughly "
        f"{ctx.num(30, 180)} minutes with reduced traffic.\n"
        f"2. A named contact for sign-off, so approval does not sit in a shared "
        f"inbox over a weekend.",
        f"Happy to walk through any of this on a call if that is easier. "
        f"{ctx.pick(NEXT_STEPS)}",
        f"Best regards,  \n{sender}  \n{CATALOG[sender].props.get('role', 'Engineer')}, Meerana",
    ]
    tags = ["email", "correspondence", slug(project)]
    return subject, "\n\n".join(body), tags, project


def gen_incident_report(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    tech = ctx.pick(TECHNOLOGIES)
    server = ctx.pick(SERVERS)
    when = ctx.day()
    sev = ctx.pick(["SEV1", "SEV2", "SEV2", "SEV3"])
    inc = f"INC-{ctx.num(1000, 9999)}"
    dur = ctx.num(12, 340)

    body = [
        f"# Incident Report {inc} — {sev}",
        f"**Service:** {project}  \n**Detected:** {when.isoformat()} "
        f"{ctx.num(0, 23):02d}:{ctx.num(0, 59):02d} UTC  \n"
        f"**Duration:** {dur} minutes  \n"
        f"**Incident commander:** {ctx.pick(STAFF)}",
        "## Summary",
        f"Between the times above, {project} returned elevated error rates to "
        f"approximately {ctx.num(5, 80)}% of requests. The trigger was "
        f"{ctx.pick(['a configuration change', 'an unannounced dependency upgrade', 'a traffic spike', 'a certificate expiry', 'exhausted connection pool'])} "
        f"affecting {tech} on {server}. Customer impact was "
        f"{ctx.pick(['degraded response quality', 'failed requests', 'increased latency', 'partial data unavailability'])}.",
        "## Timeline",
        "\n".join(f"- **T+{t} min** — {e}" for t, e in [
            (0, f"Automated alert fires on {tech} error rate crossing threshold."),
            (ctx.num(2, 9), "On-call acknowledges and opens the incident channel."),
            (ctx.num(10, 30), f"First hypothesis ({tech} saturation) investigated and ruled out."),
            (ctx.num(31, 70), "Root cause identified from correlated deploy timestamps."),
            (ctx.num(71, 140), "Mitigation applied; error rate begins to fall."),
            (dur, "Service confirmed healthy; incident closed."),
        ]),
        "## Root cause",
        f"The immediate cause was {tech} exhausting its "
        f"{ctx.pick(['connection pool', 'file descriptor limit', 'memory allocation', 'thread pool'])} "
        f"under a load pattern it had not previously seen. The underlying cause is "
        f"that the limit was set from a benchmark run against {ctx.num(2, 8)}x less "
        f"traffic than production now carries, and nothing re-evaluated it as load "
        f"grew. The alert that eventually fired was a symptom alarm, not a "
        f"saturation alarm, which is why detection lagged onset by "
        f"{ctx.num(4, 25)} minutes.",
        "## What went well",
        f"- The incident channel was opened within {ctx.num(2, 9)} minutes of the alert.\n"
        f"- The rollback procedure in the runbook worked as written.\n"
        f"- Degraded mode kept {ctx.num(20, 60)}% of traffic served throughout.",
        "## What did not",
        f"- Saturation metrics for {tech} were collected but not alerted on.\n"
        f"- The runbook pointed at a dashboard that had been renamed.\n"
        f"- Two engineers investigated the same hypothesis in parallel for "
        f"{ctx.num(10, 30)} minutes because ownership was not stated.",
        "## Corrective actions",
        "\n".join(f"- {a} — owner **{o}**, due {(when + timedelta(days=ctx.num(7, 45))).isoformat()}"
                  for a, o in zip([
                      f"Add a saturation alert on {tech} at 70% of the configured limit",
                      "Re-derive resource limits from current production traffic",
                      "Fix the dashboard link in the runbook and add a link test to CI",
                      "Add an explicit ownership statement to the incident template",
                  ], ctx.sample(STAFF, 4))),
    ]
    title = f"Incident Report {inc}: {project} {sev}"
    tags = ["incident", "postmortem", sev.lower(), slug(project)]
    return title, "\n\n".join(body), tags, project


def gen_project_doc(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    client = CATALOG[project].props.get("client") or ctx.pick(CLIENTS)
    techs = ctx.sample(TECHNOLOGIES, 4)
    team = ctx.sample(STAFF, ctx.num(4, 7))

    body = [
        f"# {project} — Project Documentation",
        "## Scope",
        f"{project} delivers {ctx.pick(['an automated analysis pipeline', 'a conversational assistant', 'a reporting and insight layer', 'a document processing workflow', 'a real-time monitoring capability'])} "
        f"for {client}. The engagement covers design, build, integration with "
        f"existing systems, and {ctx.num(3, 24)} months of support after handover. "
        f"Explicitly out of scope: hardware procurement, network changes on the "
        f"client side, and any migration of historical data older than "
        f"{ctx.num(2, 10)} years.",
        "## Team",
        "\n".join(f"- **{p}** — {CATALOG[p].props.get('role', 'Engineer')}" for p in team),
        "## Current status",
        f"The project is {CATALOG[project].props.get('status', 'active')}. "
        f"{ctx.num(3, 9)} of {ctx.num(9, 14)} planned milestones are complete. The "
        f"critical path currently runs through the {techs[0]} integration, which "
        f"is blocked on client-side access approval rather than on engineering "
        f"effort.",
        "## Technical approach",
        f"The system is built on {techs[0]} and {techs[1]}, with {techs[2]} "
        f"handling persistence and {techs[3]} used for the asynchronous workload. "
        f"The design favours boring, well-understood components over novel ones: "
        f"this is a system that must run unattended for years, and the operational "
        f"cost of an unfamiliar dependency outlasts whatever it saved at build time.",
        "## Acceptance criteria",
        "\n".join(f"{i}. {c}" for i, c in enumerate([
            f"End-to-end processing completes within {ctx.num(2, 30)} seconds at p95.",
            f"Accuracy on the agreed evaluation set is at or above {ctx.num(82, 97)}%.",
            f"The system sustains {ctx.num(50, 800)} concurrent sessions without degradation.",
            "All access is authenticated and audit-logged with a retention period agreed in writing.",
            "Handover documentation is complete and a client engineer has performed a supervised deployment.",
        ], 1)),
        "## Dependencies and assumptions",
        f"Delivery assumes the client provides network access to their "
        f"{ctx.pick(['records system', 'camera network', 'identity provider', 'data warehouse'])} "
        f"by {ctx.day(60, 150).isoformat()}. A delay there moves the end date "
        f"one-for-one; it is on the critical path and there is no parallel work "
        f"that absorbs it.",
        ctx.pick(RISK_LINES),
    ]
    title = f"{project} — Project Documentation"
    tags = ["project", "documentation", slug(project), slug(str(client))]
    return title, "\n\n".join(body), tags, project


def gen_api_reference(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    svc = ctx.pick(SERVICES)
    resource = ctx.pick(["sessions", "documents", "entities", "reports", "jobs",
                         "detections", "transcripts", "insights"])

    body = [
        f"# {svc} API Reference — `/{resource}`",
        f"Base URL: `https://api.meerana.ae/v1`  \nAuthentication: bearer token "
        f"issued by Microsoft Entra ID. All endpoints require the "
        f"`{resource}.read` scope; write operations additionally require "
        f"`{resource}.write`.",
        f"## `GET /{resource}`",
        f"Returns a paginated list of {resource} visible to the caller. Results are "
        f"scoped to the caller's tenant — there is no cross-tenant read, and no "
        f"parameter enables one.",
        "**Query parameters**\n\n"
        "| Name | Type | Default | Description |\n"
        "|---|---|---|---|\n"
        f"| `limit` | integer | 25 | Page size, maximum {ctx.num(50, 500)}. |\n"
        "| `cursor` | string | — | Opaque pagination cursor from the previous response. |\n"
        f"| `project` | string | — | Filter to one project, e.g. `{slug(project)}`. |\n"
        "| `since` | RFC3339 | — | Only items updated at or after this instant. |",
        f"```bash\ncurl -H \"Authorization: Bearer $TOKEN\" \\\n"
        f"  'https://api.meerana.ae/v1/{resource}?project={slug(project)}&limit=50'\n```",
        "**Response `200`**\n\n"
        "```json\n{\n"
        f'  "items": [\n'
        f'    {{ "id": "{slug(resource)}-{ctx.num(1000, 9999)}", '
        f'"project": "{slug(project)}", "status": "ready", '
        f'"updated_at": "{ctx.day().isoformat()}T09:14:22Z" }}\n'
        "  ],\n"
        '  "next_cursor": "eyJvZmZzZXQiOjUwfQ==",\n'
        f'  "total": {ctx.num(50, 20000)}\n'
        "}\n```",
        f"## `POST /{resource}`",
        f"Creates a {resource[:-1]}. The request is idempotent when an "
        f"`Idempotency-Key` header is supplied: replaying the same key within "
        f"{ctx.num(1, 48)} hours returns the original response rather than "
        f"creating a duplicate.",
        "## Errors\n\n"
        "| Status | Code | Meaning |\n"
        "|---|---|---|\n"
        "| 400 | `invalid_request` | A parameter failed validation. The body names the field. |\n"
        "| 401 | `unauthenticated` | Token missing, malformed, or expired. |\n"
        "| 403 | `forbidden` | Valid token without the required scope. |\n"
        "| 404 | `not_found` | No such resource, or no read access to it. The two are deliberately indistinguishable. |\n"
        f"| 429 | `rate_limited` | Over {ctx.num(60, 1200)} requests/minute. Retry after the `Retry-After` header. |\n"
        "| 503 | `dependency_unavailable` | A downstream store is unreachable. Safe to retry with backoff. |",
        f"## Rate limits\n\nThe default quota is {ctx.num(60, 1200)} requests per "
        f"minute per token, burstable to {ctx.num(1200, 4000)} for "
        f"{ctx.num(5, 60)} seconds. Quotas are per token rather than per user, so "
        f"a shared service token is a shared limit.",
    ]
    title = f"{svc} API Reference — {resource}"
    tags = ["api", "reference", slug(resource), slug(project)]
    return title, "\n\n".join(body), tags, project


def gen_research_note(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    model = ctx.pick(MODELS)
    topic = ctx.pick([
        "chunk size and retrieval precision", "reranking cost versus benefit",
        "quantisation impact on answer quality", "graph depth and answer grounding",
        "embedding model selection", "prompt compression under a token budget",
        "hybrid fusion weighting", "negative sampling for entity resolution",
    ])

    body = [
        f"# Research Note — {topic.title()}",
        f"**Author:** {ctx.pick(STAFF)}  \n**Project:** {project}  \n"
        f"**Status:** {ctx.pick(['exploratory', 'complete', 'superseded', 'in review'])}",
        "## Question",
        f"Does varying {topic} produce a measurable change in end-to-end answer "
        f"quality for {project}, and if so, is the effect large enough to justify "
        f"the added complexity?",
        "## Method",
        f"We ran {ctx.num(3, 9)} configurations against a held-out set of "
        f"{ctx.num(80, 600)} questions, holding {model} and the retrieval corpus "
        f"fixed. Each configuration was evaluated {ctx.num(3, 5)} times to "
        f"separate genuine effects from sampling noise. Scoring used exact-match "
        f"on expected entities plus a graded relevance judgement.",
        "## Results",
        "| Configuration | Precision | Recall | nDCG | p95 latency |\n"
        "|---|---|---|---|---|\n" +
        "\n".join(
            f"| {c} | {ctx.flt(0.41, 0.94)} | {ctx.flt(0.38, 0.92)} | "
            f"{ctx.flt(0.44, 0.96)} | {ctx.num(120, 2400)} ms |"
            for c in ["baseline", "variant A", "variant B", "variant C"]),
        "## Interpretation",
        f"The effect is real but smaller than expected: roughly "
        f"{ctx.flt(1.5, 9.0, 1)} points of nDCG, against a latency cost of "
        f"{ctx.num(40, 900)} ms. That trade is worth taking for batch workloads and "
        f"is not worth taking on the interactive path, where the latency budget is "
        f"already the binding constraint.",
        f"An unexpected finding: most of the gain came from the "
        f"{ctx.num(10, 30)}% of questions that mention more than one entity. On "
        f"single-entity questions the configurations are statistically "
        f"indistinguishable. That suggests the mechanism is disambiguation rather "
        f"than ranking, which points at a cheaper intervention.",
        "## Threats to validity",
        f"The evaluation set over-represents {project} content relative to "
        f"production traffic, so absolute numbers should not be quoted externally. "
        f"The held-out set was constructed by the same team that tuned the "
        f"baseline, and no attempt was made to blind that. Results below "
        f"{ctx.flt(1.0, 3.0, 1)} points should be treated as noise.",
        "## Recommendation",
        f"Adopt the variant on the batch path only. Re-run this study once the "
        f"corpus exceeds {ctx.num(5, 60)}k documents — the mechanism identified "
        f"here should strengthen with corpus size, and if it does not, the "
        f"explanation above is wrong.",
    ]
    title = f"Research Note: {topic.title()} ({project})"
    tags = ["research", "evaluation", slug(topic), slug(project)]
    return title, "\n\n".join(body), tags, project


def gen_ocr_document(ctx: Ctx) -> tuple[str, str, list[str], str]:
    """A scanned document as OCR actually returns it — with the artefacts.

    Deliberately degraded: broken words, confused glyphs, lost spacing, stray
    marks. A corpus of clean text would make retrieval look better than it is,
    because the real scanned intake in this system is far from clean.
    """
    client = ctx.pick(CLIENTS)
    project = CLIENT_PROJECT[client]
    ref = f"{ctx.num(100, 999)}/{ctx.num(2023, 2026)}"

    def degrade(text: str) -> str:
        subs = {"l": "1", "O": "0", "S": "5", "rn": "m", "I": "l"}
        out = []
        for word in text.split(" "):
            r = ctx.rng.random()
            if r < 0.06:
                k = ctx.pick(sorted(subs))
                word = word.replace(k, subs[k], 1)
            elif r < 0.09 and len(word) > 5:
                word = word[:len(word) // 2] + " " + word[len(word) // 2:]
            elif r < 0.11:
                word = word + ctx.pick([".", ",", "'", "`", "|"])
            out.append(word)
        return " ".join(out)

    # The issuing authority has to match the client, or the document contradicts
    # itself on its own letterhead — an incoherence no OCR artefact explains.
    emirate = {"Ajman Police": "AJMAN", "Dubai Parliament": "DUBAI",
               "Dar Al Ber Society": "DUBAI", "Dubai Municipality": "DUBAI",
               "Sharjah Airport Authority": "SHARJAH",
               "Ministry of Economy & Tourism": "THE UNITED ARAB EMIRATES"}[client]

    clean = [
        f"GOVERNMENT OF {emirate}",
        f"{client}",
        f"CIRCULAR No. {ref}",
        f"Date: {ctx.day().isoformat()}",
        f"Subject: {ctx.pick(['Implementation of Digital Records Policy', 'Procurement of Analytics Services', 'Data Sharing Protocol', 'Annual Systems Audit', 'Approval of Technical Specification'])}",
        f"Pursuant to the resolution of the technical committee dated "
        f"{ctx.day().isoformat()}, all departments are hereby instructed to comply "
        f"with the requirements set out below in respect of the {project} "
        f"programme.",
        f"1. All records generated under the programme shall be retained for a "
        f"period of not less than {ctx.num(3, 15)} years from the date of creation, "
        f"in a format permitting retrieval without proprietary software.",
        f"2. Access to the system shall be restricted to personnel holding a valid "
        f"authorisation issued by the competent authority. A register of such "
        f"authorisations shall be maintained and reviewed quarterly.",
        f"3. Any transfer of data outside the territory of the State requires prior "
        f"written approval. Requests shall specify the categories of data, the "
        f"recipient, the purpose, and the safeguards applied.",
        f"4. The supplier shall submit a compliance report every "
        f"{ctx.num(1, 6)} months, in the form annexed hereto, signed by an "
        f"authorised representative.",
        f"5. Non-compliance shall be reported to the committee within "
        f"{ctx.num(3, 30)} days of becoming known.",
        f"This circular takes effect from the date of issue and supersedes circular "
        f"No. {ctx.num(100, 999)}/{ctx.num(2020, 2025)}.",
        f"Signed,",
        f"{ctx.pick(CLIENT_CONTACTS)}",
        f"{client}",
        f"[SEAL]",
    ]
    body = [f"<!-- OCR extract, confidence {ctx.flt(0.61, 0.93)}, "
            f"{ctx.num(1, 6)} page(s), scanner intake -->"]
    body += [degrade(line) for line in clean]
    title = f"OCR: Circular {ref} — {client}"
    tags = ["ocr", "scanned", "circular", slug(client), "government"]
    return title, "\n\n".join(body), tags, project


def gen_model_comparison(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    cands = ctx.sample(MODELS, 4)
    task = ctx.pick(["summarisation", "entity extraction", "OCR post-correction",
                     "visual question answering", "translation", "classification",
                     "structured output generation"])

    body = [
        f"# Model Comparison — {task.title()} for {project}",
        "## Objective",
        f"Select a model for {task} in {project}. The decision criteria, in "
        f"priority order, are accuracy on the internal evaluation set, p95 "
        f"latency under production concurrency, and VRAM footprint — in that "
        f"order, because the accuracy floor is contractual while latency is "
        f"merely uncomfortable.",
        "## Candidates",
        "\n".join(
            f"- **{m}** — {CATALOG[m].props.get('parameters', 'n/a')} parameters, "
            f"{CATALOG[m].props.get('modality', 'text')}, "
            f"vendor {CATALOG[m].props.get('vendor', 'unknown')}" for m in cands),
        "## Results",
        "| Model | Accuracy | p50 latency | p95 latency | VRAM | Cost / 1k req |\n"
        "|---|---|---|---|---|---|\n" +
        "\n".join(
            f"| {m} | {ctx.flt(0.62, 0.96)} | {ctx.num(80, 900)} ms | "
            f"{ctx.num(200, 3200)} ms | {ctx.num(6, 80)} GB | "
            f"${ctx.flt(0.04, 4.5)} |" for m in cands),
        "## Analysis",
        f"{cands[0]} leads on raw accuracy by {ctx.flt(1.2, 8.0, 1)} points, but "
        f"its p95 latency is {ctx.flt(1.4, 3.8, 1)}x the next candidate and its "
        f"VRAM footprint forces a dedicated node. {cands[1]} is within "
        f"{ctx.flt(0.5, 3.0, 1)} points on accuracy at roughly half the serving "
        f"cost, and it co-locates with the existing workload.",
        f"{cands[2]} was eliminated on quality: it degrades sharply on inputs "
        f"longer than {ctx.num(1000, 8000)} tokens, which is {ctx.num(12, 45)}% of "
        f"production traffic for this task. {cands[3]} performs well but its "
        f"licence terms do not permit the deployment model this project requires.",
        "## Failure analysis",
        f"Errors cluster into three groups. Roughly {ctx.num(30, 60)}% are "
        f"hallucinated specifics — plausible names or figures with no support in "
        f"the input. About {ctx.num(15, 35)}% are truncation artefacts where the "
        f"answer is correct but incomplete. The remainder are genuine "
        f"misunderstandings of the question. Only the first group is dangerous, "
        f"and it is the group that grounding in retrieved context most reduces.",
        "## Recommendation",
        f"Adopt **{cands[1]}** as the default for {task}, with {cands[0]} "
        f"available as an escalation path for inputs flagged as high-stakes. "
        f"Revisit when the next major release of either lands, or if the accuracy "
        f"floor moves above {ctx.flt(0.88, 0.97)}.",
    ]
    title = f"Model Comparison: {task.title()} — {project}"
    tags = ["model-comparison", "benchmark", slug(task), slug(project)]
    return title, "\n\n".join(body), tags, project


def gen_tender(ctx: Ctx) -> tuple[str, str, list[str], str]:
    client = ctx.pick(CLIENTS)
    project = CLIENT_PROJECT[client]
    ref = f"TND-{ctx.num(2024, 2026)}-{ctx.num(100, 999)}"
    value = ctx.num(400, 9500) * 1000

    body = [
        f"# Tender Response {ref} — {client}",
        f"**Submitted by:** Meerana Technologies  \n"
        f"**Tender reference:** {ref}  \n"
        f"**Closing date:** {ctx.day(30, 180).isoformat()}  \n"
        f"**Estimated value:** AED {value:,}  \n"
        f"**Validity:** {ctx.num(60, 180)} days from submission",
        "## 1. Executive summary",
        f"Meerana Technologies submits this response for the supply, "
        f"implementation and support of {project} for {client}. Our proposal "
        f"covers the full scope set out in the tender documents, including "
        f"integration with existing systems, knowledge transfer, and "
        f"{ctx.num(12, 60)} months of post-implementation support.",
        "## 2. Compliance matrix",
        "| Req. | Requirement | Compliance | Reference |\n"
        "|---|---|---|---|\n" +
        "\n".join(
            f"| {i}.0 | {r} | {ctx.pick(['Fully compliant', 'Fully compliant', 'Fully compliant', 'Compliant with clarification'])} | §{ctx.num(3, 9)}.{ctx.num(1, 8)} |"
            for i, r in enumerate([
                "Data residency within the State",
                "Role-based access control with audit logging",
                "Availability of 99.5% measured monthly",
                "Handover of source code and documentation",
                "Local support presence during business hours",
                "Compliance with national information assurance standards",
            ], 1)),
        "## 3. Technical approach",
        f"The solution is delivered in {ctx.num(3, 5)} phases over "
        f"{ctx.num(6, 24)} months. Phase 1 establishes the environment and "
        f"integrations. Phase 2 delivers the core capability against an agreed "
        f"acceptance test. Phase 3 covers rollout, training and handover. Each "
        f"phase has a defined exit criterion and a payment milestone tied to it.",
        "## 4. Commercial",
        "| Item | Qty | Unit (AED) | Total (AED) |\n"
        "|---|---|---|---|\n" +
        "\n".join(
            f"| {item} | {q} | {u:,} | {q * u:,} |" for item, q, u in [
                ("Implementation services", ctx.num(80, 400), ctx.num(700, 1600)),
                ("Infrastructure and licences", ctx.num(1, 6), ctx.num(40000, 320000)),
                ("Training and knowledge transfer", ctx.num(3, 20), ctx.num(2500, 9000)),
                ("Annual support", ctx.num(1, 5), ctx.num(60000, 400000)),
            ]),
        "## 5. Assumptions and exclusions",
        f"Pricing assumes {client} provides network connectivity, physical "
        f"security, and access to source systems within {ctx.num(10, 45)} days of "
        f"contract award. Customs duties, third-party licence increases, and any "
        f"scope added after signature are excluded. Delays attributable to the "
        f"client extend the schedule on a day-for-day basis.",
        "## 6. References",
        f"Comparable deliveries: {', '.join(str(c) for c in ctx.sample(CLIENTS, 3))}. "
        f"Contactable references available on request.",
    ]
    title = f"Tender Response {ref} — {client}"
    tags = ["tender", "commercial", "government", slug(client), "procurement"]
    return title, "\n\n".join(body), tags, project


def gen_infrastructure(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    hosts = ctx.sample(SERVERS, 4)

    body = [
        f"# Infrastructure Reference — {project}",
        "## Inventory",
        "| Host | Role | Hardware | Memory | Network |\n"
        "|---|---|---|---|---|\n" +
        "\n".join(
            f"| `{slug(h)}` | {CATALOG[h].props.get('kind', 'vm')} | "
            f"{CATALOG[h].props.get('hardware', 'n/a')} | "
            f"{CATALOG[h].props.get('memory', 'n/a')} | "
            f"10.{ctx.num(1, 40)}.{ctx.num(0, 255)}.0/24 |" for h in hosts),
        "## Network",
        f"All inter-host traffic runs over Tailscale on the `meerana-prod` tailnet. "
        f"There is no public ingress to the compute tier; the only externally "
        f"reachable surface is the load balancer, which terminates TLS and "
        f"forwards to `{slug(hosts[0])}`. Node-to-node ACLs are enforced at the "
        f"tailnet level rather than by host firewall, so a misconfigured host "
        f"cannot accidentally widen access.",
        "## Storage",
        f"Block storage is provisioned at {ctx.num(200, 4000)} GB per compute node "
        f"with {ctx.num(3000, 20000)} provisioned IOPS. Object storage is a "
        f"MinIO cluster on `{slug(hosts[-1])}` with {ctx.num(2, 4)}-way "
        f"replication. Snapshots run every {ctx.num(4, 24)} hours and are retained "
        f"for {ctx.num(7, 90)} days.",
        "## Capacity",
        f"Current utilisation is approximately {ctx.num(35, 88)}% of CPU and "
        f"{ctx.num(40, 92)}% of GPU memory at peak. On the present growth rate the "
        f"GPU tier reaches its limit in roughly {ctx.num(2, 14)} months, which is "
        f"the binding constraint on the next intake of workloads — not CPU, and "
        f"not storage.",
        "## Backup and recovery",
        f"RPO is {ctx.num(1, 24)} hours and RTO is {ctx.num(1, 8)} hours. Restores "
        f"are tested quarterly against a scratch environment; the last verified "
        f"restore was {ctx.day(0, 90).isoformat()} and completed in "
        f"{ctx.num(35, 240)} minutes. A backup that has not been restored is not a "
        f"backup, so the test result — not the job status — is what is reported.",
        "## Monitoring",
        f"Prometheus scrapes every {ctx.num(10, 60)} seconds; Grafana dashboards "
        f"cover saturation, error rate, latency and queue depth. Alert routing "
        f"sends SEV1 to the on-call phone and everything else to the team channel.",
        ctx.pick(RISK_LINES),
    ]
    title = f"Infrastructure Reference — {project}"
    tags = ["infrastructure", "operations", slug(project), "capacity"]
    return title, "\n\n".join(body), tags, project


def gen_docker_compose(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    port = ctx.num(8000, 8999)

    body = [
        f"# {project} — Local Development Stack",
        f"This compose file brings up the full {project} dependency set for local "
        f"development. It is not a production topology: everything runs on one "
        f"host, secrets are literals, and no resource limits are set.",
        "## `docker-compose.yml`",
        "```yaml\n"
        "services:\n"
        "  api:\n"
        "    build: .\n"
        f"    ports: [\"{port}:8000\"]\n"
        "    environment:\n"
        "      DATABASE_URL: postgresql+asyncpg://app:app@postgres:5432/app\n"
        "      REDIS_URL: redis://redis:6379/0\n"
        "      QDRANT_URL: http://qdrant:6333\n"
        "      NEO4J_URI: bolt://neo4j:7687\n"
        "      LITELLM_BASE_URL: http://litellm:4000\n"
        "    depends_on:\n"
        "      postgres: { condition: service_healthy }\n"
        "      redis: { condition: service_started }\n"
        "    volumes: [\"./:/app\"]\n\n"
        "  postgres:\n"
        "    image: postgres:16-alpine\n"
        "    environment:\n"
        "      POSTGRES_USER: app\n"
        "      POSTGRES_PASSWORD: app\n"
        "      POSTGRES_DB: app\n"
        "    healthcheck:\n"
        "      test: [\"CMD-SHELL\", \"pg_isready -U app\"]\n"
        f"      interval: {ctx.num(5, 15)}s\n"
        "      retries: 5\n"
        "    volumes: [\"pgdata:/var/lib/postgresql/data\"]\n\n"
        "  redis:\n"
        "    image: redis:7-alpine\n"
        "    command: [\"redis-server\", \"--appendonly\", \"yes\"]\n\n"
        "  qdrant:\n"
        "    image: qdrant/qdrant:latest\n"
        "    ports: [\"6333:6333\"]\n"
        "    volumes: [\"qdrant_data:/qdrant/storage\"]\n\n"
        "  neo4j:\n"
        "    image: neo4j:5.26\n"
        "    environment:\n"
        "      NEO4J_AUTH: neo4j/localpassword\n"
        "    ports: [\"7474:7474\", \"7687:7687\"]\n"
        "    volumes: [\"neo4j_data:/data\"]\n\n"
        "volumes:\n"
        "  pgdata:\n"
        "  qdrant_data:\n"
        "  neo4j_data:\n"
        "```",
        "## Usage",
        f"```bash\ndocker compose up -d\ndocker compose logs -f api\n"
        f"curl http://localhost:{port}/health\n```",
        "## Notes",
        f"`depends_on` with `service_healthy` only waits for Postgres. Redis and "
        f"Qdrant start fast enough that the application's own retry loop covers "
        f"them; adding health gates for every service roughly doubles cold-start "
        f"time for no reliability gain locally.",
        f"The bind mount on `api` means code changes reload without a rebuild. It "
        f"also means the container sees your local `.env` — do not point a local "
        f"stack at production credentials, because the compose file will happily "
        f"let you.",
        f"Volumes persist between runs. `docker compose down -v` removes them and "
        f"gives a clean database; without `-v` a schema change from an older "
        f"branch can linger and produce confusing migration errors.",
    ]
    title = f"{project} — Docker Compose Development Stack"
    tags = ["docker", "compose", "development", slug(project), "infrastructure"]
    return title, "\n\n".join(body), tags, project


def gen_fastapi_doc(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    resource = ctx.pick(["sessions", "documents", "insights", "jobs", "entities"])

    body = [
        f"# FastAPI Service Guide — {project}",
        "## Application layout",
        f"```\napp/\n  main.py           # app factory, middleware, lifespan\n"
        f"  api/\n    {resource}.py     # routers\n    deps.py         # shared dependencies\n"
        f"  models/           # SQLAlchemy models\n  schemas/          # Pydantic v2 schemas\n"
        f"  services/         # business logic, no framework imports\n```",
        "## Routers and dependencies",
        "```python\n"
        "from fastapi import APIRouter, Depends, HTTPException, Query\n"
        "from sqlalchemy.ext.asyncio import AsyncSession\n\n"
        f"router = APIRouter(prefix=\"/{resource}\", tags=[\"{resource}\"])\n\n"
        f"@router.get(\"\", response_model=list[{resource.capitalize()}Out])\n"
        "async def list_items(\n"
        f"    limit: int = Query({ctx.num(20, 100)}, le={ctx.num(100, 500)}),\n"
        "    session: AsyncSession = Depends(get_session),\n"
        "    user: User = Depends(current_user),\n"
        ") -> list[Item]:\n"
        "    return await service.list_for_user(session, user.id, limit=limit)\n"
        "```",
        f"Business logic lives in `services/` and takes no FastAPI types. That "
        f"boundary is what makes the logic testable without a client and "
        f"replaceable without a rewrite — a router that queries the database "
        f"directly is a router you cannot reuse from a worker.",
        "## Lifespan and shared clients",
        "```python\n"
        "from contextlib import asynccontextmanager\n\n"
        "@asynccontextmanager\n"
        "async def lifespan(app: FastAPI):\n"
        "    app.state.http = httpx.AsyncClient(timeout=30.0)\n"
        "    app.state.qdrant = QdrantClient(url=settings.QDRANT_URL)\n"
        "    yield\n"
        "    await app.state.http.aclose()\n"
        "```",
        f"Clients are created once in the lifespan, not per request. A new "
        f"`AsyncClient` per request discards the connection pool and adds a full "
        f"TLS handshake to every call — measured here at roughly "
        f"{ctx.num(20, 180)} ms of avoidable latency.",
        "## Error handling",
        f"Domain errors raise typed exceptions that an exception handler maps to "
        f"responses. Routers do not build error payloads by hand: doing so was how "
        f"the API ended up with {ctx.num(3, 7)} different shapes for the same "
        f"condition before this was consolidated.",
        "## Testing",
        "```python\n"
        "@pytest.mark.asyncio\n"
        "async def test_list_requires_auth(client):\n"
        f"    r = await client.get(\"/{resource}\")\n"
        "    assert r.status_code == 401\n"
        "```",
        f"Tests run against a real Postgres in a container rather than SQLite. The "
        f"dialects differ in exactly the places this application relies on, so a "
        f"SQLite-backed test suite passes and production does not.",
    ]
    title = f"FastAPI Service Guide — {project}"
    tags = ["fastapi", "python", "backend", "engineering", slug(project)]
    return title, "\n\n".join(body), tags, project


def gen_django_doc(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(["Dubai Parliament", "OCR Pipeline", "Tender Automation"])
    app = ctx.pick(["records", "intake", "review", "archive", "workflow"])

    body = [
        f"# Django Application Guide — {project}",
        f"## The `{app}` app",
        "```python\n"
        "from django.db import models\n\n"
        f"class {app.capitalize()}Item(models.Model):\n"
        "    class Status(models.TextChoices):\n"
        "        RECEIVED = \"received\", \"Received\"\n"
        "        PROCESSING = \"processing\", \"Processing\"\n"
        "        REVIEWED = \"reviewed\", \"Reviewed\"\n\n"
        "    reference = models.CharField(max_length=64, unique=True, db_index=True)\n"
        "    status = models.CharField(max_length=16, choices=Status.choices,\n"
        "                              default=Status.RECEIVED, db_index=True)\n"
        "    received_at = models.DateTimeField(auto_now_add=True)\n"
        "    payload = models.JSONField(default=dict)\n\n"
        "    class Meta:\n"
        "        indexes = [models.Index(fields=[\"status\", \"received_at\"])]\n"
        "        ordering = [\"-received_at\"]\n"
        "```",
        "## Migrations",
        f"```bash\npython manage.py makemigrations {app}\n"
        f"python manage.py migrate --plan\npython manage.py migrate\n```",
        f"Always inspect `--plan` before applying. This project has "
        f"{ctx.num(40, 220)} migrations and a data migration that takes roughly "
        f"{ctx.num(4, 40)} minutes on production volumes — running it unnoticed "
        f"inside a deploy window is how the last extended outage started.",
        "## Querysets",
        "```python\n"
        f"qs = ({app.capitalize()}Item.objects\n"
        "      .filter(status=Status.RECEIVED)\n"
        "      .select_related(\"submitted_by\")\n"
        "      .prefetch_related(\"attachments\")\n"
        f"      .only(\"reference\", \"status\", \"received_at\")[:{ctx.num(50, 500)}])\n"
        "```",
        f"`select_related` and `prefetch_related` are not optional here. The "
        f"review list renders {ctx.num(20, 200)} rows with two related objects "
        f"each; without them the page issues {ctx.num(40, 400)} queries and takes "
        f"over {ctx.flt(1.2, 9.0, 1)} seconds.",
        "## Admin",
        f"The Django admin is the operational interface for {project}. It is "
        f"restricted to staff accounts behind SSO, with object-level permissions "
        f"on anything that mutates state. Read access is broad; write access is "
        f"narrow and logged.",
        "## Celery tasks",
        "```python\n"
        "@shared_task(bind=True, max_retries=5, autoretry_for=(RequestException,),\n"
        "             retry_backoff=True, retry_jitter=True)\n"
        f"def process_{app}_item(self, item_id: int) -> None:\n"
        "    item = Item.objects.select_for_update().get(pk=item_id)\n"
        "    ...\n"
        "```",
        f"Retries use backoff with jitter. Without jitter, a downstream outage "
        f"produces a synchronised retry storm that keeps the dependency down long "
        f"after it would otherwise have recovered.",
        "## Settings",
        f"Settings are split into `base`, `dev` and `prod`, selected by "
        f"`DJANGO_SETTINGS_MODULE`. Secrets come from the environment. `DEBUG` is "
        f"hard-coded to `False` in `prod.py` rather than read from the environment, "
        f"because that is a variable nobody should be able to set by accident.",
    ]
    title = f"Django Application Guide — {project} ({app})"
    tags = ["django", "python", "backend", "engineering", slug(project)]
    return title, "\n\n".join(body), tags, project


def _throughput_rows(ctx: Ctx, gpus: list[str]) -> list[str]:
    """Benchmark rows that obey the physics they claim to measure.

    Independently sampled cells produced tables where p95 fell below p50 and
    throughput dropped as concurrency rose — self-evidently fabricated, and worse,
    directly contradicting the prose beneath them. Each device therefore gets a
    base rate that is scaled along a saturating curve: throughput climbs with
    concurrency and flattens, latency climbs monotonically, and p95 is always
    derived from p50 rather than drawn beside it.
    """
    rows: list[str] = []
    for g in gpus:
        base_tps = ctx.num(400, 1400)          # single-stream tokens/s for this device
        base_p50 = ctx.num(40, 160)
        vram_floor = ctx.num(8, 40)
        knee = ctx.num(8, 40)                  # concurrency where the KV cache binds
        for c in (1, ctx.num(4, 12), ctx.num(24, 96)):
            # Saturating scale-up: near-linear below the knee, flat above it.
            scale = c / (1 + (c - 1) / knee)
            tps = int(base_tps * scale)
            p50 = int(base_p50 * (1 + (c - 1) / max(1, knee) * ctx.flt(0.8, 2.2)))
            p95 = int(p50 * ctx.flt(1.6, 3.4))
            vram = min(94, int(vram_floor + c * ctx.flt(0.3, 1.1)))
            rows.append(f"| {g} | {c} | {tps} | {p50} | {p95} | {vram} GB |")
    return rows


def gen_gpu_benchmark(ctx: Ctx) -> tuple[str, str, list[str], str]:
    project = ctx.pick(PROJECTS)
    gpus = ctx.sample([s for s in SERVERS if CATALOG[s].props.get("kind") in ("gpu", "npu", "edge")], 3)
    model = ctx.pick(MODELS)
    precision = ctx.pick(["FP16", "BF16", "INT8", "FP8", "INT4 (AWQ)"])

    body = [
        f"# GPU Benchmark Report — {model} on {gpus[0]}",
        f"**Project:** {project}  \n**Precision:** {precision}  \n"
        f"**Date:** {ctx.day().isoformat()}  \n**Engineer:** {ctx.pick(STAFF)}",
        "## Method",
        f"Each configuration was warmed with {ctx.num(10, 60)} requests and then "
        f"measured over {ctx.num(200, 3000)} requests at fixed concurrency. Input "
        f"length was held at {ctx.num(256, 4096)} tokens and output at "
        f"{ctx.num(64, 1024)} tokens. Numbers are the median of "
        f"{ctx.num(3, 5)} runs; the spread between runs never exceeded "
        f"{ctx.flt(1.0, 6.0, 1)}%.",
        "## Throughput",
        "| Device | Concurrency | Tokens/s | p50 (ms) | p95 (ms) | VRAM peak |\n"
        "|---|---|---|---|---|---|\n" +
        "\n".join(_throughput_rows(ctx, gpus)),
        "## Observations",
        f"Throughput scales close to linearly up to concurrency "
        f"{ctx.num(8, 40)}, after which the KV cache becomes the constraint and "
        f"additional concurrency buys queueing rather than work. On {gpus[0]} the "
        f"knee is sharp; on {gpus[1]} it is gradual, which makes the latter easier "
        f"to operate near its limit without a cliff.",
        f"Moving from FP16 to {precision} reduced VRAM by roughly "
        f"{ctx.num(18, 62)}% and increased throughput by {ctx.num(8, 90)}%, at a "
        f"quality cost of {ctx.flt(0.2, 3.4, 1)} points on the internal evaluation "
        f"set. For this workload that trade is acceptable; for the OCR "
        f"post-correction path it was not, and that path stays at higher precision.",
        "## Power and thermals",
        f"Sustained draw measured {ctx.num(180, 700)} W per device with a peak of "
        f"{ctx.num(250, 900)} W. Under sustained load the device reached "
        f"{ctx.num(62, 88)}°C and did not thermal throttle over a "
        f"{ctx.num(30, 180)}-minute soak. Ambient was {ctx.num(18, 27)}°C; results "
        f"in a warmer rack should be expected to differ.",
        "## Cost",
        f"At the measured throughput, serving {ctx.num(1, 60)}M tokens per day "
        f"requires {ctx.num(1, 8)} device(s) with headroom, giving an amortised "
        f"cost of roughly ${ctx.flt(0.02, 1.8, 3)} per thousand output tokens "
        f"exclusive of power and support.",
        "## Recommendation",
        f"Provision {gpus[0]} for the interactive path and keep {gpus[2]} for "
        f"batch. Do not run both workloads on one device: the batch job's long "
        f"sequences evict the interactive path's KV cache and p95 latency roughly "
        f"{ctx.flt(1.5, 4.0, 1)}x under mixed load.",
    ]
    title = f"GPU Benchmark: {model} on {gpus[0]} ({precision})"
    tags = ["benchmark", "gpu", "performance", slug(model), slug(project)]
    return title, "\n\n".join(body), tags, project


# ══════════════════════════════════════════════════════════════════════════════
# Corpus plan
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class TypeSpec:
    key: str
    count: int
    gen: Callable[[Ctx], tuple[str, str, list[str], str]]
    source: str
    filler: list[str] = field(default_factory=lambda: RISK_LINES + NEXT_STEPS)


PLAN: list[TypeSpec] = [
    TypeSpec("meeting_minutes", 30, gen_meeting_minutes, "Confluence"),
    TypeSpec("architecture", 22, gen_architecture, "Confluence"),
    TypeSpec("deployment_guide", 20, gen_deployment_guide, "GitLab Wiki"),
    TypeSpec("email", 34, gen_email, "Outlook"),
    TypeSpec("incident_report", 24, gen_incident_report, "Jira"),
    TypeSpec("project_documentation", 26, gen_project_doc, "SharePoint"),
    TypeSpec("api_reference", 20, gen_api_reference, "GitLab Wiki"),
    TypeSpec("research_note", 18, gen_research_note, "Notion"),
    TypeSpec("ocr_document", 16, gen_ocr_document, "Document Scanner"),
    TypeSpec("model_comparison", 14, gen_model_comparison, "Notion"),
    TypeSpec("tender_document", 16, gen_tender, "Tender Portal"),
    TypeSpec("infrastructure", 18, gen_infrastructure, "Confluence"),
    TypeSpec("docker_compose", 12, gen_docker_compose, "GitLab Wiki"),
    TypeSpec("fastapi_doc", 12, gen_fastapi_doc, "GitLab Wiki"),
    TypeSpec("django_doc", 10, gen_django_doc, "GitLab Wiki"),
    TypeSpec("gpu_benchmark", 18, gen_gpu_benchmark, "Google Drive"),
]

TOTAL = sum(s.count for s in PLAN)


def build_corpus() -> list[Doc]:
    """Compose every document. Pure — no filesystem access."""
    docs: list[Doc] = []
    index = 0
    for spec in PLAN:
        for _ in range(spec.count):
            index += 1
            ctx = Ctx(index)
            title, body, tags, project = spec.gen(ctx)

            target = ctx.num(TARGET_LO, TARGET_HI)
            paras = pad_to_target(ctx, body.split("\n\n"), spec.filler, target)
            body = trim_to_max("\n\n".join(paras))

            author = ctx.pick(STAFF)
            dept = (CATALOG[author].props.get("department")
                    or ctx.pick(DEPARTMENTS))
            doc_id = f"emd-{index:04d}"
            doc = Doc(doc_id=doc_id, title=title, author=author, project=project,
                      date=ctx.day().isoformat(), department=dept, tags=tags,
                      source=spec.source, doc_type=spec.key, body=body)
            doc.filename = f"{doc_id}-{spec.key}-{slug(title)}.md"
            docs.append(doc)
    return docs


# ══════════════════════════════════════════════════════════════════════════════
# Write + verify
# ══════════════════════════════════════════════════════════════════════════════

def write_corpus(docs: list[Doc], out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (out_dir / doc.filename).write_text(doc.render(), encoding="utf-8")
    return len(docs)


def verify(out_dir: Path) -> dict[str, Any]:
    """Read the written corpus back and check it against the stated contract."""
    files = sorted(out_dir.glob("*.md"))
    required = ("title", "author", "project", "date", "department", "tags", "source")

    counts: Counter = Counter()
    sources: Counter = Counter()
    projects: Counter = Counter()
    authors: Counter = Counter()
    depts: Counter = Counter()
    wcs: list[int] = []
    missing_meta: list[str] = []
    out_of_range: list[dict] = []

    for f in files:
        text = f.read_text(encoding="utf-8")
        head, _, body = text.partition("---\n\n")
        meta = dict(re.findall(r"^(\w+):\s*(.+)$", head, flags=re.M))
        for key in required:
            if key not in meta or not meta[key].strip(' "'):
                missing_meta.append(f"{f.name}:{key}")
        n = words(body)
        wcs.append(n)
        if not (MIN_WORDS <= n <= MAX_WORDS):
            out_of_range.append({"file": f.name, "words": n})
        counts[meta.get("doc_type", "?").strip('"')] += 1
        sources[meta.get("source", "?").strip('"')] += 1
        projects[meta.get("project", "?").strip('"')] += 1
        authors[meta.get("author", "?").strip('"')] += 1
        depts[meta.get("department", "?").strip('"')] += 1

    wcs.sort()
    n = len(wcs) or 1
    bodies = [f.read_text(encoding="utf-8").partition("---\n\n")[2] for f in files]
    return {
        "files": len(files),
        "total_words": sum(wcs),
        "word_min": wcs[0] if wcs else 0,
        "word_max": wcs[-1] if wcs else 0,
        "word_mean": round(sum(wcs) / n, 1),
        "word_median": wcs[n // 2] if wcs else 0,
        "out_of_range": out_of_range,
        "missing_metadata": missing_meta,
        "doc_types": dict(counts.most_common()),
        "sources": dict(sources.most_common()),
        "departments": dict(depts.most_common()),
        "distinct_projects": len(projects),
        "distinct_authors": len(authors),
        "top_projects": dict(projects.most_common(8)),
        "duplicate_bodies": len(bodies) - len(set(bodies)),
        "estimated_chunks": sum(max(1, -(-w // 240)) for w in wcs),  # 300-word window, 60 overlap
    }


def _report(stats: dict[str, Any], out_dir: Path) -> None:
    B, D, R = "\033[1m", "\033[2m", "\033[0m"
    ok, bad = "\033[32m✓\033[0m", "\033[31m✗\033[0m"

    print(f"\n{B}Corpus{R}  {D}{out_dir}{R}")
    print(f"  documents            {stats['files']}")
    print(f"  total words          {stats['total_words']:,}")
    print(f"  words min/mean/max   {stats['word_min']} / {stats['word_mean']} / {stats['word_max']}")
    print(f"  median words         {stats['word_median']}")
    print(f"  distinct projects    {stats['distinct_projects']}")
    print(f"  distinct authors     {stats['distinct_authors']}")
    print(f"  estimated chunks     ~{stats['estimated_chunks']}")

    print(f"\n{B}Contract checks{R}")
    m = ok if not stats["out_of_range"] else bad
    print(f"  {m} every document within {MIN_WORDS}–{MAX_WORDS} words "
          f"({len(stats['out_of_range'])} outside)")
    m = ok if not stats["missing_metadata"] else bad
    print(f"  {m} all 7 metadata fields present ({len(stats['missing_metadata'])} missing)")
    m = ok if not stats["duplicate_bodies"] else bad
    print(f"  {m} no duplicate document bodies ({stats['duplicate_bodies']} duplicates)")
    for item in stats["out_of_range"][:5]:
        print(f"      {D}{item['file']} — {item['words']} words{R}")

    print(f"\n{B}Document types{R}")
    for k, v in stats["doc_types"].items():
        print(f"  {k:<24} {v:>4}  {'▪' * v}")

    print(f"\n{B}Source systems{R}")
    for k, v in stats["sources"].items():
        print(f"  {k:<24} {v:>4}")

    print(f"\n{B}Departments{R}")
    for k, v in stats["departments"].items():
        print(f"  {k:<24} {v:>4}")

    print(f"\n{B}Top projects{R}")
    for k, v in stats["top_projects"].items():
        print(f"  {k:<24} {v:>4}")
    print()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Generate an enterprise document corpus.")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="compose but write nothing")
    ap.add_argument("--verify-only", action="store_true", help="check an existing corpus")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.dry_run:
        docs = build_corpus()
        wcs = sorted(words(d.body) for d in docs)
        plan = {"documents": len(docs), "total_words": sum(wcs),
                "word_min": wcs[0], "word_max": wcs[-1],
                "word_mean": round(sum(wcs) / len(wcs), 1),
                "out_of_range": sum(1 for w in wcs if not (MIN_WORDS <= w <= MAX_WORDS)),
                "by_type": {s.key: s.count for s in PLAN}}
        print(json.dumps(plan, indent=2))
        return 0

    if not args.verify_only:
        docs = build_corpus()
        written = write_corpus(docs, args.out)
        print(f"[corpus] wrote {written} documents to {args.out}")

    stats = verify(args.out)
    if args.json:
        print(json.dumps(stats, indent=2))
    else:
        _report(stats, args.out)
    return 0 if not (stats["out_of_range"] or stats["missing_metadata"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
