"""
Document drafting on top of the existing PDF/WeasyPrint engine.

Turns a natural-language ask ("draft a one-pager on our Q3 priorities, include my
calendar and open tasks") into a branded PDF: assemble context → LLM drafts markdown
→ render to a styled PDF with the user's report_style.
"""

from __future__ import annotations

import html
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

from backend.services import llm as _llm
from reports.pdf_generator import get_user_report_style

try:
    from config.users import USERS
except Exception:
    USERS = {}

# doc_type -> (label, structural guidance handed to the model)
DOC_TEMPLATES = {
    "memo": ("Internal Memo",
             "Format as a memo. Start with a short **TO / FROM / DATE / RE** block, "
             "then 2-4 concise sections with ## headings, then a brief closing."),
    "proposal": ("Proposal",
                 "Use these ## sections: Executive Summary, Background, Proposed "
                 "Solution, Scope & Deliverables, Timeline, Next Steps."),
    "sop": ("Standard Operating Procedure",
            "Use these ## sections: Purpose, Scope, Responsibilities, Procedure "
            "(a numbered ordered list of concrete steps), and References."),
    "one-pager": ("One-Pager",
                  "Keep it to one page. Use ## Overview, ## Key Points (bullets), "
                  "## Highlights, ## Next Steps."),
    "letter": ("Letter",
               "Format as a formal business letter: greeting, 2-3 body paragraphs, "
               "and a professional sign-off."),
    "brief": ("Briefing Note",
              "Use ## Situation, ## Key Facts (bullets), ## Analysis, ## Recommendation."),
}

DOC_ALIASES = {
    "memo": "memo", "proposal": "proposal", "sop": "sop", "procedure": "sop",
    "one pager": "one-pager", "one-pager": "one-pager", "onepager": "one-pager",
    "letter": "letter", "brief": "brief", "briefing": "brief", "note": "brief",
}


def normalize_doc_type(raw: str | None) -> str:
    if not raw:
        return "memo"
    r = raw.lower().strip()
    for key, val in DOC_ALIASES.items():
        if key in r:
            return val
    return "memo"


# ── context assembly ────────────────────────────────────────────────────────
def _assemble_context(user_id: str, includes: list[str], topic: str) -> str:
    blocks: list[str] = []
    inc = {i.lower() for i in (includes or [])}

    if "calendar" in inc:
        try:
            from backend.services.mailbox import agenda_text_sync
            agenda = agenda_text_sync(user_id)
            if agenda and agenda.strip():
                blocks.append(f"TODAY'S CALENDAR:\n{agenda.strip()}")
        except Exception:
            pass

    if "tasks" in inc:
        try:
            from tasks.store import get_pending_summary
            tasks = get_pending_summary(user_id)
            if tasks and tasks.strip():
                blocks.append(f"PENDING TASKS:\n{tasks.strip()}")
        except Exception:
            pass

    if "agents" in inc or "delegations" in inc:
        try:
            from integrations.agent_inbox import get_pending_messages
            from config.users import USERS as _U
            agent_id = _U.get(user_id, {}).get("agent_id")
            if agent_id:
                msgs = get_pending_messages(agent_id)
                if msgs:
                    lines = [f"- {m.get('type','msg')}: {m.get('payload',{})}" for m in msgs[:10]]
                    blocks.append("OPEN DELEGATIONS:\n" + "\n".join(lines))
        except Exception:
            pass

    if "memory" in inc or "rag" in inc or "company" in inc:
        try:
            from memory.long_term import search_memory
            mem = search_memory(user_id, topic or "recent decisions", top_k=5)
            if mem and mem.strip():
                blocks.append(f"RELEVANT MEMORY:\n{mem.strip()}")
        except Exception:
            pass

    return "\n\n".join(blocks)


# ── minimal, safe markdown → HTML (markdown lib isn't installed) ─────────────
def _inline(text: str) -> str:
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
    return text


def md_to_html(md: str) -> str:
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    list_type: str | None = None  # 'ul' | 'ol'
    para: list[str] = []

    def flush_para():
        if para:
            out.append(f"<p>{_inline(' '.join(para))}</p>")
            para.clear()

    def close_list():
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_para(); close_list(); continue

        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush_para(); close_list()
            level = min(len(m.group(1)), 3)
            out.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            continue

        m = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if m:
            flush_para()
            if list_type != "ol":
                close_list(); out.append("<ol>"); list_type = "ol"
            out.append(f"<li>{_inline(m.group(1).strip())}</li>")
            continue

        m = re.match(r"^\s*[-*+]\s+(.*)$", line)
        if m:
            flush_para()
            if list_type != "ul":
                close_list(); out.append("<ul>"); list_type = "ul"
            out.append(f"<li>{_inline(m.group(1).strip())}</li>")
            continue

        para.append(line.strip())

    flush_para(); close_list()
    return "\n".join(out)


# ── drafting ─────────────────────────────────────────────────────────────────
def _llm_markdown(doc_type: str, topic: str, context: str, structure: str) -> str:
    sys_prompt = (
        "You are an executive writing assistant. Draft a polished, professional "
        "document in GitHub-Flavored Markdown. Respond in clear English. "
        "Output ONLY the document markdown — no preamble, no code fences, no commentary. "
        f"{structure}"
    )
    user_prompt = f"Document type: {doc_type}\nTopic / instructions: {topic}\n"
    if context:
        user_prompt += (f"\nUse the following live context where relevant "
                        f"(do not invent facts beyond it):\n{context}\n")
    md = _llm.complete(
        [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": user_prompt}],
        temperature=0.4, max_tokens=1400, timeout=180.0).strip()
    # strip accidental code fences
    md = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", md).strip()
    return md


def draft_document(user_id: str, doc_type: str, topic: str,
                   includes: list[str] | None = None,
                   title: str | None = None) -> Path:
    user_id = os.path.basename(user_id)
    doc_type = normalize_doc_type(doc_type)
    label, structure = DOC_TEMPLATES[doc_type]

    context = _assemble_context(user_id, includes or [], topic)
    md = _llm_markdown(label, topic, context, structure)
    body_html = md_to_html(md)

    style = get_user_report_style(user_id)
    env = Environment(loader=FileSystemLoader("reports/templates"), autoescape=True)
    template = env.get_template("document_base.html")

    doc_title = title or (topic[:80] if topic else label)
    user_name = USERS.get(user_id, {}).get("name", "User")

    html_content = template.render(
        doc_title=doc_title,
        doc_type_label=label,
        user_name=user_name,
        company_name=os.getenv("COMPANY_NAME", "The Company"),
        generated_at=datetime.now(timezone.utc).strftime("%B %d, %Y"),
        body_html=body_html,
        font_family=style["font_family"],
        primary_color=style["primary_color"],
        include_header=style["include_header"],
    )

    output_dir = Path(f"temp/{user_id}/documents")
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{doc_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    HTML(string=html_content).write_pdf(str(out_path))
    return out_path
