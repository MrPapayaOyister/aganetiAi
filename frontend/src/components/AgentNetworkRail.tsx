import { useEffect, useMemo, useState, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X } from 'lucide-react'
import { getDelegations, type Delegation } from '../api/client'
import { AGENTS, ARIA, agentMeta } from '../lib/agents'

/**
 * AgentNetworkRail (Item 2) — the orchestration centerpiece. A dismissible
 * right panel showing Aria at the center wired to its sub-agents. Edges live-
 * animate by state: faint dashed when idle (with ambient slow pulse on a
 * couple so it never looks dead), bright flowing dash when a delegation to
 * that agent is active, a single flash on completion. Click a node to see its
 * recent activity. Slides in from the right with blur-focus; nodes stagger
 * (center first), edges draw after.
 */
const EASE = [0.16, 1, 0.3, 1] as const
const SIZE = 300
const CX = SIZE / 2
const CY = SIZE / 2
const R = 102

type EdgeState = 'idle' | 'active' | 'done'

export function AgentNetworkRail({
  userId, open, onClose,
}: { userId: string; open: boolean; onClose: () => void }) {
  const [delegations, setDelegations] = useState<Delegation[]>([])
  const [selected, setSelected] = useState<string | null>(null)

  const poll = useCallback(async () => {
    if (!userId || !open || document.hidden) return
    try {
      const { data } = await getDelegations(userId, 30)
      setDelegations(data.delegations || [])
    } catch { /* ignore */ }
  }, [userId, open])

  useEffect(() => {
    if (!open) return
    poll()
    const id = setInterval(poll, 3000)
    return () => clearInterval(id)
  }, [open, poll])

  // Per-agent edge state derived from recent delegations.
  const edgeState = useMemo(() => {
    const m: Record<string, EdgeState> = {}
    const now = Date.now()
    for (const a of AGENTS) m[a.id] = 'idle'
    for (const d of delegations) {
      const meta = agentMeta(d.to_agent)
      const key = AGENTS.find(a => a.id === meta.id || a.id === d.to_agent)?.id
      if (!key) continue
      if (d.status === 'pending' || d.status === 'in_progress') m[key] = 'active'
      else if ((d.status === 'completed') && now - Date.parse(d.updated_at) < 5000 && m[key] !== 'active')
        m[key] = 'done'
    }
    return m
  }, [delegations])

  // Node coordinates on a circle around Aria.
  const nodes = useMemo(() => AGENTS.map((a, i) => {
    const ang = (-Math.PI / 2) + (i / AGENTS.length) * Math.PI * 2
    return { agent: a, x: CX + Math.cos(ang) * R, y: CY + Math.sin(ang) * R }
  }), [])

  const activity = (agentId: string) =>
    delegations.filter(d => agentMeta(d.to_agent).id === agentId || d.to_agent === agentId).slice(0, 5)

  return (
    <AnimatePresence>
      {open && (
        <>
          {/* click-away scrim (does not blur chat heavily — rail is a side panel) */}
          <motion.div
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            transition={{ duration: 0.34, ease: EASE }}
            onClick={onClose}
            className="fixed inset-0 z-[110]"
            style={{ background: 'rgba(4,6,11,0.35)', backdropFilter: 'blur(2px)' }}
          />
          <motion.aside
            initial={{ x: 40, opacity: 0, filter: 'blur(10px)' }}
            animate={{ x: 0, opacity: 1, filter: 'blur(0px)' }}
            exit={{ x: 40, opacity: 0, filter: 'blur(8px)' }}
            transition={{ duration: 0.56, ease: EASE }}
            className="fixed right-0 top-0 bottom-0 z-[111] w-[340px] max-w-[88vw]
                       glass-strong border-l border-white/[0.08] flex flex-col"
          >
            {/* header */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-white/[0.06]">
              <div>
                <h2 className="text-sm font-semibold text-[#E2E8F0]">Agent network</h2>
                <p className="text-[10px] text-[#5C6B85]">Live orchestration topology</p>
              </div>
              <button onClick={onClose}
                className="press w-8 h-8 rounded-lg flex items-center justify-center
                           text-[#5C6B85] hover:text-[#E2E8F0] hover:bg-white/[0.05]">
                <X size={16} />
              </button>
            </div>

            {/* network diagram */}
            <div className="relative mx-auto my-4" style={{ width: SIZE, height: SIZE }}>
              <svg width={SIZE} height={SIZE} className="absolute inset-0">
                {/* edges (draw after nodes settle) */}
                {nodes.map((n, i) => {
                  const state = edgeState[n.agent.id]
                  const color = state === 'idle' ? 'rgba(154,167,189,0.25)' : n.agent.color
                  return (
                    <motion.line
                      key={n.agent.id}
                      x1={CX} y1={CY} x2={n.x} y2={n.y}
                      stroke={color}
                      strokeWidth={state === 'active' ? 2 : 1.2}
                      strokeLinecap="round"
                      strokeDasharray="6 6"
                      className={state === 'active' ? 'edge-flow-fast'
                        : (i < 2 ? 'status-breathe' : '')}
                      initial={{ pathLength: 0, opacity: 0 }}
                      animate={{ pathLength: 1, opacity: 1 }}
                      transition={{ duration: 0.5, ease: EASE, delay: 0.35 + i * 0.06 }}
                    />
                  )
                })}
              </svg>

              {/* satellite nodes */}
              {nodes.map((n, i) => {
                const state = edgeState[n.agent.id]
                const Icon = n.agent.icon
                const isSel = selected === n.agent.id
                return (
                  <motion.button
                    key={n.agent.id}
                    onClick={() => setSelected(isSel ? null : n.agent.id)}
                    initial={{ opacity: 0, scale: 0.7 }}
                    animate={{ opacity: 1, scale: 1 }}
                    transition={{ duration: 0.42, ease: EASE, delay: 0.18 + i * 0.07 }}
                    className="press absolute -translate-x-1/2 -translate-y-1/2 flex flex-col items-center gap-1
                               px-2.5 py-1.5 rounded-xl neu"
                    style={{
                      left: n.x, top: n.y,
                      borderColor: isSel ? n.agent.color : undefined,
                      boxShadow: state === 'active'
                        ? `var(--shadow-raised), 0 0 18px ${n.agent.color}55`
                        : undefined,
                    }}
                  >
                    <span className="relative">
                      <Icon size={15} style={{ color: n.agent.color }} />
                      <span className={`absolute -top-1 -right-1.5 w-1.5 h-1.5 rounded-full
                                        ${state === 'active' ? 'status-breathe' : ''}`}
                            style={{ background: state === 'idle' ? '#3A4256' : n.agent.color }} />
                    </span>
                    <span className="text-[9px] font-semibold text-[#C8D3E5] leading-none">{n.agent.label}</span>
                  </motion.button>
                )
              })}

              {/* Aria — larger central node */}
              <motion.div
                initial={{ opacity: 0, scale: 0.6 }} animate={{ opacity: 1, scale: 1 }}
                transition={{ duration: 0.5, ease: EASE }}
                className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2
                           flex flex-col items-center gap-1 px-3 py-2 rounded-2xl glow-cyan"
                style={{ background: 'var(--bg-elevated)' }}
              >
                <ARIA.icon size={20} className="text-[#00D4FF]" />
                <span className="text-[10px] font-bold text-[#E2E8F0] leading-none">Aria</span>
                <span className="text-[8px] text-[#5C6B85] leading-none">orchestrator</span>
              </motion.div>
            </div>

            {/* selected agent activity */}
            <div className="flex-1 overflow-y-auto px-4 pb-4">
              {selected ? (
                <div>
                  <div className="text-[10px] uppercase tracking-[0.16em] text-[#5C6B85] mb-2">
                    {agentMeta(selected).label} · recent activity
                  </div>
                  {activity(selected).length === 0 ? (
                    <p className="text-xs text-[#5C6B85]">No recent tasks for this agent.</p>
                  ) : (
                    <div className="space-y-2">
                      {activity(selected).map(d => (
                        <div key={d.id} className="glass-sm rounded-xl px-3 py-2">
                          <div className="text-xs text-[#C8D3E5] line-clamp-2">{d.task}</div>
                          <div className="text-[10px] mt-1"
                               style={{ color: d.status === 'completed' ? '#00FF88'
                                 : d.status === 'failed' ? '#FF4466' : '#00D4FF' }}>
                            {d.status.replace('_', ' ')}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ) : (
                <p className="text-[11px] text-[#5C6B85] text-center leading-relaxed mt-2">
                  Tap a node to see what Aria has delegated to it.
                </p>
              )}
            </div>
          </motion.aside>
        </>
      )}
    </AnimatePresence>
  )
}

export default AgentNetworkRail
