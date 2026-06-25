import { useState, useCallback, useRef } from 'react'
import type { UserID } from '../api/client'

export const useStream = () => {
  const [streaming, setStreaming] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  const stream = useCallback(async (
    message: string,
    sessionId: string,
    userId: UserID,
    onToken: (t: string) => void,
    onDone: () => void
  ) => {
    abortRef.current = new AbortController()
    setStreaming(true)

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, session_id: sessionId, stream: true, user_id: userId }),
        signal: abortRef.current.signal
      })

      const reader = res.body!.getReader()
      const decoder = new TextDecoder()
      let buf = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''

        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed.startsWith('data:')) continue
          const data = trimmed.slice(5).trim()
          if (data === '[DONE]') { onDone(); return }
          try {
            const parsed = JSON.parse(data)
            const t = parsed?.choices?.[0]?.delta?.content ?? ''
            if (t) onToken(t)
          } catch { /* skip malformed frames */ }
        }
      }
      onDone()
    } catch (e: unknown) {
      if (e instanceof Error && e.name !== 'AbortError') console.error('Stream error:', e)
    } finally {
      setStreaming(false)
    }
  }, [])

  const abort = () => abortRef.current?.abort()

  return { stream, streaming, abort }
}
