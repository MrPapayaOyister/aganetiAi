import { useCallback, useRef, useState } from 'react'
import type { UserID } from '../api/client'

export type ThinkingEvent = { message: string }
export type ActionEvent = {
  action: string
  payload: Record<string, unknown>
}

interface UseStreamCallbacks {
  onToken: (t: string) => void
  onThinking?: (msg: string) => void
  onAction?: (action: string, payload: Record<string, unknown>) => void
  onSources?: (sources: Array<{ source: string }>) => void
  onError?: (msg: string) => void
  onDone: () => void | Promise<void>
}

export const useStream = () => {
  const [streaming, setStreaming] = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  const stream = useCallback(async (
    message: string,
    sessionId: string,
    userId: UserID,
    callbacks: UseStreamCallbacks
  ) => {
    abortRef.current = new AbortController()
    setStreaming(true)

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message,
          session_id: sessionId,
          stream: true,
          user_id: userId,
        }),
        signal: abortRef.current.signal,
      })

      if (!res.ok) {
        callbacks.onError?.(`Server error: ${res.status}`)
        callbacks.onDone()
        return
      }

      const reader = res.body!.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? ''

        for (const line of lines) {
          if (!line.startsWith('data:')) continue
          const raw = line.slice(line.indexOf(':') + 1).trim()
          if (raw === '[DONE]') { await callbacks.onDone(); return }

          try {
            const parsed = JSON.parse(raw)

            // New typed event format
            if (parsed.type === 'token') {
              callbacks.onToken(parsed.content ?? '')
            } else if (parsed.type === 'thinking') {
              callbacks.onThinking?.(parsed.message ?? '')
            } else if (parsed.type === 'action') {
              callbacks.onAction?.(parsed.action ?? '', parsed.payload ?? {})
            } else if (parsed.type === 'sources') {
              callbacks.onSources?.(parsed.payload?.sources ?? [])
            } else if (parsed.type === 'error') {
              callbacks.onError?.(parsed.message ?? 'An error occurred')
            }
            // Legacy fallback — old OpenAI delta format
            else if (parsed.choices?.[0]?.delta?.content) {
              callbacks.onToken(parsed.choices[0].delta.content)
            }
          } catch {
            // malformed chunk, skip
          }
        }
      }
      await callbacks.onDone()
    } catch (e: unknown) {
      if (e instanceof Error && e.name !== 'AbortError') {
        callbacks.onError?.('Connection lost')
      }
      await callbacks.onDone()
    } finally {
      setStreaming(false)
    }
  }, [])

  const abort = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  return { stream, streaming, abort }
}
