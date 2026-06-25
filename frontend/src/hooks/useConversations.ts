import { useCallback, useEffect, useState } from 'react'
import { generateId } from '../utils/uuid'

export interface Convo { id: string; title: string; updatedAt: number }

/**
 * Client-side multi-conversation store (localStorage). Messages themselves are
 * persisted per-session by the caller under `aria_msgs_{id}`. The backend
 * accepts any session_id, so memory/RAG context stays correct per conversation.
 */
export function useConversations(userId: string) {
  const listKey = `aria_convos_${userId}`
  const activeKey = `aria_active_${userId}`

  const [sessions, setSessions] = useState<Convo[]>(() => {
    try { return JSON.parse(localStorage.getItem(listKey) || '[]') } catch { return [] }
  })
  const [activeId, setActiveId] = useState<string>(() => {
    const stored = localStorage.getItem(activeKey)
    if (stored) return stored
    const id = generateId()
    return id
  })

  // Persist list + active
  useEffect(() => { localStorage.setItem(listKey, JSON.stringify(sessions)) }, [sessions, listKey])
  useEffect(() => { localStorage.setItem(activeKey, activeId) }, [activeId, activeKey])

  // Ensure the active session exists in the list
  useEffect(() => {
    setSessions(prev => prev.some(s => s.id === activeId)
      ? prev
      : [{ id: activeId, title: 'New chat', updatedAt: Date.now() }, ...prev])
  }, [activeId])

  const newConversation = useCallback(() => {
    const id = generateId()
    setSessions(prev => [{ id, title: 'New chat', updatedAt: Date.now() }, ...prev])
    setActiveId(id)
    return id
  }, [])

  const switchTo = useCallback((id: string) => setActiveId(id), [])

  const remove = useCallback((id: string) => {
    try { localStorage.removeItem(`aria_msgs_${id}`) } catch { /* noop */ }
    setSessions(prev => {
      const next = prev.filter(s => s.id !== id)
      if (id === activeId) setActiveId(next[0]?.id ?? generateId())
      return next
    })
  }, [activeId])

  const rename = useCallback((id: string, title: string) => {
    setSessions(prev => prev.map(s => s.id === id ? { ...s, title } : s))
  }, [])

  // Set title from first message + bump updatedAt
  const touch = useCallback((id: string, firstText?: string) => {
    setSessions(prev => prev.map(s => {
      if (s.id !== id) return s
      const title = (s.title === 'New chat' && firstText) ? firstText.slice(0, 40) : s.title
      return { ...s, title, updatedAt: Date.now() }
    }))
  }, [])

  return { sessions, activeId, newConversation, switchTo, remove, rename, touch }
}
