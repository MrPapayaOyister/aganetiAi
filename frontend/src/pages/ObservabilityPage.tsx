/**
 * Observability — the engineering/admin dashboard.
 *
 * Deliberately separate from AnalyticsPage, which stays a business/user view.
 * This page answers "is the platform healthy", not "how is the business doing",
 * and the two audiences want opposite defaults: this one surfaces failures first.
 *
 * READ-ONLY. Every panel GETs from /api/observability/*; nothing here mutates.
 *
 * Honesty rule, mirrored from the backend: a metric the deployment does not
 * actually produce renders as an explicit "not available" chip with the reason,
 * never as 0 or a dash. A zero reads as "measured, and it's zero", which is a
 * different and much worse claim than "we cannot see this".
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import axios from 'axios'
import {
  PipelineTimeline, GraphExplorer, GrowthPanel, QdrantExplorer,
  ProvidersPanel, TopologyPanel, EvaluationTrend, DocumentIndexingPanel,
} from '../components/observability/Phase2Panels'
import {
  Activity, AlertTriangle, Cpu, GitBranch, HardDrive,
  Layers, RefreshCw, Server, Share2, Terminal, Zap,
} from 'lucide-react'

// ── shared primitives ───────────────────────────────────────────────────────

type Status = 'healthy' | 'warning' | 'error' | 'not_configured' | 'unknown' | string

const STATUS_STYLE: Record<string, string> = {
  healthy:        'text-emerald-300 bg-emerald-500/10 border-emerald-500/25',
  ok:             'text-emerald-300 bg-emerald-500/10 border-emerald-500/25',
  warning:        'text-amber-300  bg-amber-500/10  border-amber-500/25',
  error:          'text-rose-300   bg-rose-500/10   border-rose-500/25',
  critical:       'text-rose-300   bg-rose-500/10   border-rose-500/25',
  not_configured: 'text-slate-400  bg-slate-500/10  border-slate-500/25',
  unavailable:    'text-slate-400  bg-slate-500/10  border-slate-500/25',
  unknown:        'text-slate-400  bg-slate-500/10  border-slate-500/25',
  info:           'text-sky-300    bg-sky-500/10    border-sky-500/25',
}

function Pill({ status, children }: { status: Status; children?: React.ReactNode }) {
  const cls = STATUS_STYLE[status] ?? STATUS_STYLE.unknown
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {children ?? status}
    </span>
  )
}

/** A value that may legitimately not exist. Renders the reason, never a fake 0. */
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
          <Pill status="unavailable">not available</Pill>
          {(detail || hint) && (
            <div className="mt-1 text-[10px] leading-snug text-[#4A6080]">{detail || hint}</div>
          )}
        </div>
      ) : (
        <div className="mt-0.5 text-lg font-semibold text-[#E2E8F0] tabular-nums">
          {typeof value === 'number' ? value.toLocaleString() : String(value)}
          {unit && <span className="ml-1 text-xs font-normal text-[#4A6080]">{unit}</span>}
        </div>
      )}
    </div>
  )
}

function Section({ icon: Icon, title, subtitle, children, right }: {
  icon: any; title: string; subtitle?: string
  children: React.ReactNode; right?: React.ReactNode
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

/** Polling hook. Every panel owns its own cadence so a slow probe (the context
 *  builder runs a real retrieval) never blocks a fast one (system metrics). */
function usePoll<T>(url: string, ms: number) {
  const [data, setData] = useState<T | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const alive = useRef(true)

  const load = useCallback(async () => {
    try {
      const r = await axios.get(`/api${url}`)
      if (alive.current) { setData(r.data); setErr(null) }
    } catch (e: any) {
      if (alive.current) setErr(e?.message ?? 'request failed')
    } finally {
      if (alive.current) setLoading(false)
    }
  }, [url])

  useEffect(() => {
    alive.current = true
    load()
    const t = setInterval(load, ms)
    return () => { alive.current = false; clearInterval(t) }
  }, [load, ms])

  return { data, err, loading, reload: load }
}

const fmtMs = (v: any) => (typeof v === 'number' ? `${Math.round(v)} ms` : '—')

// ── §1 Infrastructure ───────────────────────────────────────────────────────

function Infrastructure() {
  const { data, loading } = usePoll<any>('/observability/infrastructure', 20000)
  const services = data?.services ?? []
  return (
    <Section icon={Server} title="Infrastructure Health"
      subtitle="Live probes — latency, version and connection state per dependency"
      right={<span className="text-[11px] text-[#4A6080]">{loading ? 'probing…' : `${services.length} services`}</span>}>
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-3">
        {services.map((s: any) => (
          <div key={s.name} className="rounded-xl bg-white/[0.02] border border-white/5 p-3.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium text-[#E2E8F0] truncate">{s.name}</span>
              <Pill status={s.status} />
            </div>
            <dl className="mt-2.5 space-y-1 text-[11px]">
              <div className="flex justify-between"><dt className="text-[#4A6080]">Latency</dt>
                <dd className="text-[#94A3B8] tabular-nums">{fmtMs(s.latency_ms)}</dd></div>
              <div className="flex justify-between"><dt className="text-[#4A6080]">Connection</dt>
                <dd className="text-[#94A3B8] truncate max-w-[60%]">{s.connection ?? '—'}</dd></div>
              {s.version && <div className="flex justify-between"><dt className="text-[#4A6080]">Version</dt>
                <dd className="text-[#94A3B8]">{s.version}</dd></div>}
              <div className="flex justify-between"><dt className="text-[#4A6080]">Checked</dt>
                <dd className="text-[#94A3B8]">{s.last_check ? new Date(s.last_check).toLocaleTimeString() : '—'}</dd></div>
            </dl>
            {s.detail && <p className="mt-2 text-[10px] leading-snug text-[#4A6080]">{s.detail}</p>}
          </div>
        ))}
      </div>
    </Section>
  )
}

// ── §2 LLM ──────────────────────────────────────────────────────────────────

function LLMPanel() {
  const { data } = usePoll<any>('/observability/llm', 30000)
  return (
    <Section icon={Zap} title="LLM Monitoring"
      subtitle="Gateway routing and model configuration"
      right={data?.gateway && <Pill status={data.gateway.status === 'healthy' ? 'healthy' : 'error'} />}>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Metric label="Chat model" value={data?.chat_model} />
        <Metric label="Extraction model" value={data?.extraction_model} />
        <Metric label="Extraction timeout" value={data?.extraction_timeout_s} unit="s" />
        <Metric label="Gateway routes" value={data?.gateway?.routes?.length} />
        <Metric label="Requests / min" value={data?.requests_per_min} />
        <Metric label="Avg latency" value={data?.avg_latency_ms} unit="ms" />
        <Metric label="P95 latency" value={data?.p95_latency_ms} unit="ms" />
        <Metric label="Token throughput" value={data?.token_throughput} />
        <Metric label="Active streams" value={data?.active_streams} />
        <Metric label="Queue depth" value={data?.queue_depth} />
        <Metric label="Timeouts" value={data?.timeout_count} />
        <Metric label="Errors / retries" value={data?.error_count} />
      </div>
      {data?.gateway?.routes?.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {data.gateway.routes.map((r: string) => (
            <span key={r} className="rounded-md bg-white/[0.03] border border-white/5 px-2 py-0.5 text-[11px] text-[#94A3B8]">{r}</span>
          ))}
        </div>
      )}
    </Section>
  )
}

// ── §3 Knowledge graph ──────────────────────────────────────────────────────

function GraphPreview() {
  const { data } = usePoll<any>('/observability/graph/preview?limit=40', 60000)
  const nodes = data?.nodes ?? []
  const edges = data?.edges ?? []

  // Deterministic circular layout — no physics engine, no extra dependency, and
  // it stays stable between polls so the preview does not jitter on refresh.
  const pos = useMemo(() => {
    const m = new Map<string, { x: number; y: number }>()
    nodes.forEach((n: any, i: number) => {
      const a = (i / Math.max(1, nodes.length)) * Math.PI * 2
      const r = 42 + (i % 3) * 6
      m.set(n.id, { x: 50 + r * Math.cos(a), y: 50 + r * Math.sin(a) })
    })
    return m
  }, [nodes])

  if (!nodes.length) return <div className="text-xs text-[#4A6080]">No preview data.</div>
  return (
    <div className="rounded-xl bg-white/[0.02] border border-white/5 p-2">
      <svg viewBox="0 0 100 100" className="w-full h-56" role="img" aria-label="Knowledge graph preview">
        {edges.map((e: any, i: number) => {
          const a = pos.get(e.source), b = pos.get(e.target)
          if (!a || !b) return null
          return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
            stroke="rgba(148,163,184,0.16)" strokeWidth={0.18} />
        })}
        {nodes.map((n: any) => {
          const p = pos.get(n.id)!
          return (
            <g key={n.id}>
              <circle cx={p.x} cy={p.y} r={0.9} fill="#38BDF8" opacity={0.85}>
                <title>{`${n.id} · ${n.label}`}</title>
              </circle>
            </g>
          )
        })}
      </svg>
      <p className="px-1 pb-1 text-[10px] text-[#4A6080]">
        {nodes.length} nodes · {edges.length} edges — seeded from the highest-degree entities. Hover a node for its id.
      </p>
    </div>
  )
}

function GraphPanel() {
  const { data } = usePoll<any>('/observability/graph', 30000)
  const types = Object.entries(data?.relationship_types ?? {}).slice(0, 8) as [string, number][]
  const max = Math.max(1, ...types.map(([, c]) => c))
  return (
    <Section icon={Share2} title="Knowledge Graph"
      subtitle="Entities, relationships and the retrieval parameters in force">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Metric label="Nodes" value={data?.nodes} />
        <Metric label="Relationships" value={data?.relationships} />
        <Metric label="Labels" value={data?.label_count} />
        <Metric label="Relationship types" value={data?.relationship_type_count} />
        <Metric label="Average degree" value={data?.average_degree} />
        <Metric label="Corroborated" value={data?.corroborated_entities} />
        <Metric label="Alias registry" value={data?.alias_registry_size} />
        <Metric label="Retrieval latency" value={data?.retrieval_latency_ms} unit="ms" />
        <Metric label="Graph depth" value={data?.graph_depth} />
        <Metric label="Fuzzy threshold" value={data?.fuzzy_threshold} />
        <Metric label="Entities resolved (probe)" value={data?.resolution_probe?.entities_resolved} />
        <Metric label="Strongly corroborated" value={data?.strongly_corroborated} />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mt-3">
        <div className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
          <div className="text-[11px] uppercase tracking-wide text-[#4A6080] mb-2">Top relationship types</div>
          <div className="space-y-1.5">
            {types.map(([t, c]) => (
              <div key={t} className="flex items-center gap-2">
                <span className="w-32 shrink-0 truncate text-[11px] text-[#94A3B8]">{t}</span>
                <div className="flex-1 h-1.5 rounded-full bg-white/5 overflow-hidden">
                  <div className="h-full rounded-full bg-sky-400/60" style={{ width: `${(c / max) * 100}%` }} />
                </div>
                <span className="w-10 text-right text-[11px] tabular-nums text-[#4A6080]">{c}</span>
              </div>
            ))}
          </div>
        </div>
        <GraphPreview />
      </div>
    </Section>
  )
}

// ── §4 Context engine ───────────────────────────────────────────────────────

function ContextPanel() {
  const { data } = usePoll<any>('/observability/context', 45000)
  const p = data?.probe ?? {}
  const byProv: Record<string, number> = p.items_by_provider ?? {}
  return (
    <Section icon={Layers} title="Hybrid Context Engine"
      subtitle="Measured by a live context build against a fixed probe question">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Metric label="Total build" value={p.total_ms} unit="ms" />
        <Metric label="Context items" value={p.context_items} />
        <Metric label="Fusion (merged)" value={p.merged ?? p.duplicates_merged} />
        <Metric label="Compression ratio" value={p.compression_ratio} />
        <Metric label="Budget usage" value={p.budget_utilization} />
        <Metric label="Prompt tokens" value={p.tokens_used ?? p.avg_tokens} />
        <Metric label="Fusion time" value={p.fusion_ms} unit="ms" />
        <Metric label="Ranking time" value={p.ranking_ms} unit="ms" />
      </div>
      <div className="mt-3 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2">
        {(data?.providers ?? []).map((pr: any) => {
          const n = byProv[pr.name]
          const contributed = typeof n === 'number' && n > 0
          return (
            <div key={pr.name} className="rounded-xl bg-white/[0.02] border border-white/5 px-3 py-2">
              <div className="text-[11px] text-[#94A3B8] capitalize truncate">{pr.name}</div>
              <div className="mt-1">
                <Pill status={contributed ? 'healthy' : 'unknown'}>
                  {contributed ? `${n} items` : 'no items'}
                </Pill>
              </div>
            </div>
          )
        })}
      </div>
    </Section>
  )
}

// ── §5 Storage ──────────────────────────────────────────────────────────────

function StoragePanel() {
  const { data } = usePoll<any>('/observability/storage', 60000)
  const notConfigured = data?.status === 'not_configured'
  return (
    <Section icon={HardDrive} title="SeaweedFS"
      subtitle="Object storage tier"
      right={data && <Pill status={data.status} />}>
      {notConfigured ? (
        <div className="rounded-xl border border-dashed border-white/10 bg-white/[0.01] p-5 text-center">
          <p className="text-sm text-[#94A3B8]">Not deployed yet</p>
          <p className="mt-1 text-xs text-[#4A6080] max-w-lg mx-auto">{data.detail}</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <Metric label="Master" value={data?.master?.leader} />
          <Metric label="Volume servers" value={data?.volume_servers} />
          <Metric label="Filer" value={data?.filer} />
          <Metric label="Collections" value={data?.collections} />
          <Metric label="Buckets" value={data?.buckets} />
          <Metric label="Objects" value={data?.objects} />
          <Metric label="Total storage" value={data?.total_storage} />
          <Metric label="Used" value={data?.used_storage} />
          <Metric label="Free" value={data?.free_storage} />
          <Metric label="Upload rate" value={data?.upload_rate} />
          <Metric label="Download rate" value={data?.download_rate} />
          <Metric label="Replication" value={data?.replication} />
        </div>
      )}
    </Section>
  )
}

// ── §6 Evaluation ───────────────────────────────────────────────────────────

function EvaluationPanel() {
  const { data } = usePoll<any>('/observability/evaluation', 60000)
  const [sel, setSel] = useState<string | null>(null)
  const history = data?.history ?? []
  const current = sel ? history.find((h: any) => h.file === sel) : history[0]
  const fam = current?.families ?? {}
  const base = data?.baseline?.families ?? {}
  return (
    <Section icon={GitBranch} title="Evaluation"
      subtitle="Regression baseline and historical reports"
      right={history.length > 1 && (
        <select value={sel ?? history[0]?.file} onChange={(e) => setSel(e.target.value)}
          className="rounded-lg bg-white/[0.04] border border-white/10 px-2 py-1 text-[11px] text-[#94A3B8]">
          {history.map((h: any) => <option key={h.file} value={h.file}>{h.file}</option>)}
        </select>
      )}>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Metric label="Overall" value={current?.overall != null ? `${(current.overall * 100).toFixed(1)}%` : null} />
        <Metric label="Baseline overall" value={data?.baseline?.overall != null ? `${(data.baseline.overall * 100).toFixed(1)}%` : null} />
        <Metric label="Cases" value={current?.cases} />
        <Metric label="Failing" value={current?.failing} />
      </div>
      <div className="mt-3 grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-2">
        {['entity_resolution', 'graph', 'qdrant', 'fusion', 'ranking', 'answer'].map((k) => {
          const v = fam[k], b = base[k]
          const delta = v != null && b != null ? (v - b) * 100 : null
          return (
            <div key={k} className="rounded-xl bg-white/[0.02] border border-white/5 px-3 py-2">
              <div className="text-[10px] uppercase tracking-wide text-[#4A6080] truncate">{k.replace('_', ' ')}</div>
              <div className="mt-0.5 text-base font-semibold text-[#E2E8F0] tabular-nums">
                {v != null ? `${(v * 100).toFixed(1)}%` : '—'}
              </div>
              {delta != null && (
                <div className={`text-[10px] tabular-nums ${delta >= 0 ? 'text-emerald-400/80' : 'text-rose-400/80'}`}>
                  {delta >= 0 ? '+' : ''}{delta.toFixed(1)}pp vs baseline
                </div>
              )}
            </div>
          )
        })}
      </div>
      {current?.modified && (
        <p className="mt-3 text-[11px] text-[#4A6080]">
          Last evaluation {new Date(current.modified).toLocaleString()}
          {current.took_s ? ` · ran in ${Math.round(current.took_s)}s` : ''}
        </p>
      )}
    </Section>
  )
}

// ── §7 Live activity ────────────────────────────────────────────────────────

const EVENT_COLOUR: Record<string, string> = {
  entity_resolution: 'text-sky-300',
  graph_retrieval: 'text-violet-300',
  context_fusion: 'text-emerald-300',
  extraction: 'text-amber-300',
  llm_error: 'text-rose-300',
}

function ActivityPanel() {
  const { data } = usePoll<any>('/observability/activity?limit=80', 10000)
  const events = data?.events ?? []
  return (
    <Section icon={Activity} title="Live Activity"
      subtitle="Pipeline events parsed from the structured application logs"
      right={<span className="text-[11px] text-[#4A6080]">{events.length} events</span>}>
      <div className="max-h-80 overflow-y-auto rounded-xl bg-black/20 border border-white/5 divide-y divide-white/[0.04]">
        {events.length === 0 && <div className="p-4 text-xs text-[#4A6080]">No recent events in the logs.</div>}
        {events.map((e: any, i: number) => (
          <div key={i} className="flex items-center gap-3 px-3 py-2 text-[11px]">
            <span className="w-16 shrink-0 tabular-nums text-[#4A6080]">
              {e.timestamp ? String(e.timestamp).slice(11, 19) : '—'}
            </span>
            <span className={`w-36 shrink-0 font-medium ${EVENT_COLOUR[e.type] ?? 'text-[#94A3B8]'}`}>
              {e.type.replace(/_/g, ' ')}
            </span>
            <span className="flex-1 truncate text-[#4A6080]">
              {Object.entries(e.detail ?? {}).slice(0, 3).map(([k, v]) => `${k}=${v}`).join('  ')}
            </span>
            <span className="w-16 shrink-0 text-right tabular-nums text-[#94A3B8]">{fmtMs(e.latency_ms)}</span>
          </div>
        ))}
      </div>
      {data?.note && <p className="mt-2 text-[10px] text-[#4A6080]">{data.note}</p>}
    </Section>
  )
}

// ── §8 Agent trace ──────────────────────────────────────────────────────────

const PIPELINE = ['User', 'Planner', 'Memory', 'Qdrant', 'Neo4j', 'Calendar',
  'Fusion', 'Prompt', 'LiteLLM', 'Final Answer']

function TracePanel() {
  const { data: sessions } = usePoll<any>('/observability/sessions?limit=25', 60000)
  const [sid, setSid] = useState<string | null>(null)
  const [trace, setTrace] = useState<any>(null)
  const list = sessions?.sessions ?? []

  useEffect(() => {
    if (!sid && list.length) setSid(list[0].session_id)
  }, [list, sid])

  useEffect(() => {
    if (!sid) return
    let alive = true
    axios.get(`/api/observability/trace/${encodeURIComponent(sid)}`)
      .then((r) => { if (alive) setTrace(r.data) })
      .catch(() => { if (alive) setTrace(null) })
    return () => { alive = false }
  }, [sid])

  const stages = trace?.stages ?? PIPELINE.map((s) => ({ stage: s, latency_ms: null, observed: false }))
  const maxLat = Math.max(1, ...stages.map((s: any) => s.latency_ms ?? 0))

  return (
    <Section icon={Terminal} title="Agent Trace"
      subtitle="Execution pipeline for one conversation"
      right={list.length > 0 && (
        <select value={sid ?? ''} onChange={(e) => setSid(e.target.value)}
          className="max-w-[16rem] rounded-lg bg-white/[0.04] border border-white/10 px-2 py-1 text-[11px] text-[#94A3B8]">
          {list.map((s: any) => (
            <option key={s.session_id} value={s.session_id}>
              {s.session_id.slice(0, 20)} · {s.messages} msgs
            </option>
          ))}
        </select>
      )}>
      {list.length === 0 ? (
        <div className="text-xs text-[#4A6080]">No conversations recorded yet.</div>
      ) : (
        <>
          <ol className="space-y-1.5">
            {stages.map((s: any, i: number) => (
              <li key={s.stage} className="flex items-center gap-3">
                <span className="w-6 shrink-0 text-right text-[10px] tabular-nums text-[#4A6080]">{i + 1}</span>
                <span className="w-28 shrink-0 text-[11px] text-[#94A3B8]">{s.stage}</span>
                <div className="flex-1 h-2 rounded-full bg-white/5 overflow-hidden">
                  {s.observed && (
                    <div className="h-full rounded-full bg-gradient-to-r from-sky-500/70 to-violet-500/70"
                      style={{ width: `${Math.max(3, ((s.latency_ms ?? 0) / maxLat) * 100)}%` }} />
                  )}
                </div>
                <span className="w-20 shrink-0 text-right text-[11px] tabular-nums text-[#94A3B8]">
                  {s.observed ? fmtMs(s.latency_ms) : <span className="text-[#4A6080]">not measured</span>}
                </span>
              </li>
            ))}
          </ol>
          {trace?.note && <p className="mt-3 text-[10px] leading-snug text-[#4A6080]">{trace.note}</p>}
        </>
      )}
    </Section>
  )
}

// ── §9 System ───────────────────────────────────────────────────────────────

function SystemPanel() {
  const { data } = usePoll<any>('/observability/system', 10000)
  const gpus = Array.isArray(data?.gpu) ? data.gpu : []
  return (
    <Section icon={Cpu} title="System Metrics" subtitle="Host and accelerator utilisation">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Metric label="CPU" value={data?.cpu_percent} unit="%" />
        <Metric label="RAM used" value={data?.ram?.percent ?? data?.ram?.used_mb} unit={data?.ram?.percent ? '%' : 'MB'} />
        <Metric label="Disk used" value={data?.disk?.percent ?? data?.disk?.used_gb} unit={data?.disk?.percent ? '%' : 'GB'} />
        <Metric label="Load average" value={data?.load_avg?.map((n: number) => n.toFixed(2)).join(' ')} />
      </div>
      <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-3">
        {gpus.map((g: any, i: number) => (
          <div key={i} className="rounded-xl bg-white/[0.02] border border-white/5 p-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium text-[#E2E8F0]">{g.name}</span>
              <Pill status={(g.temperature_c ?? 0) > 85 ? 'warning' : 'healthy'}>
                {g.temperature_c != null ? `${g.temperature_c}°C` : 'temp n/a'}
              </Pill>
            </div>
            <div className="mt-2.5">
              <div className="flex justify-between text-[11px] text-[#4A6080]">
                <span>GPU utilisation</span><span className="tabular-nums">{g.utilization_pct ?? '—'}%</span>
              </div>
              <div className="mt-1 h-2 rounded-full bg-white/5 overflow-hidden">
                <div className="h-full rounded-full bg-gradient-to-r from-emerald-500/70 to-amber-500/70"
                  style={{ width: `${g.utilization_pct ?? 0}%` }} />
              </div>
            </div>
            <div className="mt-2 text-[11px] text-[#4A6080]">
              {g.vram_total_mb != null
                ? <>VRAM {Math.round(g.vram_used_mb)} / {Math.round(g.vram_total_mb)} MB</>
                : <span>{g.vram_note ?? 'VRAM not reported'}</span>}
            </div>
          </div>
        ))}
        {gpus.length === 0 && (
          <div className="rounded-xl bg-white/[0.02] border border-white/5 p-3 text-xs text-[#4A6080]">
            {data?.gpu?.detail ?? 'No GPU telemetry available.'}
          </div>
        )}
      </div>
      {data?.network && (
        <p className="mt-3 text-[10px] text-[#4A6080]">
          {typeof data.network === 'object' && 'bytes_recv' in data.network
            ? `Network since boot — ${(data.network.bytes_recv / 1e9).toFixed(2)} GB in / ${(data.network.bytes_sent / 1e9).toFixed(2)} GB out. ${data.network.note}`
            : data.network.detail}
        </p>
      )}
    </Section>
  )
}

// ── §10 Alerts ──────────────────────────────────────────────────────────────

function AlertsPanel() {
  const { data } = usePoll<any>('/observability/alerts', 20000)
  const alerts = data?.alerts ?? []
  const critical = alerts.filter((a: any) => a.severity === 'critical')
  return (
    <Section icon={AlertTriangle} title="Alerts"
      subtitle="Derived from live probes — nothing is scheduled or remembered"
      right={<div className="flex gap-1.5">
        {Object.entries(data?.by_severity ?? {}).map(([sev, n]) => (
          <Pill key={sev} status={sev}>{`${n} ${sev}`}</Pill>
        ))}
      </div>}>
      {alerts.length === 0 ? (
        <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/[0.04] p-4 text-sm text-emerald-300">
          No active alerts.
        </div>
      ) : (
        <ul className="space-y-2">
          {[...critical, ...alerts.filter((a: any) => a.severity !== 'critical')].map((a: any, i: number) => (
            <li key={i} className="flex items-start gap-3 rounded-xl bg-white/[0.02] border border-white/5 px-3 py-2.5">
              <Pill status={a.severity} />
              <div className="min-w-0 flex-1">
                <div className="text-[12px] text-[#E2E8F0]">{a.message}</div>
                {a.detail && <div className="mt-0.5 text-[10px] leading-snug text-[#4A6080]">{a.detail}</div>}
              </div>
              <span className="shrink-0 text-[10px] text-[#4A6080]">{a.type}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

// ── page ────────────────────────────────────────────────────────────────────

export default function ObservabilityPage() {
  const { data: summary, reload } = usePoll<any>('/observability/summary', 20000)
  const [spin, setSpin] = useState(false)

  const refreshAll = () => {
    setSpin(true)
    reload().finally(() => setTimeout(() => setSpin(false), 600))
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-7xl mx-auto px-4 py-6 space-y-5 pb-24 md:pb-6">
        <header className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-lg font-semibold text-[#E2E8F0]">Observability</h1>
            <p className="text-sm text-[#4A6080] mt-0.5">
              Engineering view — infrastructure, retrieval and model telemetry. Read-only.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {summary && (
              <Pill status={(summary.alerts?.critical ?? 0) > 0 ? 'critical' : 'healthy'}>
                {summary.healthy}/{summary.total_services} healthy
              </Pill>
            )}
            <button onClick={refreshAll}
              className="neu rounded-xl p-2 text-[#94A3B8] hover:text-[#E2E8F0] transition-colors"
              aria-label="Refresh all panels">
              <RefreshCw className={`w-4 h-4 ${spin ? 'animate-spin' : ''}`} />
            </button>
          </div>
        </header>

        <AlertsPanel />
        <TopologyPanel />
        <Infrastructure />
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
          <LLMPanel />
          <SystemPanel />
        </div>
        <PipelineTimeline />
        <GraphPanel />
        <ContextPanel />
        <GraphExplorer />
        <GrowthPanel />
        <DocumentIndexingPanel />
        <QdrantExplorer />
        <ProvidersPanel />
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
          <EvaluationPanel />
          <StoragePanel />
        </div>
        <EvaluationTrend />
        <ActivityPanel />
        <TracePanel />
      </div>
    </div>
  )
}
