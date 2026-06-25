import { memo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Copy, Check, RefreshCw, Volume2, FileText } from 'lucide-react'
import type { ChatMessage } from './MessageBubble'

export type { ChatMessage }

export interface Source { source: string }

interface ConversationStreamProps {
  messages: ChatMessage[]
  streaming: boolean
  speakingMessageId?: string | null
  sourcesByMsg?: Record<string, Source[]>
  renderActionCards?: (messageId: string) => React.ReactNode
  onPlay?: (text: string) => void
  onRegenerate?: (id: string) => void
  /** Current agent mode — colors the assistant accent rail */
  agentMode?: string
}

/**
 * Bubble-less conversation rendering.
 *
 *   ╭─────────────────────────────────────────────────────────────╮
 *   │  USER · 2:43                                                │   ← quiet caps label
 *   │  Schedule a 9am call with Priya tomorrow                    │   ← user text, dim
 *   │                                                             │
 *   │  Hairline rule                                              │   ← thin divider
 *   │                                                             │
 *   │   Aria                                                      │   ← speaker, aurora-tint
 *   │   I've drafted a meeting for tomorrow at 9 AM with…         │   ← assistant prose
 *   │   ▎live cursor                                              │
 *   │                                                             │
 *   │   [action card]   [source chip]                             │
 *   ╰─────────────────────────────────────────────────────────────╯
 */
function ConversationStreamBase({
  messages, streaming, speakingMessageId, sourcesByMsg = {},
  renderActionCards, onPlay, onRegenerate, agentMode = 'idle',
}: ConversationStreamProps) {
  return (
    <div className="conv-surface conv-lane max-w-3xl mx-auto w-full px-4 sm:px-7 py-6 space-y-3">
      <AnimatePresence initial={false}>
        {messages.map((m, i) => (
          <ConvTurn
            key={m.id}
            msg={m}
            streaming={!!m.streaming && streaming}
            speaking={speakingMessageId === m.id}
            sources={sourcesByMsg[m.id]}
            actionCards={renderActionCards?.(m.id)}
            onPlay={onPlay}
            onRegenerate={onRegenerate}
            isFirst={i === 0}
            agentMode={agentMode}
          />
        ))}
      </AnimatePresence>
    </div>
  )
}

function ConvTurn({
  msg, streaming, speaking, sources, actionCards, onPlay, onRegenerate, isFirst, agentMode,
}: {
  msg: ChatMessage
  streaming: boolean
  speaking: boolean
  sources?: Source[]
  actionCards?: React.ReactNode
  onPlay?: (t: string) => void
  onRegenerate?: (id: string) => void
  isFirst: boolean
  agentMode: string
}) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard?.writeText(msg.content).then(() => {
      setCopied(true); setTimeout(() => setCopied(false), 1400)
    }).catch(() => {})
  }

  const isUser = msg.role === 'user'

  // ── USER: right-aligned dark blue pill ────────────────────────
  if (isUser) {
    return (
      <motion.section
        initial={{ opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.20, ease: 'easeOut' }}
        className="flex flex-col items-end"
      >
        <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#5C6B85] mb-1
                         opacity-45">
          You
        </span>
        <div className="user-pill whitespace-pre-wrap">{msg.content}</div>
      </motion.section>
    )
  }

  // ── ASSISTANT: full-width frosted slab with cyan rail ──────────
  return (
    <motion.section
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.20, ease: 'easeOut' }}
      className="group"
    >
      {/* Hairline separator above (except the very first message) */}
      {!isFirst && (
        <div className="h-px mb-3 bg-gradient-to-r from-transparent via-white/[0.06] to-transparent" />
      )}

      {/* Assistant label — solid cyan (was a distracting cyan→violet gradient) */}
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-[#00D4FF] opacity-70">
          Assistant{speaking ? '  ·  speaking' : ''}
        </span>
      </div>

      {/* Frosted slab */}
      <div className="assistant-slab" data-mode={speaking ? 'speaking' : agentMode}>
        <div className="prose-chat text-[15px] leading-[1.7] text-[#E6EBF5]">
          {msg.content ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
          ) : streaming ? (
            <ThinkingPulse />
          ) : null}
          {streaming && msg.content && (
            <span className="inline-block w-[2px] h-4 bg-[#38DBFF] cursor-blink ml-0.5 align-middle" />
          )}

          {/* Citations */}
          {sources && sources.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-3">
              {sources.map((s, i) => (
                <span key={i}
                      className="flex items-center gap-1 px-2 py-0.5 rounded-full
                                 bg-white/[0.04] border border-white/[0.06] text-[11px] text-[#9AA7BD]">
                  <FileText size={10} className="text-[#38DBFF]" />
                  {s.source}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Action cards live INSIDE the slab so they group visually with the answer */}
        {actionCards && <div className="mt-3 space-y-1.5">{actionCards}</div>}
      </div>

      {/* Hover actions */}
      {!streaming && msg.content && (
        <div className="flex items-center gap-1 mt-2 ml-1 opacity-0 group-hover:opacity-100 transition-opacity">
          <ActionBtn title={copied ? 'Copied' : 'Copy'} onClick={copy}>
            {copied ? <Check size={13} className="text-[#00FF88]" /> : <Copy size={13} />}
          </ActionBtn>
          {onPlay && (
            <ActionBtn title="Play" onClick={() => onPlay(msg.content)}>
              <Volume2 size={13} />
            </ActionBtn>
          )}
          {onRegenerate && (
            <ActionBtn title="Regenerate" onClick={() => onRegenerate(msg.id)}>
              <RefreshCw size={13} />
            </ActionBtn>
          )}
        </div>
      )}
    </motion.section>
  )
}

function ThinkingPulse() {
  return (
    <div className="flex items-center gap-1.5 h-5">
      {[0, 0.15, 0.3].map((d, i) => (
        <span key={i} className="w-1.5 h-1.5 rounded-full bg-[#38DBFF]"
              style={{ animation: `blink 1.1s ${d}s ease-in-out infinite` }} />
      ))}
    </div>
  )
}

function ActionBtn({ children, title, onClick }:
  { children: React.ReactNode; title: string; onClick: () => void }) {
  return (
    <button
      title={title}
      onClick={onClick}
      className="w-7 h-7 rounded-lg flex items-center justify-center text-[#5C6B85]
                 hover:text-[#E6EBF5] hover:bg-white/[0.04] transition-colors"
    >
      {children}
    </button>
  )
}

export const ConversationStream = memo(ConversationStreamBase)
export default ConversationStream
