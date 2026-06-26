import { useEffect, useState, useRef, useCallback } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Loader2, CheckCircle2, XCircle, Network } from 'lucide-react'
import { getDelegations, type Delegation } from '../api/client'

/**
 * Live delegation status pills (P7/P8). Polls /delegations and shows
 * active + recently-finished delegations as animated pills:
 *   "Delegating to calendar agent…"  → "calendar_agent → completed"
 * Completed/failed pills auto-dismiss after a short linger.
 */
const LINGER_MS = 6000

export function DelegationDock({ userId }: { userId: string }) {
  const [items, setItems] = useState<Delegation[]>([])
  const lingerTimers = useRef<Record<string, number>>({})

  const poll = useCallback(async () => {
    if (!userId || document.hidden) return
    try {
      const { data } = await getDelegations(userId, 10)
      const all = data.delegations || []
      // Keep anything active; keep finished ones only within the linger window.
      const now = Date.now()
      const visible = all.filter(d => {
        if (d.status === 'pending' || d.status === 'in_progress') return true
        const fin = Date.parse(d.updated_at)
        return !isNaN(fin) && (now - fin) < LINGER_MS
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

  // Force a re-render as linger windows expire.
  useEffect(() => {
    if (!items.some(d => d.status === 'completed' || d.status === 'failed')) return
    const t = setTimeout(poll, LINGER_MS)
    return () => clearTimeout(t)
  }, [items, poll])

  void lingerTimers
  if (!items.length) return null

  return (
    <div className="flex flex-wrap gap-2 px-3 pb-2 max-w-3xl mx-auto w-full">
      <AnimatePresence>
        {items.map(d => (
          <motion.div
            key={d.id}
            initial={{ opacity: 0, y: 6, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, scale: 0.94 }}
            transition={{ type: 'spring', stiffness: 320, damping: 26 }}
            className="flex items-center gap-2 px-3 py-1.5 rounded-full text-xs glass-sm border"
            style={{
              borderColor:
                d.status === 'completed' ? 'rgba(0,255,136,0.3)'
                : d.status === 'failed' ? 'rgba(255,68,102,0.3)'
                : 'rgba(0,212,255,0.3)',
            }}
            title={d.result || d.error || d.task}
          >
            {d.status === 'completed' ? (
              <CheckCircle2 size={13} className="text-[#00FF88]" />
            ) : d.status === 'failed' ? (
              <XCircle size={13} className="text-[#FF4466]" />
            ) : (
              <Loader2 size={13} className="text-[#00D4FF] animate-spin" />
            )}
            <Network size={11} className="text-[#5C6B85]" />
            <span className="text-[#C8D3E5] font-medium">
              {d.to_agent.replace(/_/g, ' ')}
            </span>
            <span className="text-[#5C6B85]">
              {d.status === 'in_progress' || d.status === 'pending' ? '· working…'
                : d.status === 'completed' ? '· done'
                : '· failed'}
            </span>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  )
}

export default DelegationDock
