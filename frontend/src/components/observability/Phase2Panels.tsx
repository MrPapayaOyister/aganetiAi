/**
 * Observability Phase 2 panels.
 *
 * Kept in their own module so ObservabilityPage stays a layout file and each
 * panel can be reviewed (or dropped) independently.
 *
 * Same two rules as Phase 1, and they are the reason several panels look
 * deliberately empty: everything is READ-ONLY, and a metric the deployment does
 * not produce renders as an explicit "not available" chip carrying the reason —
 * never as a zero, which would assert a measurement that was never made.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import {
  Boxes, Database, FileStack, GitBranch, Network, Search, TrendingUp, Workflow,
} from 'lucide-react'

// ── shared ──────────────────────────────────────────────────────────────────

const STATUS_STYLE: Record<string, string> = {
  healthy: 'text-emerald-300 bg-emerald-500/10 border-emerald-500/25',
  warning: 'text-amber-300 bg-amber-500/10 border-amber-500/25',
  offline: 'text-rose-300 bg-rose-500/10 border-rose-500/25',
  error: 'text-rose-300 bg-rose-500/10 border-rose-500/25',
  not_configured: 'text-slate-400 bg-slate-500/10 border-slate-500/25',
  unknown: 'text-slate-400 bg-slate-500/10 border-slate-500/25',
  strong: 'text-emerald-300 bg-emerald-500/10 border-emerald-500/25',
  corroborated: 'text-sky-300 bg-sky-500/10 border-sky-500/25',
  'single-source': 'text-amber-300 bg-amber-500/10 border-amber-500/25',
}

export function Pill({ status, children }: { status: string; children?: React.ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${STATUS_STYLE[status] ?? STATUS_STYLE.unknown}`}>
      {children ?? status}
    </span>
  )
}

export function Section({ icon: Icon, title, subtitle, right, children }: {
  icon: any; title: string; subtitle?: string
  right?: React.ReactNode; children: React.ReactNode
}) {
  return (
    <section className="neu rounded-2xl p-5">
      <header className="flex items-start justify-between gap-3 mb-4">
        <div className="flex items-start gap-2.5">
          <Icon className="w-4 h-4 mt-0.5 text-[#94A3B8] shrink-0" />
          <div>
            <h2 className="text-sm font-semibold text-[#E2E8F0]">{title}</h2>
            {subtitle && <p className="text-xs text-[#4A6080] mt-0.5">{subtitle}</p>}
          </div>
        </div>
        {right}
      </header>
      {children}
    </section>
  )
}

function usePoll<T>(url: string, ms: number, enabled = true) {
  const [data, setData] = useState<T | null>(null)
  const load = useCallback(async () => {
    if (!enabled) return
    try { setData((await axios.get(`/api${url}`)).data) } catch { /* panel shows empty */ }
  }, [url, enabled])
  useEffect(() => {
    load()
    if (!ms) return
    const t = setInterval(load, ms)
    return () => clearInterval(t)
  }, [load, ms])
  return { data, reload: load }
}

const ms = (v: any) => (typeof v === 'number' ? `${Math.round(v)} ms` : '—')
/** A value that may legitimately not exist. Renders the reason, never a fake 0.
 *  Defined here rather than imported from ObservabilityPage: that page imports
 *  THIS module, so importing back would be a cycle. */
function Metric({ label, value, unit, hint }: {
  label: string; value: unknown; unit?: string; hint?: string
}) {
  const missing =
    value === null || value === undefined ||
    (typeof value === 'object' && (value as any)?.status === 'unavailable')
  const detail = typeof value === 'object' ? (value as any)?.detail : undefined
  return (
    <div className="rounded-xl bg-white/[0.02] border border-white/5 px-3 py-2.5">
      <div className="text-[11px] uppercase tracking-wide text-[#4A6080]">{label}</div>
      {missing ? (
        <div className="mt-1">
          <Pill status="unknown">not available</Pill>
          {(detail || hint) && (
            <div className="mt-1 text-[10px] leading-snug text-[#4A6080]">{detail || hint}</div>
          )}
        </div>
      ) : (
        <>
          <div className="mt-0.5 text-lg font-semibold text-[#E2E8F0] tabular-nums">
            {typeof value === 'number' ? value.toLocaleString() : String(value)}
            {unit && <span className="ml-1 text-xs font-normal text-[#4A6080]">{unit}</span>}
          </div>
          {hint && <div className="mt-0.5 text-[10px] leading-snug text-[#4A6080]">{hint}</div>}
        </>
      )}
    </div>
  )
}

const Unavailable = ({ reason }: { reason?: string }) => (
  <div><Pill status="unknown">not available</Pill>
    {reason && <div className="mt-1 text-[10px] leading-snug text-[#4A6080]">{reason}</div>}</div>
)

// ── §1 pipeline timeline ────────────────────────────────────────────────────

export function PipelineTimeline() {
  const [q, setQ] = useState('How does Agentic AI use LiteLLM?')
  const [live, setLive] = useState(q)
  const [withAnswer, setWithAnswer] = useState(false)
  const url = `/observability/pipeline?question=${encodeURIComponent(live)}&with_answer=${withAnswer}`
  const { data, reload } = usePoll<any>(url, 0)

  const stages = data?.stages ?? []
  const max = Math.max(1, ...stages.map((s: any) => s.latency_ms ?? 0))

  return (
    <Section icon={Workflow} title="AI Pipeline Timeline"
      subtitle="One real request, timed stage by stage — every number measured on this run"
      right={<span className="text-[11px] text-[#4A6080]">{data ? `${Math.round(data.total_ms)} ms total` : ''}</span>}>
      <form className="flex flex-wrap gap-2 mb-4"
        onSubmit={(e) => { e.preventDefault(); setLive(q); setTimeout(reload, 0) }}>
        <input value={q} onChange={(e) => setQ(e.target.value)}
          className="flex-1 min-w-[16rem] rounded-xl bg-white/[0.04] border border-white/10 px-3 py-2 text-xs text-[#E2E8F0] placeholder-[#4A6080]"
          placeholder="Ask a question to trace…" />
        <label className="flex items-center gap-1.5 text-[11px] text-[#4A6080]">
          <input type="checkbox" checked={withAnswer} onChange={(e) => setWithAnswer(e.target.checked)} />
          generate answer
        </label>
        <button type="submit"
          className="rounded-xl bg-sky-500/15 border border-sky-500/30 px-3 py-2 text-xs text-sky-200 hover:bg-sky-500/25 transition-colors">
          Trace
        </button>
      </form>

      <ol className="space-y-1.5">
        {stages.map((s: any, i: number) => (
          <li key={s.stage} className="flex items-center gap-3">
            <span className="w-5 text-right text-[10px] tabular-nums text-[#4A6080]">{i + 1}</span>
            <span className="w-32 shrink-0 text-[11px] text-[#94A3B8]">{s.stage}</span>
            <div className="flex-1 h-2 rounded-full bg-white/5 overflow-hidden">
              {s.observed && (
                <div className={`h-full rounded-full ${s.ok ? 'bg-gradient-to-r from-sky-500/70 to-violet-500/70' : 'bg-rose-500/60'}`}
                  style={{ width: `${Math.max(2, ((s.latency_ms ?? 0) / max) * 100)}%` }} />
              )}
            </div>
            <span className="w-20 text-right text-[11px] tabular-nums text-[#94A3B8]">
              {s.observed ? ms(s.latency_ms) : <span className="text-[#4A6080]">skipped</span>}
            </span>
          </li>
        ))}
        {stages.length === 0 && <li className="text-xs text-[#4A6080]">Run a trace to see stage latencies.</li>}
      </ol>

      {data?.resolved_entities?.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="text-[10px] uppercase tracking-wide text-[#4A6080]">resolved</span>
          {data.resolved_entities.map((e: string) => (
            <span key={e} className="rounded-md bg-white/[0.03] border border-white/5 px-2 py-0.5 text-[11px] text-[#94A3B8]">{e}</span>
          ))}
        </div>
      )}
      {stages.find((s: any) => s.stage === 'Answer')?.detail?.text && (
        <p className="mt-3 rounded-xl bg-black/20 border border-white/5 p-3 text-[11px] leading-relaxed text-[#94A3B8]">
          {stages.find((s: any) => s.stage === 'Answer').detail.text}
        </p>
      )}
    </Section>
  )
}

// ── §2 knowledge graph explorer ─────────────────────────────────────────────

export function GraphExplorer() {
  const [term, setTerm] = useState('')
  const [results, setResults] = useState<any[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [entity, setEntity] = useState<any>(null)

  const search = useCallback(async (t: string) => {
    if (!t.trim()) { setResults([]); return }
    try {
      const r = await axios.get(`/api/observability/graph/search?q=${encodeURIComponent(t)}`)
      setResults(r.data.results ?? [])
    } catch { setResults([]) }
  }, [])

  useEffect(() => {
    if (!sel) return
    axios.get(`/api/observability/graph/entity/${encodeURIComponent(sel)}`)
      .then((r) => setEntity(r.data)).catch(() => setEntity(null))
  }, [sel])

  const c = entity?.corroboration ?? {}
  const p = entity?.provenance ?? {}

  return (
    <Section icon={Search} title="Knowledge Graph Explorer"
      subtitle="Search entities, inspect neighbours, provenance and corroboration — read-only">
      <form className="flex gap-2 mb-4" onSubmit={(e) => { e.preventDefault(); search(term) }}>
        <input value={term} onChange={(e) => setTerm(e.target.value)}
          className="flex-1 rounded-xl bg-white/[0.04] border border-white/10 px-3 py-2 text-xs text-[#E2E8F0] placeholder-[#4A6080]"
          placeholder="Search by name, id or alias…" />
        <button type="submit"
          className="rounded-xl bg-sky-500/15 border border-sky-500/30 px-3 py-2 text-xs text-sky-200 hover:bg-sky-500/25">
          Search
        </button>
      </form>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <div className="rounded-xl bg-white/[0.02] border border-white/5 max-h-72 overflow-y-auto divide-y divide-white/[0.04]">
          {results.length === 0 && <div className="p-3 text-[11px] text-[#4A6080]">No results yet.</div>}
          {results.map((r) => (
            <button key={r.id} onClick={() => setSel(r.id)}
              className={`w-full text-left px-3 py-2 hover:bg-white/[0.03] transition-colors ${sel === r.id ? 'bg-white/[0.04]' : ''}`}>
              <div className="text-[12px] text-[#E2E8F0] truncate">{r.name || r.id}</div>
              <div className="text-[10px] text-[#4A6080]">
                {r.labels?.join(' · ')} — degree {r.degree}, {r.sources} source{r.sources === 1 ? '' : 's'}
              </div>
            </button>
          ))}
        </div>

        <div className="lg:col-span-2 rounded-xl bg-white/[0.02] border border-white/5 p-3.5">
          {!entity || entity.status === 'not_found' ? (
            <div className="text-[11px] text-[#4A6080]">Select an entity to inspect it.</div>
          ) : (
            <>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-sm font-semibold text-[#E2E8F0] truncate">
                    {entity.properties?.canonical_name || entity.entity_id}
                  </div>
                  <div className="mt-0.5 text-[10px] text-[#4A6080]">
                    {entity.labels?.join(' · ')} · id {entity.entity_id}
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <Pill status={c.strength ?? 'unknown'}>{c.strength}</Pill>
                  {entity.neo4j_browser_url && (
                    <a href={entity.neo4j_browser_url} target="_blank" rel="noreferrer"
                      className="rounded-lg border border-white/10 px-2 py-1 text-[10px] text-[#94A3B8] hover:text-[#E2E8F0]">
                      Neo4j Browser ↗
                    </a>
                  )}
                </div>
              </div>

              <div className="mt-3 grid grid-cols-2 sm:grid-cols-4 gap-2">
                {[['Sources', c.distinct_sources], ['Observations', p.observations],
                  ['Confidence', p.confidence], ['Neighbours', entity.neighbours?.length]].map(([k, v]) => (
                  <div key={String(k)} className="rounded-lg bg-white/[0.02] border border-white/5 px-2.5 py-1.5">
                    <div className="text-[10px] uppercase tracking-wide text-[#4A6080]">{k}</div>
                    <div className="text-sm text-[#E2E8F0] tabular-nums">{v ?? '—'}</div>
                  </div>
                ))}
              </div>

              {entity.aliases?.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {entity.aliases.map((a: string) => (
                    <span key={a} className="rounded-md bg-white/[0.03] border border-white/5 px-2 py-0.5 text-[10px] text-[#94A3B8]">{a}</span>
                  ))}
                </div>
              )}

              <div className="mt-3 max-h-40 overflow-y-auto rounded-lg bg-black/20 border border-white/5 divide-y divide-white/[0.04]">
                {(entity.neighbours ?? []).map((n: any, i: number) => (
                  <div key={i} className="flex items-center gap-2 px-2.5 py-1.5 text-[11px]">
                    <span className={`w-6 text-center ${n.direction === 'out' ? 'text-sky-400' : 'text-violet-400'}`}>
                      {n.direction === 'out' ? '→' : '←'}
                    </span>
                    <span className="w-40 shrink-0 truncate text-[#4A6080]">{n.rel}</span>
                    <span className="flex-1 truncate text-[#94A3B8]">{n.name || n.id}</span>
                    <span className="text-[10px] text-[#4A6080]">{n.rel_sources} src</span>
                  </div>
                ))}
              </div>

              {p.source_ids?.length > 0 && (
                <div className="mt-3">
                  <div className="text-[10px] uppercase tracking-wide text-[#4A6080] mb-1">
                    Source documents ({p.source_ids.length})
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {p.source_ids.slice(0, 24).map((s: string) => (
                      <span key={s} className="rounded bg-white/[0.03] px-1.5 py-0.5 text-[10px] text-[#4A6080]">{s}</span>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </Section>
  )
}

// ── §3 + §12 growth / learning ──────────────────────────────────────────────

function Spark({ points, colour = '#38BDF8' }: { points: number[]; colour?: string }) {
  if (!points.length) return <div className="h-10 text-[10px] text-[#4A6080]">no data</div>
  const max = Math.max(...points, 1)
  const d = points.map((v, i) =>
    `${(i / Math.max(1, points.length - 1)) * 100},${30 - (v / max) * 28}`).join(' ')
  return (
    <svg viewBox="0 0 100 30" preserveAspectRatio="none" className="w-full h-10">
      <polyline points={d} fill="none" stroke={colour} strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
    </svg>
  )
}

export function GrowthPanel() {
  const { data } = usePoll<any>('/observability/graph/growth?days=30', 60000)
  const ing = data?.series?.by_ingestion_date ?? []
  const rel = data?.series?.relationships_by_ingestion_date ?? []
  const src = data?.series?.by_source_document_date ?? []
  return (
    <Section icon={TrendingUp} title="Graph Growth & Learning"
      subtitle="What the graph knows, and what it learned recently">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[['Nodes', data?.nodes], ['Relationships', data?.relationships],
          ['Avg degree', data?.average_degree], ['Corroborated', data?.corroborated_entities],
          ['New entities today', data?.new_entities_today],
          ['New relationships today', data?.new_relationships_today],
          ['New documents today', data?.new_documents_today],
          ['Corroboration %', data?.corroboration_pct]].map(([k, v]) => (
          <div key={String(k)} className="rounded-xl bg-white/[0.02] border border-white/5 px-3 py-2.5">
            <div className="text-[11px] uppercase tracking-wide text-[#4A6080]">{k}</div>
            <div className="mt-0.5 text-lg font-semibold text-[#E2E8F0] tabular-nums">
              {v ?? '—'}
            </div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-3">
        <div className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
          <div className="text-[10px] uppercase tracking-wide text-[#4A6080]">Entities by ingestion date</div>
          <Spark points={ing.map((p: any) => p.nodes)} />
        </div>
        <div className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
          <div className="text-[10px] uppercase tracking-wide text-[#4A6080]">Relationships by ingestion date</div>
          <Spark points={rel.map((p: any) => p.relationships)} colour="#A78BFA" />
        </div>
        <div className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
          <div className="text-[10px] uppercase tracking-wide text-[#4A6080]">Entities by source-document date</div>
          <Spark points={src.map((p: any) => p.entities)} colour="#34D399" />
        </div>
      </div>
      {data?.series_note && <p className="mt-2 text-[10px] text-[#4A6080]">{data.series_note}</p>}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mt-3">
        <div className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
          <div className="text-[10px] uppercase tracking-wide text-[#4A6080] mb-1.5">Top entities</div>
          {(data?.top_entities ?? []).map((e: any) => (
            <div key={e.id} className="flex justify-between py-0.5 text-[11px]">
              <span className="truncate text-[#94A3B8]">{e.name || e.id}</span>
              <span className="tabular-nums text-[#4A6080]">{e.degree}</span>
            </div>
          ))}
        </div>
        <div className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
          <div className="text-[10px] uppercase tracking-wide text-[#4A6080] mb-1.5">Recent graph activity</div>
          <div className="max-h-40 overflow-y-auto">
            {(data?.recent_activity ?? []).map((r: any, i: number) => (
              <div key={i} className="flex justify-between gap-2 py-0.5 text-[11px]">
                <span className="truncate text-[#94A3B8]">{r.name || r.id}</span>
                <span className="shrink-0 text-[10px] text-[#4A6080]">{String(r.at).slice(0, 16).replace('T', ' ')}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </Section>
  )
}

// ── §4 Qdrant explorer ──────────────────────────────────────────────────────

export function QdrantExplorer() {
  const { data } = usePoll<any>('/observability/qdrant', 60000)
  const cols = data?.collections ?? []
  return (
    <Section icon={Database} title="Qdrant Explorer"
      subtitle="Collections, embedding configuration and measured search latency">
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {[['Collections', data?.collection_count], ['Total points', data?.total_points],
          ['Embedding model', data?.embedding_model], ['Dimensions', data?.embedding_dim],
          ['Avg search', data?.search_latency?.avg_ms ? `${data.search_latency.avg_ms} ms` : null]]
          .map(([k, v]) => (
            <div key={String(k)} className="rounded-xl bg-white/[0.02] border border-white/5 px-3 py-2.5">
              <div className="text-[11px] uppercase tracking-wide text-[#4A6080]">{k}</div>
              <div className="mt-0.5 text-sm font-semibold text-[#E2E8F0] truncate">{v ?? '—'}</div>
            </div>
          ))}
      </div>
      <div className="mt-3 max-h-56 overflow-y-auto rounded-xl bg-black/20 border border-white/5 divide-y divide-white/[0.04]">
        {cols.map((c: any) => (
          <div key={c.name} className="flex items-center gap-3 px-3 py-1.5 text-[11px]">
            <span className="flex-1 truncate text-[#94A3B8]">{c.name}</span>
            <span className="tabular-nums text-[#4A6080]">{c.points?.toLocaleString() ?? '—'} pts</span>
          </div>
        ))}
      </div>
      <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
          <div className="text-[10px] uppercase tracking-wide text-[#4A6080] mb-1.5">Recent ingestions</div>
          {Array.isArray(data?.recent_ingestions) ? (
            data.recent_ingestions.slice(0, 8).map((r: any, i: number) => (
              <div key={i} className="flex justify-between gap-2 py-0.5 text-[11px]">
                <span className="truncate text-[#94A3B8]">{r.source}</span>
                <span className="shrink-0 text-[10px] text-[#4A6080]">{r.chunks} chunks</span>
              </div>
            ))
          ) : <Unavailable reason={data?.recent_ingestions?.detail} />}
        </div>
        <div className="rounded-xl bg-white/[0.02] border border-white/5 p-3 space-y-2">
          <div>
            <div className="text-[10px] uppercase tracking-wide text-[#4A6080]">Top searched collections</div>
            <Unavailable reason={data?.top_searched_collections?.detail} />
          </div>
          <div>
            <div className="text-[10px] uppercase tracking-wide text-[#4A6080]">Most retrieved documents</div>
            <Unavailable reason={data?.most_retrieved_documents?.detail} />
          </div>
        </div>
      </div>
    </Section>
  )
}

// ── §6 Microsoft providers ──────────────────────────────────────────────────

export function ProvidersPanel() {
  const { data } = usePoll<any>('/observability/providers', 60000)
  const conns = data?.connections ?? []
  return (
    <Section icon={Boxes} title="Provider Connections"
      subtitle="Microsoft and Google OAuth — expiry, scopes and live refresh state"
      right={data && <Pill status={data.refresh_failures > 0 ? 'warning' : 'healthy'}>
        {data.connected_count} connected · {data.refresh_failures} failing
      </Pill>}>
      {conns.length === 0 ? (
        <div className="text-xs text-[#4A6080]">No stored provider connections.</div>
      ) : (
        <div className="space-y-2">
          {conns.map((c: any, i: number) => (
            <div key={i} className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <div className="text-[12px] text-[#E2E8F0] truncate">
                    {c.email || c.user_id} <span className="text-[#4A6080]">· {c.provider}</span>
                  </div>
                  <div className="text-[10px] text-[#4A6080]">
                    expires {c.token_expiry ? new Date(c.token_expiry).toLocaleString() : '—'}
                    {typeof c.expires_in_minutes === 'number' && ` (${c.expires_in_minutes} min)`}
                  </div>
                </div>
                <Pill status={c.refresh_status === 'ok' ? 'healthy'
                  : c.refresh_status === 'unknown' ? 'unknown' : 'warning'}>
                  {c.refresh_status}
                </Pill>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                {['mail', 'calendar', 'contacts'].map((k) => (
                  <span key={k}
                    className={`rounded px-1.5 py-0.5 text-[10px] border ${c.capabilities?.[k]
                      ? 'text-emerald-300 border-emerald-500/25 bg-emerald-500/10'
                      : 'text-[#4A6080] border-white/5 bg-white/[0.02]'}`}>{k}</span>
                ))}
                <span className="text-[10px] text-[#4A6080]">{c.scopes?.length ?? 0} scopes</span>
                {!c.has_refresh_token && <Pill status="warning">no refresh token</Pill>}
              </div>
              {c.refresh_detail && <p className="mt-1.5 text-[10px] text-[#4A6080]">{c.refresh_detail}</p>}
            </div>
          ))}
        </div>
      )}
    </Section>
  )
}

// ── §10 topology ────────────────────────────────────────────────────────────

const TOPO_DOT: Record<string, string> = {
  healthy: 'bg-emerald-400', warning: 'bg-amber-400',
  offline: 'bg-rose-400', not_configured: 'bg-slate-500', unknown: 'bg-slate-500',
}

export function TopologyPanel() {
  const { data } = usePoll<any>('/observability/topology', 30000)
  const nodes = data?.nodes ?? []
  const tiers = useMemo(() => {
    const m = new Map<number, any[]>()
    nodes.forEach((n: any) => { m.set(n.tier, [...(m.get(n.tier) ?? []), n]) })
    return [...m.entries()].sort((a, b) => a[0] - b[0])
  }, [nodes])

  return (
    <Section icon={Network} title="Infrastructure Topology"
      subtitle="Declared call paths with live status per node"
      right={data && <div className="flex gap-1.5">
        {Object.entries(data.summary ?? {}).map(([k, v]) => <Pill key={k} status={k}>{`${v} ${k}`}</Pill>)}
      </div>}>
      <div className="space-y-3">
        {tiers.map(([tier, group]) => (
          <div key={tier}>
            <div className="text-[10px] uppercase tracking-wide text-[#4A6080] mb-1.5">
              {['Client', 'Application', 'Gateway', 'Backing services'][tier] ?? `Tier ${tier}`}
            </div>
            <div className="flex flex-wrap gap-2">
              {group.map((n: any) => (
                <div key={n.id}
                  className="flex items-center gap-2 rounded-xl bg-white/[0.02] border border-white/5 px-3 py-2">
                  <span className={`w-1.5 h-1.5 rounded-full ${TOPO_DOT[n.status] ?? TOPO_DOT.unknown}`} />
                  <span className="text-[12px] text-[#E2E8F0]">{n.label}</span>
                  {n.latency_ms != null && (
                    <span className="text-[10px] tabular-nums text-[#4A6080]">{Math.round(n.latency_ms)}ms</span>
                  )}
                  {n.version && <span className="text-[10px] text-[#4A6080]">{n.version}</span>}
                </div>
              ))}
            </div>
            {tier < tiers.length - 1 && <div className="mt-2 ml-3 h-3 w-px bg-white/10" />}
          </div>
        ))}
      </div>
      {data?.note && <p className="mt-3 text-[10px] text-[#4A6080]">{data.note}</p>}
    </Section>
  )
}

// ── §8 evaluation trend ─────────────────────────────────────────────────────

export function EvaluationTrend() {
  const { data } = usePoll<any>('/observability/evaluation/trend', 120000)
  const pts = data?.points ?? []
  const fams = ['entity_resolution', 'graph', 'fusion', 'ranking', 'answer']
  return (
    <Section icon={GitBranch} title="Evaluation Trend"
      subtitle="Family scores and regressions across every saved report"
      right={data?.regressions?.length > 0 && <Pill status="warning">{data.regressions.length} regressions</Pill>}>
      {pts.length === 0 ? <div className="text-xs text-[#4A6080]">No evaluation reports yet.</div> : (
        <>
          <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
            {fams.map((f) => (
              <div key={f} className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
                <div className="text-[10px] uppercase tracking-wide text-[#4A6080] truncate">{f.replace('_', ' ')}</div>
                <Spark points={pts.map((p: any) => (p.families?.[f] ?? 0) * 100)} />
                <div className="text-[11px] tabular-nums text-[#E2E8F0]">
                  {pts.at(-1)?.families?.[f] != null ? `${(pts.at(-1).families[f] * 100).toFixed(1)}%` : '—'}
                </div>
              </div>
            ))}
          </div>
          <div className="mt-3 rounded-xl bg-black/20 border border-white/5 divide-y divide-white/[0.04]">
            {pts.slice().reverse().slice(0, 6).map((p: any) => (
              <div key={p.file} className="flex items-center gap-3 px-3 py-1.5 text-[11px]">
                <span className="flex-1 truncate text-[#94A3B8]">{p.file}</span>
                <span className="tabular-nums text-[#E2E8F0]">{p.overall != null ? `${(p.overall * 100).toFixed(1)}%` : '—'}</span>
                <span className="w-20 text-right tabular-nums text-[#4A6080]">{p.failing}/{p.cases} fail</span>
                <span className="w-24 text-right tabular-nums text-[#4A6080]">{ms(p.latency?.total_ms)}</span>
              </div>
            ))}
          </div>
          {data?.note && <p className="mt-2 text-[10px] text-[#4A6080]">{data.note}</p>}
        </>
      )}
    </Section>
  )
}

// ── Document indexing (ingestion pipeline) ──────────────────────────────────

/**
 * Ingestion-pipeline state from /observability/documents.
 *
 * Every number is a PostgreSQL COUNT or MAX. There is no queue in this
 * architecture, so no queue depth, worker count or throughput is shown — and
 * this is deliberately not called a Redis queue, because Redis is not used.
 *
 * The four states use the backend's exact vocabulary. `stored` means the bytes
 * are safe in SeaweedFS, NOT that the document is searchable; showing them as
 * one number would tell an operator a document is findable when it is not.
 */
export function DocumentIndexingPanel() {
  const { data } = usePoll<any>('/observability/documents', 30000)

  // An unreachable endpoint must never render as zeros — "0 failed" and "we
  // cannot see the pipeline" are opposite claims.
  if (data && data.status !== 'ok') {
    return (
      <Section icon={FileStack} title="Document Indexing"
        subtitle="Ingestion pipeline — PostgreSQL-derived counts"
        right={<Pill status="unknown">unavailable</Pill>}>
        <div className="rounded-xl border border-dashed border-white/10 bg-white/[0.01] p-4">
          <Unavailable reason={data.detail || 'the statistics endpoint did not respond'} />
        </div>
      </Section>
    )
  }

  const c = data?.counts ?? {}
  const failed = Number(c.failed ?? 0)
  const stale = Boolean(data?.stale_processing)
  const fmtTime = (v: any) =>
    v ? new Date(String(v)).toLocaleString() : null

  const STATES: [string, keyof typeof c, string][] = [
    ['Stored', 'stored', 'bytes durable in SeaweedFS — not yet searchable'],
    ['Processing', 'processing', 'indexing running'],
    ['Indexed', 'indexed', 'searchable'],
    ['Failed', 'failed', 'indexing failed after the retry budget'],
  ]

  return (
    <Section icon={FileStack} title="Document Indexing"
      subtitle="Ingestion pipeline — PostgreSQL-derived counts, no queue in this architecture"
      right={
        <div className="flex items-center gap-1.5">
          {stale && <Pill status="warning">stale processing</Pill>}
          {failed > 0 && <Pill status="error">{failed} failed</Pill>}
          {!stale && failed === 0 && data && <Pill status="healthy">healthy</Pill>}
        </div>
      }>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {STATES.map(([label, key, hint]) => {
          const n = data ? Number(c[key] ?? 0) : null
          const warn = (key === 'failed' && (n ?? 0) > 0) ||
                       (key === 'processing' && stale)
          return (
            <div key={label}
              className={`rounded-xl border px-3 py-2.5 ${warn
                ? 'border-amber-500/25 bg-amber-500/[0.06]'
                : 'border-white/5 bg-white/[0.02]'}`}>
              <div className="text-[11px] uppercase tracking-wide text-[#4A6080]">{label}</div>
              <div className={`mt-0.5 text-lg font-semibold tabular-nums ${
                warn ? 'text-amber-300' : 'text-[#E2E8F0]'}`}>
                {n === null ? '—' : n.toLocaleString()}
              </div>
              <div className="mt-0.5 text-[10px] leading-snug text-[#4A6080]">{hint}</div>
            </div>
          )
        })}
      </div>

      {stale && (
        <div className="mt-3 rounded-xl border border-amber-500/25 bg-amber-500/[0.06] px-3 py-2.5">
          <div className="text-[12px] text-amber-300">
            A document has been <span className="font-medium">processing</span> for longer than{' '}
            {Math.round((data?.stale_processing_after_s ?? 900) / 60)} minutes
          </div>
          <div className="mt-0.5 text-[10px] leading-snug text-[#4A6080]">
            Its process most likely died mid-index. The recovery sweep re-drives it
            from PostgreSQL — no action is normally required, but repeated staleness
            means indexing is dying rather than failing.
          </div>
        </div>
      )}

      <div className="mt-3 grid grid-cols-2 md:grid-cols-4 gap-3">
        <Metric label="Avg index duration" value={data?.avg_index_duration_ms} unit="ms"
          hint="mean over documents that reached indexed" />
        <Metric label="Retry attempts" value={data?.retry_attempts}
          hint={`max ${data?.max_attempts ?? 3} attempts per document`} />
        <Metric label="Total documents" value={data?.total_documents} />
        <Metric label="Active in this process" value={data?.active_in_process?.length} />
      </div>

      <dl className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-2 text-[11px]">
        {[['Oldest processing', fmtTime(data?.oldest_processing)],
          ['Latest indexed', fmtTime(data?.latest_indexed)],
          ['Latest failure', fmtTime(data?.latest_failure)]].map(([k, v]) => (
          <div key={String(k)} className="rounded-xl bg-white/[0.02] border border-white/5 px-3 py-2">
            <dt className="text-[10px] uppercase tracking-wide text-[#4A6080]">{k}</dt>
            <dd className="mt-0.5 text-[#94A3B8]">{v ?? <span className="text-[#4A6080]">none</span>}</dd>
          </div>
        ))}
      </dl>

      {(data?.recent_failures ?? []).length > 0 && (
        <div className="mt-3 rounded-xl bg-black/20 border border-white/5 divide-y divide-white/[0.04]">
          {data.recent_failures.map((f: any, i: number) => (
            <div key={i} className="flex items-center gap-3 px-3 py-2 text-[11px]">
              <span className="w-40 shrink-0 truncate text-[#94A3B8]">{f.title}</span>
              <span className="w-40 shrink-0 text-rose-300/80">{f.category ?? 'unknown'}</span>
              <span className="flex-1 truncate text-[#4A6080]">{f.last_error}</span>
              <span className="shrink-0 text-[10px] text-[#4A6080]">
                {f.attempts}/{data?.max_attempts ?? 3}
              </span>
            </div>
          ))}
        </div>
      )}

      {data?.note && <p className="mt-2 text-[10px] text-[#4A6080]">{data.note}</p>}
    </Section>
  )
}
