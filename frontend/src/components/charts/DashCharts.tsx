/* Interactive, dependency-free chart components for the prompt-to-chart Dashboard.
   Separate from Charts.tsx (which stays untouched). Styled with agenticAi's own neu
   tokens + accent palette — NOT ported from Hermes's theme system. Each supports the
   Hermes UX being replicated: hover emphasis, click-to-isolate (dim others), a
   cursor-following tooltip, staggered entrance, and x-label rotate/truncate. */
import { useEffect, useRef, useState, useMemo } from 'react'
import { motion, useMotionValue, useSpring } from 'framer-motion'

const EASE = [0.16, 1, 0.3, 1] as const

// agenticAi accent palette, cycled per series.
export const PALETTE = ['#00D4FF', '#7B2FFF', '#00FF88', '#38DBFF', '#FFB800', '#FF4466', '#A78BFA', '#22D3EE']
export const colorFor = (i: number) => PALETTE[i % PALETTE.length]

const nf = new Intl.NumberFormat('en-US')
export const fmt = (n: number) => (Number.isFinite(n) ? nf.format(Math.round(n * 100) / 100) : String(n))

export interface Row { x?: string | number; y?: number; value?: number; [k: string]: unknown }

// ── shared cursor tooltip ──────────────────────────────────────────────────────
function useTooltip() {
  const [tip, setTip] = useState<{ x: number; y: number; label: string; value: string } | null>(null)
  const node = tip && (
    <div
      className="pointer-events-none fixed z-50 px-2.5 py-1.5 rounded-lg text-xs"
      style={{
        left: tip.x + 14, top: tip.y + 14,
        background: 'var(--bg-elevated, #1B1F29)',
        border: '1px solid rgba(0,212,255,0.25)',
        color: 'var(--text-primary, #E6EBF5)',
        boxShadow: '0 8px 24px rgba(0,0,0,0.45)',
      }}
    >
      <div className="font-medium">{tip.label}</div>
      <div style={{ color: 'var(--cyan, #00D4FF)' }}>{tip.value}</div>
    </div>
  )
  return { tip, setTip, node }
}

// ── KPI (count-up) ──────────────────────────────────────────────────────────────
export function KpiTile({ value }: { value: number }) {
  const mv = useMotionValue(0)
  const spring = useSpring(mv, { stiffness: 90, damping: 20 })
  const ref = useRef<HTMLSpanElement>(null)
  useEffect(() => { mv.set(value) }, [value, mv])
  useEffect(() => {
    let last = NaN
    return spring.on('change', v => {
      const r = Math.round(v)
      if (r !== last && ref.current) { last = r; ref.current.textContent = nf.format(r) }
    })
  }, [spring])
  return (
    <div className="flex flex-col items-start justify-center h-[180px]">
      <span ref={ref} className="text-5xl font-bold tabular-nums" style={{ color: 'var(--cyan, #00D4FF)' }}>
        {nf.format(Math.round(value))}
      </span>
    </div>
  )
}

// ── vertical bar chart ──────────────────────────────────────────────────────────
export function BarChartI({ data }: { data: Row[] }) {
  const rows = useMemo(() => data.map((d, i) => ({
    label: String(d.x ?? ''), value: Number(d.y ?? 0), color: colorFor(i),
  })), [data])
  const max = Math.max(1, ...rows.map(r => r.value))
  const [active, setActive] = useState<number | null>(null)
  const { setTip, node } = useTooltip()

  // Rotate/truncate x-labels when there are many or they're long (the overlap fix).
  const maxLen = Math.max(0, ...rows.map(r => r.label.length))
  const rotate = rows.length > 8 || maxLen > 12
  const trunc = (s: string) => (s.length > 16 ? s.slice(0, 15) + '…' : s)

  if (!rows.length) return <div className="t-caption h-[200px] grid place-items-center">No data</div>

  return (
    <div className="select-none">
      {node}
      <div className="grid items-end gap-2 h-[190px]" style={{ gridTemplateColumns: `repeat(${rows.length}, minmax(0,1fr))` }}>
        {rows.map((r, i) => {
          const isDim = active !== null && active !== i
          return (
            <div key={r.label + i} className="flex flex-col items-center justify-end h-full">
              <motion.div
                initial={{ height: 0, opacity: 0.3 }}
                animate={{ height: `${(r.value / max) * 100}%`, opacity: isDim ? 0.35 : 1, scaleX: active === i ? 1.06 : 1 }}
                transition={{ delay: Math.min(i * 0.05, 0.4), duration: 0.5, ease: EASE }}
                onMouseEnter={e => setTip({ x: e.clientX, y: e.clientY, label: r.label, value: fmt(r.value) })}
                onMouseMove={e => setTip({ x: e.clientX, y: e.clientY, label: r.label, value: fmt(r.value) })}
                onMouseLeave={() => setTip(null)}
                onClick={() => setActive(a => (a === i ? null : i))}
                className="w-full rounded-t-md cursor-pointer"
                style={{ background: `linear-gradient(to top, ${r.color}66, ${r.color})`, minHeight: 2 }}
                title={r.label}
              />
            </div>
          )
        })}
      </div>
      <div className="grid gap-2 mt-2" style={{ gridTemplateColumns: `repeat(${rows.length}, minmax(0,1fr))` }}>
        {rows.map((r, i) => (
          <div key={r.label + i} className="flex justify-center" style={{ height: rotate ? 44 : 18 }}>
            <span
              className="t-caption truncate"
              title={r.label}
              style={rotate
                ? { transform: 'rotate(-35deg)', transformOrigin: 'top right', whiteSpace: 'nowrap', maxWidth: 90 }
                : { maxWidth: '100%' }}
            >
              {trunc(r.label)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── donut (pie) ─────────────────────────────────────────────────────────────────
export function DonutChartI({ data }: { data: Row[] }) {
  const rows = useMemo(() => data.map((d, i) => ({
    label: String(d.x ?? ''), value: Number(d.y ?? 0), color: colorFor(i),
  })), [data])
  const total = rows.reduce((s, r) => s + r.value, 0) || 1
  const [active, setActive] = useState<number | null>(null)
  const size = 170, thickness = 22, r = (size - thickness) / 2, c = size / 2, circ = 2 * Math.PI * r
  let offset = 0
  const focus = active !== null ? rows[active] : null

  if (!rows.length) return <div className="t-caption h-[200px] grid place-items-center">No data</div>

  return (
    <div className="flex items-center gap-5 select-none">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90 shrink-0">
        <circle cx={c} cy={c} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={thickness} />
        {rows.map((s, i) => {
          const len = (s.value / total) * circ
          const el = (
            <motion.circle
              key={s.label + i} cx={c} cy={c} r={r} fill="none" stroke={s.color}
              strokeWidth={active === i ? thickness + 6 : thickness}
              strokeDasharray={`${len} ${circ - len}`} strokeDashoffset={-offset}
              initial={{ opacity: 0.25 }} animate={{ opacity: active !== null && active !== i ? 0.4 : 1 }}
              transition={{ duration: 0.5, ease: EASE, delay: i * 0.08 }}
              className="cursor-pointer"
              onClick={() => setActive(a => (a === i ? null : i))}
            />
          )
          offset += len
          return el
        })}
      </svg>
      <div className="flex flex-col gap-1.5 min-w-0">
        <div className="t-title leading-none">
          {focus ? `${Math.round((focus.value / total) * 100)}%` : fmt(total)}
        </div>
        <div className="t-caption mb-1">{focus ? focus.label : 'total'}</div>
        {rows.map((s, i) => (
          <button
            key={s.label + i}
            onClick={() => setActive(a => (a === i ? null : i))}
            className="flex items-center gap-2 t-caption text-left"
            style={{ opacity: active !== null && active !== i ? 0.45 : 1 }}
          >
            <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ background: s.color }} />
            <span className="truncate" style={{ color: 'var(--text-secondary,#9AA7BD)' }} title={s.label}>{s.label}</span>
            <span className="ml-auto font-medium tabular-nums" style={{ color: 'var(--text-primary,#E6EBF5)' }}>{fmt(s.value)}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

// ── line chart ──────────────────────────────────────────────────────────────────
export function LineChartI({ data }: { data: Row[] }) {
  const rows = useMemo(() => data.map(d => ({ label: String(d.x ?? ''), value: Number(d.y ?? 0) })), [data])
  const { setTip, node } = useTooltip()
  if (rows.length < 2) return <BarChartI data={data} />
  const W = 480, H = 190, pad = 8
  const max = Math.max(1, ...rows.map(r => r.value)), min = Math.min(0, ...rows.map(r => r.value))
  const span = max - min || 1
  const px = (i: number) => pad + (i / (rows.length - 1)) * (W - 2 * pad)
  const py = (v: number) => H - pad - ((v - min) / span) * (H - 2 * pad)
  const d = rows.map((r, i) => `${i === 0 ? 'M' : 'L'} ${px(i).toFixed(1)} ${py(r.value).toFixed(1)}`).join(' ')
  const maxLen = Math.max(0, ...rows.map(r => r.label.length))
  const rotate = rows.length > 8 || maxLen > 12
  return (
    <div className="select-none">
      {node}
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" className="overflow-visible">
        <motion.path d={d} fill="none" stroke="var(--cyan,#00D4FF)" strokeWidth={2.5}
          initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.9, ease: EASE }} />
        {rows.map((r, i) => (
          <circle key={i} cx={px(i)} cy={py(r.value)} r={4} fill="var(--cyan,#00D4FF)" className="cursor-pointer"
            onMouseEnter={e => setTip({ x: e.clientX, y: e.clientY, label: r.label, value: fmt(r.value) })}
            onMouseMove={e => setTip({ x: e.clientX, y: e.clientY, label: r.label, value: fmt(r.value) })}
            onMouseLeave={() => setTip(null)} />
        ))}
      </svg>
      <div className="grid gap-1 mt-2" style={{ gridTemplateColumns: `repeat(${rows.length}, minmax(0,1fr))` }}>
        {rows.map((r, i) => (
          <div key={i} className="flex justify-center" style={{ height: rotate ? 40 : 16 }}>
            <span className="t-caption truncate" title={r.label}
              style={rotate ? { transform: 'rotate(-35deg)', transformOrigin: 'top right', whiteSpace: 'nowrap', maxWidth: 80 } : {}}>
              {r.label.length > 16 ? r.label.slice(0, 15) + '…' : r.label}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}
