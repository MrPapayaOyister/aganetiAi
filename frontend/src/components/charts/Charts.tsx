import { motion } from 'framer-motion'

/* Dependency-free SVG charts, neumorphic styling. */

export interface Segment { label: string; value: number; color: string }

export function DonutChart({ segments, size = 140, thickness = 16, centerLabel, centerValue }: {
  segments: Segment[]; size?: number; thickness?: number; centerLabel?: string; centerValue?: string
}) {
  const total = segments.reduce((s, x) => s + x.value, 0) || 1
  const r = (size - thickness) / 2
  const c = size / 2
  const circ = 2 * Math.PI * r
  let offset = 0

  return (
    <div className="flex items-center gap-4">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90">
        <circle cx={c} cy={c} r={r} fill="none" stroke="rgba(0,0,0,0.35)" strokeWidth={thickness} />
        {segments.map((s, i) => {
          const len = (s.value / total) * circ
          const el = (
            <motion.circle
              key={i}
              cx={c} cy={c} r={r} fill="none" stroke={s.color} strokeWidth={thickness}
              strokeLinecap="round"
              strokeDasharray={`${len} ${circ - len}`}
              strokeDashoffset={-offset}
              initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: i * 0.1 }}
            />
          )
          offset += len
          return el
        })}
      </svg>
      <div className="flex flex-col gap-1.5">
        {centerValue && <div className="t-title leading-none">{centerValue}</div>}
        {centerLabel && <div className="t-caption mb-1">{centerLabel}</div>}
        {segments.map((s, i) => (
          <div key={i} className="flex items-center gap-2 t-caption">
            <span className="w-2.5 h-2.5 rounded-sm" style={{ background: s.color }} />
            <span className="text-[var(--text-secondary)]">{s.label}</span>
            <span className="ml-auto text-[var(--text-primary)] font-medium">{s.value}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

export function BarChart({ data }: { data: Segment[] }) {
  const max = Math.max(1, ...data.map(d => d.value))
  return (
    <div className="flex flex-col gap-2.5">
      {data.length === 0 && <div className="t-caption">No data yet</div>}
      {data.map((d, i) => (
        <div key={i} className="flex items-center gap-3">
          <span className="t-caption w-16 shrink-0 truncate text-right">{d.label}</span>
          <div className="flex-1 h-3 rounded-full neu-inset overflow-hidden">
            <motion.div
              className="h-full rounded-full"
              style={{ background: d.color }}
              initial={{ width: 0 }}
              animate={{ width: `${(d.value / max) * 100}%` }}
              transition={{ delay: i * 0.06, type: 'spring', stiffness: 120, damping: 20 }}
            />
          </div>
          <span className="t-label w-6 text-right tabular-nums">{d.value}</span>
        </div>
      ))}
    </div>
  )
}
