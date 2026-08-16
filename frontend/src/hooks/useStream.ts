import { useCallback, useRef, useState } from 'react'
import type { UserID } from '../api/client'
import { isValidVideoId, type EmbedItem } from '../lib/embedWidget'
import { authHeader } from '../lib/supabase'

export type ThinkingEvent = { message: string }
export type ActionEvent = {
  action: string
  payload: Record<string, unknown>
}

/** Wire shape of an attachment chip. Mirrors _attachment_chip in
 *  backend/main.py — name and size only, never the extracted text. */
export interface AttachmentChipWire {
  filename: string
  ext: string
  size: number
  total_chars: number
  truncated: boolean
}

interface UseStreamCallbacks {
  onToken: (t: string) => void
  onThinking?: (msg: string) => void
  onAction?: (action: string, payload: Record<string, unknown>) => void
  onSources?: (sources: Array<{ source: string }>) => void
  /** Widgets a tool produced: sandboxed-iframe HTML plus an optional link
   *  rendered outside the frame. Never text for the model — see
   *  backend/tool_result.py. */
  onEmbeds?: (embeds: EmbedItem[]) => void
  /** The server's id for this assistant turn. Arrives once the turn is
   *  persisted, and is the key rehydrated turns will use. */
  onMessageId?: (id: string) => void
  /** Which persisted message now owns the attachments uploaded this turn.
   *  Arrives BEFORE `message` so the id it names is already meaningful. The
   *  chips are keyed by it so a live turn and a reloaded one agree. */
  onAttachments?: (messageId: string, attachments: AttachmentChipWire[]) => void
  onError?: (msg: string) => void
  onDone: () => void | Promise<void>
  /** POC-3 agent frames. All three are OPTIONAL and all three use the EXISTING
   *  frame vocabulary (`stage`, `artifact`, `done`) — no new event type was
   *  introduced. A Runtime A turn emits none of them, so a caller that does not
   *  pass these callbacks behaves exactly as it did before.
   *
   *  The raw frame is handed over rather than a parsed shape: parsing lives in
   *  `lib/agentProcess.reduceAgentFrame`, so the hook stays a transport and the
   *  field whitelist has one home. */
  onStage?: (frame: Record<string, unknown>) => void
  onArtifact?: (frame: Record<string, unknown>) => void
  /** The `done` frame's `verified` flag. Distinct from `onDone`, which is the
   *  end-of-stream signal driven by the `[DONE]` sentinel and must keep firing
   *  exactly once whether or not a `done` frame was sent. */
  onVerified?: (frame: Record<string, unknown>) => void
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
        headers: { 'Content-Type': 'application/json', ...(await authHeader()) },
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
            } else if (parsed.type === 'embeds') {
              const embeds = parsed.payload?.embeds
              if (Array.isArray(embeds)) {
                // Only `html` is required; `link` is optional and dropped unless
                // it carries a usable url.
                const items: EmbedItem[] = embeds
                  .filter((e: unknown): e is EmbedItem =>
                    !!e && typeof (e as EmbedItem).html === 'string')
                  .map((e: EmbedItem) => ({
                    html: e.html,
                    link: e.link && typeof e.link.url === 'string'
                      ? { url: e.link.url, label: e.link.label ?? e.link.url }
                      : null,
                    // Validated again in ToolEmbeds before it reaches an iframe src.
                    video: isValidVideoId(e.video?.id)
                      ? { id: e.video!.id, start: Number(e.video!.start) || 0 }
                      : null,
                    results: Array.isArray(e.results)
                      ? e.results.filter(r => isValidVideoId(r?.id))
                      : null,
                    query: typeof e.query === 'string' ? e.query : null,
                    csp: typeof e.csp === 'string' ? e.csp : null,
                    articles: Array.isArray(e.articles)
                      ? e.articles.filter(a => a && typeof a.url === 'string'
                          && /^https?:\/\//i.test(a.url))
                      : null,
                    channels: Array.isArray(e.channels)
                      ? e.channels.filter(c => c && typeof c.name === 'string')
                      : null,
                    // Which library the picker commits to. Dropping this made a
                    // ticked radio station commit as a TV channel, which then
                    // failed HLS validation and read as a bad station.
                    //
                    // This whitelist is the hazard: it is written by hand, and a
                    // field the backend starts emitting is silently absent here
                    // until someone notices the feature not working. `qr` is the
                    // one key deliberately NOT carried — it is the server-side
                    // regeneration spec and the client has no use for it. Anything
                    // else the backend emits belongs in this list; there is a test
                    // that compares the two (tests/test_embed_contract.py).
                    channel_kind: typeof e.channel_kind === 'string' ? e.channel_kind : null,
                  }))
                if (items.length) callbacks.onEmbeds?.(items)
              }
            } else if (parsed.type === 'stage') {
              // POC-3 pipeline step. Appended as a NEW branch below the existing
              // ones so no current event changes behaviour.
              callbacks.onStage?.(parsed)
            } else if (parsed.type === 'artifact') {
              callbacks.onArtifact?.(parsed)
            } else if (parsed.type === 'done') {
              // Carries `final` and `verified`. The stream is still terminated
              // by the `[DONE]` sentinel above — this does NOT call onDone, or a
              // turn emitting both would finish twice.
              callbacks.onVerified?.(parsed)
            } else if (parsed.type === 'attachment') {
              const mid = parsed.payload?.message_id
              const list = parsed.payload?.attachments
              if (typeof mid === 'string' && mid && Array.isArray(list)) {
                // Only `filename` is required. The rest is display detail and a
                // missing field must degrade the chip, not drop it.
                const chips: AttachmentChipWire[] = list
                  .filter((a: unknown): a is AttachmentChipWire =>
                    !!a && typeof (a as AttachmentChipWire).filename === 'string')
                  .map((a: AttachmentChipWire) => ({
                    filename: a.filename,
                    ext: typeof a.ext === 'string' ? a.ext : '',
                    size: Number(a.size) || 0,
                    total_chars: Number(a.total_chars) || 0,
                    truncated: !!a.truncated,
                  }))
                if (chips.length) callbacks.onAttachments?.(mid, chips)
              }
            } else if (parsed.type === 'message') {
              const id = parsed.payload?.id
              if (typeof id === 'string' && id) callbacks.onMessageId?.(id)
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
