# POC-3 — The Minimal Real Agent Execution Loop

**Status:** implemented and demonstrated end-to-end against the live stack.
**Scope:** one agentic loop. Not multi-agent collaboration, not a production planner.

---

## 1. What it does

```
USER
 ↓
AGENT RUNTIME  (LangGraph — reused)
 ↓
PLANNER AGENT        → a bounded, inspectable plan
 ↓
KNOWLEDGE AGENT      → knowledge_search (the ONLY retrieval path)
 ↓                      ↳ Qdrant + Neo4j, tenant-filtered by POC-2
EVIDENCE             → citations carried verbatim
 ↓
DRAFT                → an answer written ONLY from the evidence
 ↓
VERIFICATION AGENT   → SUPPORTED | PARTIALLY_SUPPORTED | UNSUPPORTED | CONFLICTING
 ↓
FINAL ANSWER         → released, or refused with what is missing
```

The load-bearing edge is the last one: **`finalize` reads the verdict, not the
draft.** An answer exists after `draft` and is released only if `verify` permits
it. Remove that edge and the system answers from retrieval alone — the behaviour
POC-3 exists to prevent.

---

## 2. What was reused, and what is new

| Reused (unchanged) | Used for |
|---|---|
| LangGraph + `StateGraph` | the runtime |
| `orchestrator/graph.py::AgentState` | extended by TypedDict inheritance — **not copied, not edited** |
| `orchestrator/llm.chat` → `router.plan()` | every model call; model choice stays in the gateway |
| `orchestrator/knowledge.knowledge_search` | every retrieval, and therefore POC-2's tenant predicate for both stores |
| `context/` engine, GraphRAG, frozen config | retrieval itself — untouched |

**New:** `backend/agents/` — `contract.py`, `planner.py`, `knowledge_agent.py`,
`verification_agent.py`, `loop.py`. Nothing else was modified.

**Not built, deliberately:** no second orchestration framework, no second tool
registry, no second retrieval implementation, no parallel state store. This is a
second *graph* in the existing framework — a linear pipeline, because
plan→retrieve→verify is a pipeline, while `orchestrator/graph.py` is a **cyclic**
tool-calling executor. Forcing this into that cycle would put the planner back
inside the model loop on every step, which is the uncontrolled loop the brief
forbids.

---

## 3. The agent contract

```python
AgentSpec(name, description, capabilities)          # the DECLARATION
AgentRequest(task, user_id, tenant_id, session_id, context)
AgentResult(agent, status, output, evidence, errors, took_ms)
class Agent(Protocol): spec; async def run(request) -> AgentResult
```

`AgentStatus` is `OK | INSUFFICIENT | FAILED | SKIPPED`. `INSUFFICIENT` is
separate from `FAILED` on purpose — an agent that ran correctly and found nothing
is not broken, and collapsing the two would make "no evidence exists"
indistinguishable from "retrieval was down".

**Designed to become `AgentManifest`, not to be replaced by it.** `AgentSpec`
holds only serialisable fields; nothing executable lives on it. The migration is
`AgentSpec.from_manifest(...)` plus a handler lookup — the same
declaration/projection split the tool registry already uses for `Tool`.

Deliberately absent (all Phase-6 fields): version, pack, entitlement, tool
allowlist, policy reference, model profile.

---

## 4. Model-selection boundary

Agents request a **capability profile**, never a model:

```python
llm.chat(messages, tier="tool", temperature=0.0, ctx={...})
        → router.complete → router.plan() → concrete model_key
```

No agent module contains a model or provider name — asserted by
`test_no_agent_names_a_model`, which scans all four modules for `gpt-`, `qwen`,
`claude`, `azure/`, `openai`, `vllm`, `AsyncOpenAI` and friends. Changing the
deployment's model map requires no change here.

---

## 5. Tenant propagation

```
run_agent_loop(tenant_id=…)
  → AgentRunState["tenant_id"]        (inherited from AgentState)
    → AgentRequest.tenant_id          (explicit argument, not a ContextVar)
      → knowledge_search(tenant_id=…)
        → Qdrant org_id predicate  +  Neo4j node-property predicate
```

Carried explicitly rather than read from the ContextVar. The ContextVar exists
and is the correct fallback for code far from a request, but an isolation
boundary that depends on ambient state is one `asyncio.to_thread` away from being
silently absent.

`tenant_enforced_by` is carried through to the result, so a caller can tell
*which* providers actually applied a predicate. A populated `tenant_id` is not
an enforced one.

---

## 6. Tool boundary

The Knowledge Agent calls `knowledge_search` and nothing else. It does not
import `qdrant_client`, the Neo4j driver, `backend.ingest`, or
`backend.knowledge_graph` — enforced by a test that strips docstrings and
comments, then scans the executable code.

This is not tidiness. `knowledge_search` is where the tenant predicate is applied
for both stores. An agent querying a store directly would be correctly scoped
only by accident, and the day someone added a second direct call the isolation
would regress with no failing test — because every tenancy test sits behind
`knowledge_search`.

---

## 7. Verification behaviour

**Deterministic floor** — decided in code, never asked of a model:

| Condition | Verdict |
|---|---|
| no evidence at all | `UNSUPPORTED` |
| no draft to check | `UNSUPPORTED` |
| verifier unavailable / unparseable | `UNSUPPORTED` (**fails closed**) |
| model says `SUPPORTED` with zero evidence | clamped to `UNSUPPORTED` |

Everything above the floor is a judgement call and does go to the gateway.

**With no evidence, the draft step is skipped entirely** — the model is never
asked to answer, so it cannot hallucinate what it was not asked. `finalize` then
emits a refusal naming what is missing.

`PARTIALLY_SUPPORTED` releases the draft **with its caveat appended**. Refusing a
partly-evidenced answer outright is its own dishonesty when the evidence
genuinely covers part of it.

---

## 8. Planner bounds

Bounded by construction, not by prompt instruction:

- `MAX_STEPS = 4` — truncated, so a fifty-step plan cannot run fifty.
- A step naming an unknown agent is **dropped, not executed**. The legal agent
  list is passed in; the model cannot invent a capability.
- No loop, no re-planning. One plan, executed once.
- Unparseable output → the deterministic `knowledge → verification` plan, with
  `fallback_used: True` recorded and an error surfaced, so a demo cannot show a
  hand-written plan as if the model produced it.

---

## 9. Running it

```python
from backend.agents import run_agent_loop

state = await run_agent_loop(
    request="What does the platform use LiteLLM for?",
    user_id=ctx.user_id, tenant_id=str(ctx.tenant_id), session_id=session_id,
)
state["plan"] / state["evidence"] / state["verification"] / state["final_answer"] / state["trace"]
```

The returned state carries plan, evidence, verdict, answer and trace, so a caller
can show its work without a second call.

**Not yet wired to a route.** `/agent/chat` (`routes/agent_os.py`) and
`chat/unified.py` are both under concurrent modification; wiring POC-3 into them
would mean editing two contended files. The backend path works and is tested;
the route is the next small step.

---

## 10. Demonstrated end-to-end

Live gateway, live corpus, live graph, no stubs:

| Question | Evidence | Verdict | Outcome |
|---|---|---|---|
| "What does the platform use LiteLLM for?" | 7 (1 doc + 6 graph) | `PARTIALLY_SUPPORTED` | answered, cited `[G4] [G5]` |
| "How does Agentic AI relate to LiteLLM?" | 4 (1 doc + 3 graph) | `PARTIALLY_SUPPORTED` | answered from `agentic-ai —USES→ litellm` |
| "Policy on interplanetary shipping insurance?" | 1 irrelevant chunk | `UNSUPPORTED` | **refused, named what was missing** |

The third is the strongest result: retrieval returned *something*, so the
deterministic floor did **not** fire — the model verifier had to judge, and it
refused. That is precisely "the final answer must not be produced simply because
the Knowledge Agent returned something".

With a real tenant, `tenant_enforced_by: ["corporate", "graph"]`.

---

## 11. Limitations

- Not wired to `/agent/chat` (see §9). No UI.
- Single-turn. No conversation history in the loop.
- No approval gate — POC-3 has no outbound action to approve. The existing gate
  is untouched and applies to the other runtime.
- The plan is executed by fixed nodes, not dispatched from `plan.steps`. Adding a
  fourth agent needs a node, not just a spec — acceptable at three agents,
  and the point at which it stops being acceptable is the point to build a
  dispatcher.
- Verification is one model call, no per-claim alignment.
- Draft and verify each cost a model call: ~3 gateway calls per run.
