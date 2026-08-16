/**
 * POC-3 agent-process contract tests.
 *
 * The project has NO frontend test framework and the brief forbids adding one,
 * so these run on Node's built-in `assert` via the `tsx` that is already
 * installed:
 *
 *     npx tsx src/lib/agentProcess.test.ts
 *
 * Scope is the reducer and the substantiation rule — the only parts with real
 * logic. Rendering is covered by the browser validation, which exercises the
 * actual component rather than a simulated DOM.
 */
import assert from 'node:assert/strict'
import {
  AGENT_STAGES, emptyAgentProcess, isSubstantiated, reduceAgentFrame,
  type AgentProcessData,
} from './agentProcess'

let passed = 0
function test(name: string, fn: () => void) {
  try { fn(); passed++; console.log(`  ✓ ${name}`) }
  catch (e) { console.error(`  ✗ ${name}\n    ${(e as Error).message}`); process.exitCode = 1 }
}

const fold = (frames: Array<Record<string, unknown>>): AgentProcessData | undefined =>
  frames.reduce<AgentProcessData | undefined>((s, f) => reduceAgentFrame(s, f), undefined)

const stage = (k: string, status: string, ms?: number) =>
  ({ type: 'stage', stage: k, status, label: k, ...(ms !== undefined ? { ms } : {}) })
const artifact = (kind: string, data: unknown, meta: unknown = {}) =>
  ({ type: 'artifact', artifact: { id: 'x', kind, title: kind, spec: {}, data, meta } })

// ── M. no agent frames ⇒ no panel ───────────────────────────────────────────

test('M: an ordinary turn produces no agent state at all', () => {
  const s = fold([
    { type: 'token', content: 'hello' },
    { type: 'sources', payload: { sources: [{ source: 'a.pdf' }] } },
    { type: 'embeds', payload: { embeds: [] } },
    { type: 'message', payload: { id: 'm1' } },
  ])
  assert.equal(s, undefined, 'a non-agent turn must not create a panel')
})

test('M: a done frame alone cannot conjure a panel', () => {
  assert.equal(fold([{ type: 'done', final: 'hi', verified: true }]), undefined)
})

test('M: the router stage is not shown and does not create a panel', () => {
  assert.equal(fold([stage('router', 'done')]), undefined,
    'router is lane dispatch, not agent work')
})

// ── B. stages ───────────────────────────────────────────────────────────────

test('B: a stage frame creates all five steps, others pending', () => {
  const s = fold([stage('plan', 'running')])!
  assert.equal(s.stages.length, 5)
  assert.deepEqual(s.stages.map(x => x.key), AGENT_STAGES.map(x => x.key))
  assert.equal(s.stages.find(x => x.key === 'plan')!.status, 'running')
  assert.equal(s.stages.find(x => x.key === 'verify')!.status, 'pending')
})

test('B: statuses advance and ms is captured', () => {
  const s = fold([stage('plan', 'running'), stage('plan', 'done', 12)])!
  const plan = s.stages.find(x => x.key === 'plan')!
  assert.equal(plan.status, 'done')
  assert.equal(plan.ms, 12)
})

test('B: skipped and error statuses survive', () => {
  const s = fold([stage('draft', 'skipped'), stage('verify', 'error')])!
  assert.equal(s.stages.find(x => x.key === 'draft')!.status, 'skipped')
  assert.equal(s.stages.find(x => x.key === 'verify')!.status, 'error')
})

test('B: an unknown status degrades to pending rather than rendering raw', () => {
  const s = fold([stage('plan', 'weird-new-status')])!
  assert.equal(s.stages.find(x => x.key === 'plan')!.status, 'pending')
})

// ── C. plan ─────────────────────────────────────────────────────────────────

test('C: agent_plan is parsed', () => {
  const s = fold([artifact('agent_plan', {
    goal: 'Answer from corporate knowledge',
    steps: [{ id: '1', agent: 'knowledge', task: 'find evidence' },
            { id: '2', agent: 'verification', task: 'verify' }],
  })])!
  assert.equal(s.plan!.goal, 'Answer from corporate knowledge')
  assert.deepEqual(s.plan!.steps.map(x => x.agent), ['knowledge', 'verification'])
  assert.equal(s.plan!.steps[0].task, 'find evidence')
})

test('C: a malformed step is dropped, not rendered as junk', () => {
  const s = fold([artifact('agent_plan', { goal: 'g', steps: [null, 'nope', { id: '1', agent: 'knowledge', task: 't' }] })])!
  assert.equal(s.plan!.steps.length, 1)
})

// ── D. evidence ─────────────────────────────────────────────────────────────

test('D: agent_evidence is parsed with counts', () => {
  const s = fold([artifact('agent_evidence', {
    citations: [{ kind: 'document', source: 'policy.pdf', text: 'Ninety days.', where: 'document' },
                { kind: 'relationship', source: 'doc-x', text: 'a —USES→ b', where: 'knowledge graph' }],
    counts: { total: 2, documents: 1, graph: 1 },
  }, { tenant_scoped: true })])!
  assert.equal(s.evidence!.citations.length, 2)
  assert.equal(s.evidence!.counts.total, 2)
  assert.equal(s.evidence!.citations[1].where, 'knowledge graph')
})

// ── L. no internal diagnostics ──────────────────────────────────────────────

test('L: a citation keeps ONLY the four public fields', () => {
  const s = fold([artifact('agent_evidence', {
    citations: [{
      kind: 'document', source: 'policy.pdf', text: 't', where: 'document',
      // everything below must be dropped even if the server regresses
      final_score: 0.91, corroboration: 3, provider: 'corporate',
      document_id: 'doc-a', evidence: [{ provider: 'corporate' }],
      org_id: 'tenant-a', top_k: 8, hop_decay: 0.55,
    }],
    counts: { total: 1 },
  })])!
  assert.deepEqual(Object.keys(s.evidence!.citations[0]).sort(),
    ['kind', 'source', 'text', 'where'])
  const blob = JSON.stringify(s)
  for (const leak of ['tenant-a', 'org_id', 'top_k', 'hop_decay', 'final_score',
                      'corroboration', 'qdrant', 'neo4j'])
    assert.ok(!blob.includes(leak), `leaked ${leak}`)
})

test('L: artifact meta is never copied into UI state', () => {
  const s = fold([artifact('agent_evidence', { citations: [], counts: {} },
                           { tenant_scoped: true, tenant_id: 'tenant-a' })])!
  assert.ok(!JSON.stringify(s).includes('tenant'), 'tenant metadata reached UI state')
})

// ── E. verification ─────────────────────────────────────────────────────────

test('E/I: SUPPORTED is parsed and substantiated', () => {
  const s = fold([
    artifact('agent_verification', {
      verdict: 'SUPPORTED', label: 'Supported by the evidence',
      explanation: 'The policy states it.', missing: [], conflicts: [], supported: true }),
    { type: 'done', final: 'x', verified: true },
  ])!
  assert.equal(s.verification!.verdict, 'SUPPORTED')
  assert.equal(isSubstantiated(s), true)
})

test('J: PARTIALLY_SUPPORTED keeps its caveat and is still released', () => {
  const s = fold([
    artifact('agent_verification', {
      verdict: 'PARTIALLY_SUPPORTED', label: 'Partially supported', explanation: 'e',
      missing: ['the renewal process'], conflicts: [], supported: true }),
    { type: 'done', final: 'x', verified: true },
  ])!
  assert.equal(s.verification!.verdict, 'PARTIALLY_SUPPORTED')
  assert.deepEqual(s.verification!.missing, ['the renewal process'])
  assert.equal(isSubstantiated(s), true)
})

test('K: UNSUPPORTED is a refusal and must never read as substantiated', () => {
  const s = fold([
    artifact('agent_verification', {
      verdict: 'UNSUPPORTED', label: 'Not supported by the evidence',
      explanation: 'The evidence does not answer the question.',
      missing: ['evidence that addresses the question'], conflicts: [], supported: false }),
    { type: 'done', final: 'I could not answer this.', verified: false },
  ])!
  assert.equal(s.verification!.verdict, 'UNSUPPORTED')
  assert.equal(s.verified, false)
  assert.equal(isSubstantiated(s), false)
})

test('K: an unrecognised verdict fails closed to UNSUPPORTED', () => {
  const s = fold([artifact('agent_verification',
    { verdict: 'PROBABLY_FINE', supported: true })])!
  assert.equal(s.verification!.verdict, 'UNSUPPORTED')
})

test('K: a missing `supported` flag is not treated as support', () => {
  const s = fold([artifact('agent_verification', { verdict: 'SUPPORTED' })])!
  assert.equal(s.verification!.supported, false)
  assert.equal(isSubstantiated(s), false)
})

// ── F. done.verified, and the disagreement rule ─────────────────────────────

test('F: done.verified is captured', () => {
  const s = fold([stage('plan', 'done'), { type: 'done', final: 'x', verified: true }])!
  assert.equal(s.verified, true)
})

test('F: if done.verified disagrees with the verdict, the cautious answer wins', () => {
  const s = fold([
    artifact('agent_verification', { verdict: 'SUPPORTED', label: 'S', supported: true }),
    { type: 'done', final: 'x', verified: false },
  ])!
  assert.equal(isSubstantiated(s), false,
    'a contradiction must not render as substantiated')
})

test('substantiation requires a verdict at all', () => {
  assert.equal(isSubstantiated(undefined), false)
  assert.equal(isSubstantiated(emptyAgentProcess()), false)
})

// ── A/N. existing behaviour is untouched ────────────────────────────────────

test('A/N: non-agent frames are passed through unchanged (identity preserved)', () => {
  const start = emptyAgentProcess()
  for (const f of [{ type: 'token', content: 'x' }, { type: 'thinking', message: 'm' },
                   { type: 'action', action: 'a' }, { type: 'sources', payload: {} },
                   { type: 'embeds', payload: {} }, { type: 'error', message: 'e' }])
    assert.equal(reduceAgentFrame(start, f), start,
      `frame ${f.type} must not alter agent state`)
})

test('A/N: garbage input never throws', () => {
  for (const f of [{}, { type: 'artifact' }, { type: 'artifact', artifact: null },
                   { type: 'stage' }, { type: 'artifact', artifact: { kind: 'unknown' } }])
    assert.doesNotThrow(() => reduceAgentFrame(undefined, f as never))
})

// ── full realistic stream ───────────────────────────────────────────────────

test('a complete agent turn folds to the expected state', () => {
  const s = fold([
    { type: 'start', session_id: 's', agent: 'agent' },
    stage('router', 'done'),
    stage('plan', 'running'), stage('plan', 'done', 12),
    stage('knowledge', 'done', 40), stage('draft', 'done', 900),
    stage('verify', 'done', 300), stage('finalize', 'done', 0),
    artifact('agent_plan', { goal: 'g', steps: [{ id: '1', agent: 'knowledge', task: 't' }] }),
    artifact('agent_evidence', { citations: [{ kind: 'document', source: 'p.pdf', text: 'x', where: 'document' }], counts: { total: 1 } }),
    artifact('agent_verification', { verdict: 'SUPPORTED', label: 'Supported by the evidence', explanation: 'e', missing: [], conflicts: [], supported: true }),
    { type: 'token', content: 'answer' },
    { type: 'done', final: 'answer', verified: true },
  ])!
  assert.ok(s.stages.every(x => x.status === 'done'), 'all five stages should be done')
  assert.ok(s.plan && s.evidence && s.verification)
  assert.equal(isSubstantiated(s), true)
})

console.log(`\n${passed} passed`)
