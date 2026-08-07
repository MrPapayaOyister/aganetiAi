"""
LLM contract for extraction: JSON schemas and the prompts that request them.

Both the allowed-label list and the allowed-relationship list are GENERATED from
the enums in models.py. That is the point — the vocabulary is defined once, and
the prompt, the validation and the database constraints can never drift apart.
Adding a label to `NodeLabel` teaches the extractor about it automatically.

Extraction is deliberately two calls, not one. Asking for entities and
relationships together makes the model invent relationships between things it
did not extract, and makes a malformed edge poison the whole response. Split, the
entity pass grounds the relationship pass: the second prompt is given the exact
entity names and told to use only those.
"""
from __future__ import annotations

import json

from .models import NodeLabel, RelType

ALLOWED_LABELS: list[str] = [label.value for label in NodeLabel]
ALLOWED_REL_TYPES: list[str] = [rel.value for rel in RelType]


# ── JSON schemas (also serve as documentation of the contract) ────────────────

ENTITY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ALLOWED_LABELS},
                    "name": {"type": "string"},
                },
                "required": ["type", "name"],
            },
        }
    },
    "required": ["entities"],
}

RELATIONSHIP_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "type": {"type": "string", "enum": ALLOWED_REL_TYPES},
                    "target": {"type": "string"},
                },
                "required": ["source", "type", "target"],
            },
        }
    },
    "required": ["relationships"],
}


# ── Prompts ───────────────────────────────────────────────────────────────────

_LABEL_HINTS = """\
Person            a named human being
User              an account holder in this platform
Organization      a body of people; Company for commercial ones
Company           a commercial organisation
Location          a place, city, country, office
Project           a named body of work (e.g. "Agentic AI")
Task              a single unit of work / to-do
Technology        a named tool, framework, library, protocol or standard
Provider          a vendor supplying a service (e.g. Anthropic, OpenAI)
Model             a named ML model (e.g. qwen-fast, gpt-4.1)
Service           a running service or daemon
API               a named interface (e.g. Microsoft Graph, REST API)
Plugin            an extension or add-on
Repository        a source-code repository
Issue / PR        a tracked issue / pull request
Document          a file, report, spec or attachment
Email             an email message
Meeting           a scheduled meeting
Calendar          a calendar
Conversation      a chat thread
Message           one message inside a conversation
Memory            a stored recollection or fact
Entity            use ONLY when nothing above fits"""


def entity_prompt(text: str) -> str:
    """Prompt for STEP 1 — entities only, no relationships."""
    return f"""You extract entities from text to build a knowledge graph.

Return ONLY a JSON object of this exact shape:
{{"entities": [{{"type": "<LABEL>", "name": "<canonical name>"}}]}}

"type" MUST be one of these labels, spelled exactly:
{", ".join(ALLOWED_LABELS)}

What each label means:
{_LABEL_HINTS}

Rules:
- Extract only entities that are ACTUALLY NAMED in the text. Never invent one.
- "name" is the proper name as written, not a description and not a sentence.
- Use the COMPLETE product name exactly as the text writes it. "Microsoft Graph"
  is one entity named "Microsoft Graph" — never shorten it to "Microsoft", and
  never split it into two entities. The same applies to any multi-word name.
- Use the most specific label that fits; fall back to Entity only as a last resort.
- Do not extract pronouns, dates, quantities, or generic nouns ("the system", "a file").
- If the same thing appears twice, list it once.
- If there are no entities, return {{"entities": []}}.

TEXT:
{text}"""


def relationship_prompt(text: str, entity_names: list[str]) -> str:
    """Prompt for STEP 2 — relationships between ALREADY-EXTRACTED entities."""
    listed = "\n".join(f"- {n}" for n in entity_names) or "(none)"
    return f"""You extract relationships between known entities to build a knowledge graph.

Return ONLY a JSON object of this exact shape:
{{"relationships": [{{"source": "<entity>", "type": "<TYPE>", "target": "<entity>"}}]}}

"source" and "target" MUST be copied EXACTLY from this list of entities:
{listed}

"type" MUST be one of these, spelled exactly:
{", ".join(ALLOWED_REL_TYPES)}

Rules:
- Only state a relationship the text actually supports. Never infer one that is
  merely plausible.
- Direction matters: (source)-[TYPE]->(target). "A uses B" is A USES B.
- Never use an entity name that is not in the list above.
- Pick the most specific type; RELATED_TO is a last resort.
- If there are no relationships, return {{"relationships": []}}.

TEXT:
{text}"""


def schema_reference() -> str:
    """Human-readable dump of the vocabulary — used by the demo script."""
    return json.dumps({"labels": ALLOWED_LABELS, "relationship_types": ALLOWED_REL_TYPES},
                      indent=2)


# ── Unified single-call extraction (phase 2.5) ───────────────────────────────
# One request returns entities, relationships, summary, keywords and confidence.
# Two reasons this beats the two-call design beyond halving latency:
#   * the model sees its own entity list while writing relationships, so
#     endpoints match by construction instead of by prompt discipline;
#   * an entity's labels and a relationship's confidence are decided in one
#     reasoning pass, so they cannot disagree with each other.

UNIFIED_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ALLOWED_LABELS},
                    "also": {"type": "array", "items": {"type": "string", "enum": ALLOWED_LABELS}},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["name", "type", "confidence"],
            },
        },
        "relationships": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "type": {"type": "string", "enum": ALLOWED_REL_TYPES},
                    "target": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["source", "type", "target", "confidence"],
            },
        },
        "summary": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["entities", "relationships", "summary", "keywords", "confidence"],
}


def unified_prompt(text: str, source_type: str = "conversation") -> str:
    """One prompt for everything. `source_type` only frames the text for the model."""
    return f"""You build a knowledge graph from text. Read the {source_type} below and
return ONE JSON object describing what it contains.

Return ONLY this shape:
{{
  "entities": [
    {{"name": "<full proper name>", "type": "<LABEL>", "also": ["<LABEL>", ...],
     "aliases": ["<other spellings used in the text>"], "confidence": 0.0-1.0}}
  ],
  "relationships": [
    {{"source": "<entity name>", "type": "<TYPE>", "target": "<entity name>",
     "confidence": 0.0-1.0}}
  ],
  "summary": "<one or two sentences>",
  "keywords": ["<topic>", ...],
  "confidence": 0.0-1.0
}}

"type" for entities MUST be one of:
{", ".join(ALLOWED_LABELS)}

What each label means:
{_LABEL_HINTS}

"type" for relationships MUST be one of:
{", ".join(ALLOWED_REL_TYPES)}

ENTITY RULES
- Extract only entities ACTUALLY NAMED in the text. Never invent one.
- Use the COMPLETE product name exactly as written. "Microsoft Graph" is ONE
  entity named "Microsoft Graph" — never shorten it to "Microsoft", never split it.
- "type" is the single most specific label. "also" lists any OTHER labels that
  are genuinely true of the same thing (e.g. Microsoft Graph is a Technology
  and also an API). Leave "also" empty when unsure — a wrong label is worse
  than a missing one.
- "aliases" holds other spellings the TEXT itself uses for that entity
  (e.g. "MS Graph", "the Graph API"). Empty list if there are none.
- Do not extract pronouns, dates, quantities or generic nouns.

RELATIONSHIP RULES
- "source" and "target" MUST exactly match a "name" you listed in "entities".
- Only state what the text supports. Never infer a merely plausible link.
- Direction matters: (source)-[TYPE]->(target). "A uses B" is A USES B.
- Prefer the most specific type; RELATED_TO is a last resort.

CONFIDENCE
- Per item: how certain you are that item is correct and correctly typed.
- Top level: how certain you are about the extraction as a whole.
- Be honest. 0.5 for a genuine guess is more useful than a false 0.95.

TEXT:
{text}"""
