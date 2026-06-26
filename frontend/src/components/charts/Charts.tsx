import { motion } from 'framer-motion'

/* Dependency-free SVG charts, neumorphic styling. Item 6: stable keys,
   chart fade-in, staggered bar draw-in, taller bars for label clearance. */

const EASE = [0.16, 1, 0.3, 1] as const

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
    <motion.div
      className="flex items-center gap-4"
      initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ duration: 0.4, ease: EASE }}
    >
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="-rotate-90">
        <circle cx={c} cy={c} r={r} fill="none" stroke="rgba(0,0,0,0.35)" strokeWidth={thickness} />
        {segments.map((s, i) => {
          const len = (s.value / total) * circ
          const el = (
            <motion.circle
              key={`${s.label}-${s.color}`}                /* stable key — no remount on reorder */
              cx={c} cy={c} r={r} fill="none" stroke={s.color} strokeWidth={thickness}
              strokeLinecap="round"
              strokeDasharray={`${len} ${circ - len}`}
              strokeDashoffset={-offset}
              initial={{ opacity: 0.25 }} animate={{ opacity: 1 }}
              transition={{ duration: 0.5, ease: EASE, delay: i * 0.1 }}
            />
          )
          offset += len
          return el
        })}
      </svg>
      <div className="flex flex-col gap-1.5">
        {centerValue && <div className="t-title leading-none">{centerValue}</div>}
        {centerLabel && <div className="t-caption mb-1 text-[var(--text-secondary)]">{centerLabel}</div>}
        {segments.map(s => (
          <div key={`${s.label}-${s.color}`} className="flex items-center gap-2 t-caption">
            <span className="w-2.5 h-2.5 rounded-sm" style={{ background: s.color }} />
            <span className="text-[var(--text-secondary)]">{s.label}</span>
            <span className="ml-auto text-[var(--text-primary)] font-medium">{s.value}</span>
          </div>
        ))}
      </div>
    </motion.div>
  )
}

export function BarChart({ data }: { data: Segment[] }) {
  const max = Math.max(1, ...data.map(d => d.value))
  return (
    <div className="flex flex-col gap-3">
      {data.length === 0 && <div className="t-caption">No data yet</div>}
      {data.map((d, i) => (
        <div key={`${d.label}-${i}`} className="flex items-center gap-3">
          <span className="t-caption w-16 shrink-0 truncate text-right">{d.label}</span>
          <div className="flex-1 h-4 rounded-full neu-inset overflow-hidden">
            <motion.div
              className="h-full rounded-full"
              style={{ background: d.color }}
              initial={{ width: 0 }}
              animate={{ width: `${(d.value / max) * 100}%` }}
              transition={{ delay: i * 0.04, duration: 0.5, ease: EASE }}
            />
          </div>
          <span className="t-label min-w-8 text-right tabular-nums">{d.value}</span>
        </div>
      ))}
    </div>
  )
}
