/**
 * The POC-3 "agent process" contract, as the client sees it.
 *
 * The backend streams the EXISTING frame vocabulary (backend/chat/frames.py) —
 * `stage` for pipeline steps and `artifact` for structured payloads. No new
 * event type was added for this, so every other consumer of the stream keeps
 * working untouched.
 *
 * The reducer lives here rather than in the hook or the component for two
 * reasons: it is the only part with real logic, so it is the part worth testing
 * without a browser; and it is where the field whitelist lives.
 *
 * WHY A WHITELIST AND NOT A SPREAD. The server already strips retrieval
 * diagnostics (scores, corroboration counts, provider names, tenant ids) before
 * it emits an artifact. Copying only named fields here means that if the server
 * ever regresses and starts sending more, the UI still cannot render it — the
 * leak would have to pass two independent gates instead of one. `{...data}`
 * would make the client faithfully display whatever it was handed.
 */

/** The five steps a customer sees. `router` is emitted by the lane dispatcher
 *  but is deliberately not shown: it is routing, not agent work. */
export const AGENT_STAGES = [
  { key: 'plan', label: 'Plan' },
  { key: 'knowledge', label: 'Knowledge' },
  { key: 'draft', label: 'Draft' },
  { key: 'verify', label: 'Verify' },
  { key: 'finalize', label: 'Finalize' },
] as const

export type StageKey = (typeof AGENT_STAGES)[number]['key']
export type StageStatus = 'pending' | 'running' | 'done' | 'skipped' | 'error'

export interface AgentStage {
  key: string
  label: string
  status: StageStatus
  ms?: number
}

export interface AgentPlanStep { id: string; agent: string; task: string }
export interface AgentPlan { goal: string; steps: AgentPlanStep[] }

/** A citation as the SERVER chose to expose it. Four fields, no scores. */
export interface AgentCitation {
  kind: string
  source: string
  text: string
  /** "document" | "knowledge graph" — a plain word, never the store's name. */
  where: string
}

export interface AgentEvidence {
  citations: AgentCitation[]
  counts: { total?: number; documents?: number; graph?: number }
}

export type Verdict =
  | 'SUPPORTED' | 'PARTIALLY_SUPPORTED' | 'UNSUPPORTED' | 'CONFLICTING'

export interface AgentVerification {
  verdict: Verdict
  /** Server-supplied wording, so the label cannot drift from the verdict. */
  label: string
  explanation: string
  missing: string[]
  conflicts: string[]
  /** Whether the answer was released. Mirrors `done.verified`. */
  supported: boolean
}

export interface AgentProcessData {
  stages: AgentStage[]
  plan?: AgentPlan
  evidence?: AgentEvidence
  verification?: AgentVerification
  /** From the `done` frame. Kept separate from `verification.supported` so a
   *  disagreement between the two is visible rather than silently reconciled. */
  verified?: boolean
}

const KNOWN_STAGE_KEYS = new Set<string>(AGENT_STAGES.map(s => s.key))
const VERDICTS = new Set<Verdict>(
  ['SUPPORTED', 'PARTIALLY_SUPPORTED', 'UNSUPPORTED', 'CONFLICTING'])

/** All five stages pending — the shape before any frame arrives. */
export function emptyAgentProcess(): AgentProcessData {
  return { stages: AGENT_STAGES.map(s => ({ ...s, status: 'pending' as StageStatus })) }
}

const str = (v: unknown, fallback = ''): string =>
  typeof v === 'string' ? v : fallback

const strList = (v: unknown): string[] =>
  Array.isArray(v) ? v.filter(x => typeof x === 'string') as string[] : []

function toStatus(raw: unknown): StageStatus {
  switch (raw) {
    case 'running': return 'running'
    case 'done': return 'done'
    case 'skipped': return 'skipped'
    case 'error': return 'error'
    default: return 'pending'
  }
}

/**
 * Fold one SSE frame into the agent-process state.
 *
 * Returns the SAME object when the frame is not an agent frame, so a caller can
 * cheaply tell "did this turn produce agent data at all?" by identity — which is
 * what decides whether the panel renders. An ordinary Runtime A turn never
 * produces agent frames and therefore never gets a panel.
 */
export function reduceAgentFrame(
  state: AgentProcessData | undefined,
  frame: { type?: string; [k: string]: unknown },
): AgentProcessData | undefined {
  if (!frame || typeof frame !== 'object') return state

  if (frame.type === 'stage') {
    const key = str(frame.stage)
    // `router` and anything unrecognised is ignored rather than appended: the
    // panel shows a fixed five-step pipeline, and an unknown key would render
    // as a mystery row.
    if (!KNOWN_STAGE_KEYS.has(key)) return state
    const next = state ?? emptyAgentProcess()
    const ms = typeof frame.ms === 'number' ? frame.ms : undefined
    return {
      ...next,
      stages: next.stages.map(s =>
        s.key === key
          ? { ...s, status: toStatus(frame.status), ...(ms !== undefined ? { ms } : {}) }
          : s),
    }
  }

  if (frame.type === 'artifact') {
    const art = frame.artifact as { kind?: unknown; data?: unknown } | undefined
    if (!art || typeof art !== 'object') return state
    const data = (art.data ?? {}) as Record<string, unknown>
    const next = state ?? emptyAgentProcess()

    if (art.kind === 'agent_plan') {
      const steps = Array.isArray(data.steps) ? data.steps : []
      return {
        ...next,
        plan: {
          goal: str(data.goal),
          steps: steps
            .filter((s): s is Record<string, unknown> => !!s && typeof s === 'object')
            .map(s => ({ id: str(s.id), agent: str(s.agent), task: str(s.task) })),
        },
      }
    }

    if (art.kind === 'agent_evidence') {
      const cites = Array.isArray(data.citations) ? data.citations : []
      const counts = (data.counts ?? {}) as Record<string, unknown>
      return {
        ...next,
        evidence: {
          // Four named fields. Anything else the server sends is dropped here.
          citations: cites
            .filter((c): c is Record<string, unknown> => !!c && typeof c === 'object')
            .map(c => ({
              kind: str(c.kind, 'document'),
              source: str(c.source),
              text: str(c.text),
              where: str(c.where, 'document'),
            })),
          counts: {
            total: typeof counts.total === 'number' ? counts.total : undefined,
            documents: typeof counts.documents === 'number' ? counts.documents : undefined,
            graph: typeof counts.graph === 'number' ? counts.graph : undefined,
          },
        },
      }
    }

    if (art.kind === 'agent_verification') {
      const raw = str(data.verdict).toUpperCase() as Verdict
      const verdict: Verdict = VERDICTS.has(raw) ? raw : 'UNSUPPORTED'
      return {
        ...next,
        verification: {
          verdict,
          label: str(data.label, verdict.replace(/_/g, ' ').toLowerCase()),
          explanation: str(data.explanation),
          missing: strList(data.missing),
          conflicts: strList(data.conflicts),
          // Fail closed: anything other than an explicit `true` is not supported.
          supported: data.supported === true,
        },
      }
    }
    return state
  }

  if (frame.type === 'done') {
    // Only meaningful once agent frames have been seen; a Runtime A `done`
    // must not conjure a panel out of nothing.
    if (!state) return state
    return { ...state, verified: frame.verified === true }
  }

  return state
}

/**
 * Is this answer safe to present as substantiated?
 *
 * Requires BOTH signals to agree. They are produced by different parts of the
 * backend (the verification artifact and the `done` frame), and if they ever
 * disagree the honest reading is the cautious one — a refusal shown as
 * unverified is a small cost, an unsupported answer shown with a green badge is
 * the failure this whole loop exists to prevent.
 */
export function isSubstantiated(d: AgentProcessData | undefined): boolean {
  if (!d?.verification) return false
  if (d.verified === false) return false
  return d.verification.supported === true
}
