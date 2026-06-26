import { useEffect, useState, useRef, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { CheckCircle2, XCircle, ChevronDown } from 'lucide-react'
import { getDelegations, type Delegation } from '../api/client'
import { agentMeta, ARIA } from '../lib/agents'

/**
 * Operational delegation capsules (Item 1). Each card tells the orchestration
 * story across four zones: SOURCE (Aria) → connector → TARGET (sub-agent) →
 * STATUS → RESULT. The breadcrumb arrow animates by state (dashed pulse pending,
 * flowing dash in-progress, solid completed, red-dashed failed). Cards reveal
 * left-to-right; completed/failed linger then dismiss.
 */
const LINGER_MS = 6000
const FAIL_LINGER_MS = 10000
const EASE = [0.16, 1, 0.3, 1] as const

function relTime(iso: string): string {
  const t = Date.parse(iso)
  if (isNaN(t)) return ''
  const s = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
}

const STATUS = {
  pending:     { color: '#00D4FF', label: 'queued' },
  in_progress: { color: '#00D4FF', label: 'running' },
  completed:   { color: '#00FF88', label: 'done' },
  failed:      { color: '#FF4466', label: 'failed' },
} as const

function DelegationCard({ d }: { d: Delegation }) {
  const [open, setOpen] = useState(false)
  // live duration tick while running
  const [, force] = useState(0)
  useEffect(() => {
    if (d.status !== 'in_progress' && d.status !== 'pending') return
    const id = setInterval(() => force(n => n + 1), 1000)
    return () => clearInterval(id)
  }, [d.status])

  const src = ARIA
  const tgt = agentMeta(d.to_agent)
  const st = STATUS[d.status] ?? STATUS.pending
  const running = d.status === 'in_progress' || d.status === 'pending'
  const Icon = tgt.icon
  const detail = (d.result || d.error || '').trim()

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 8, scale: 0.97 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, scale: 0.95, transition: { duration: 0.25 } }}
      transition={{ duration: 0.46, ease: EASE }}
      className="neu rounded-2xl overflow-hidden w-full max-w-xl"
      style={{ borderLeft: `2px solid ${st.color}` }}
    >
      <div className="flex items-stretch gap-0 px-3 py-2.5">
        {/* ZONE 1 — SOURCE */}
        <motion.div
          initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.3, ease: EASE }}
          className="flex items-center gap-2 shrink-0"
        >
          <span className="w-6 h-6 rounded-lg flex items-center justify-center
                           bg-[#00D4FF]/12 border border-[#00D4FF]/25">
            <src.icon size={13} className="text-[#00D4FF]" />
          </span>
          <div className="leading-tight">
            <div className="text-[11px] font-semibold text-[#C8D3E5]">{src.label}</div>
            <div className="text-[9px] text-[#5C6B85]">{relTime(d.created_at)} ago</div>
          </div>
        </motion.div>

        {/* ZONE 2 — CONNECTOR (animated breadcrumb arrow) */}
        <div className="flex items-center px-2 flex-1 min-w-[40px]">
          <svg className="w-full h-3" viewBox="0 0 100 12" preserveAspectRatio="none">
            <line x1="2" y1="6" x2="92" y2="6"
              stroke={st.color}
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeDasharray={d.status === 'completed' ? '0' : '5 4'}
              className={d.status === 'in_progress' ? 'edge-flow'
                : d.status === 'pending' ? 'status-breathe' : ''}
              opacity={d.status === 'failed' ? 0.7 : 1}
            />
            <path d={`M 90 2 L 96 6 L 90 10`} fill="none"
              stroke={st.color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>

        {/* ZONE 3 — TARGET + STATUS */}
        <motion.div
          initial={{ opacity: 0, x: 6 }} animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.3, ease: EASE, delay: 0.12 }}
          className="flex items-center gap-2 shrink-0"
        >
          <span className="w-6 h-6 rounded-lg flex items-center justify-center border"
                style={{ background: `${tgt.color}1f`, borderColor: `${tgt.color}40` }}>
            <Icon size={13} style={{ color: tgt.color }} />
          </span>
          <div className="leading-tight">
            <div className="text-[11px] font-semibold text-[#C8D3E5]">{tgt.label}</div>
            <div className="text-[9px] text-[#5C6B85]">{tgt.role}</div>
          </div>
          {/* status pill */}
          <div className="flex items-center gap-1.5 ml-1 pl-2 border-l border-white/[0.07]">
            {d.status === 'completed' ? (
              <CheckCircle2 size={13} className="text-[#00FF88]" />
            ) : d.status === 'failed' ? (
              <XCircle size={13} className="text-[#FF4466]" />
            ) : (
              <span className="status-breathe w-2 h-2 rounded-full"
                    style={{ background: st.color, boxShadow: `0 0 8px ${st.color}` }} />
            )}
            <span className="text-[10px] font-medium" style={{ color: st.color }}>
              {running ? `${st.label} ${relTime(d.created_at)}` : st.label}
            </span>
          </div>
        </motion.div>

        {/* ZONE 4 — expand toggle (only when there's detail) */}
        {detail && (
          <button
            onClick={() => setOpen(o => !o)}
            className="press shrink-0 ml-1 w-6 h-6 rounded-lg flex items-center justify-center
                       text-[#5C6B85] hover:text-[#C8D3E5] hover:bg-white/[0.04]"
            title={open ? 'Hide detail' : 'Show detail'}
          >
            <motion.span animate={{ rotate: open ? 180 : 0 }} transition={{ duration: 0.24, ease: EASE }}>
              <ChevronDown size={14} />
            </motion.span>
          </button>
        )}
      </div>

      {/* ZONE 4 — RESULT detail (expand/collapse) */}
      <AnimatePresence initial={false}>
        {open && detail && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.34, ease: EASE }}
            className="overflow-hidden border-t border-white/[0.06]"
          >
            <div className="px-3 py-2.5">
              <div className="text-[9px] uppercase tracking-[0.16em] text-[#5C6B85] mb-1">
                {d.status === 'failed' ? 'Error' : 'Result'}
              </div>
              <p className="text-xs text-[#C8D3E5] leading-relaxed whitespace-pre-wrap">{detail}</p>
              <div className="text-[9px] text-[#5C6B85] mt-1.5">
                {src.label} → {tgt.label} · {relTime(d.updated_at)} ago
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}

export function DelegationDock({ userId }: { userId: string }) {
  const [items, setItems] = useState<Delegation[]>([])
  const seenActive = useRef<Set<string>>(new Set())

  const poll = useCallback(async () => {
    if (!userId || document.hidden) return
    try {
      const { data } = await getDelegations(userId, 10)
      const all = data.delegations || []
      const now = Date.now()
      const visible = all.filter(d => {
        if (d.status === 'pending' || d.status === 'in_progress') return true
        const fin = Date.parse(d.updated_at)
        const window = d.status === 'failed' ? FAIL_LINGER_MS : LINGER_MS
        return !isNaN(fin) && (now - fin) < window
      })
      setItems(visible)
    } catch { /* ignore */ }
  }, [userId])

  useEffect(() => {
    if (!userId) return
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [userId, poll])

  useEffect(() => {
    if (!items.some(d => d.status === 'completed' || d.status === 'failed')) return
    const t = setTimeout(poll, LINGER_MS)
    return () => clearTimeout(t)
  }, [items, poll])

  void seenActive
  if (!items.length) return null

  return (
    <div className="flex flex-col gap-2 px-3 pb-2 max-w-3xl mx-auto w-full items-center">
      <AnimatePresence>
        {items.map(d => <DelegationCard key={d.id} d={d} />)}
      </AnimatePresence>
    </div>
  )
}

export default DelegationDock
