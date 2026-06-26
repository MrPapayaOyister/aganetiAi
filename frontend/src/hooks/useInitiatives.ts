import { useEffect, useRef, useCallback } from 'react'
import { getInitiatives, ackInitiative, type Initiative } from '../api/client'

/**
 * Polls the backend initiative queue (P3) and delivers any NEW proactive
 * items to the caller. The caller decides how to render them (we inject them
 * into the conversation as proactive assistant messages).
 *
 * Polls every 45s while the page is visible. De-dups by initiative id so the
 * same item is delivered once even though /initiatives returns it until acked.
 */
export function useInitiatives(userId: string, onNew: (items: Initiative[]) => void) {
  const seen = useRef<Set<string>>(new Set())
  const onNewRef = useRef(onNew)
  useEffect(() => { onNewRef.current = onNew }, [onNew])

  const poll = useCallback(async () => {
    if (!userId || document.hidden) return
    try {
      const { data } = await getInitiatives(userId, 10)
      const fresh = (data.initiatives || []).filter(i => !seen.current.has(i.id))
      if (fresh.length) {
        fresh.forEach(i => seen.current.add(i.id))
        onNewRef.current(fresh)
      }
    } catch { /* offline / not ready — retry next tick */ }
  }, [userId])

  useEffect(() => {
    if (!userId) return
    // small initial delay so it doesn't race the first paint
    const t0 = setTimeout(poll, 4000)
    const id = setInterval(poll, 45000)
    const onVis = () => { if (!document.hidden) poll() }
    document.addEventListener('visibilitychange', onVis)
    return () => { clearTimeout(t0); clearInterval(id); document.removeEventListener('visibilitychange', onVis) }
  }, [userId, poll])

  const acknowledge = useCallback((id: string, dismissed = false) => {
    seen.current.add(id)
    ackInitiative(id, dismissed).catch(() => {})
  }, [])

  return { acknowledge }
}
