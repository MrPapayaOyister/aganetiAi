import { memo, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Copy, Check, RefreshCw, Volume2, FileText, Paperclip } from 'lucide-react'
import type { ChatMessage } from './MessageBubble'
import ToolEmbeds from './ToolEmbeds'
import AgentProcess from './AgentProcess'
import type { AgentProcessData } from '../lib/agentProcess'
import type { EmbedItem } from '../lib/embedWidget'

export type { ChatMessage }

export interface Source { source: string }

/** Chip-shaped view of an attachment. Name and size only — never the extracted
 *  text, which can be the full context budget. */
export interface AttachmentChipItem {
  filename: string
  ext: string
  size: number
  total_chars: number
  truncated: boolean
}

interface ConversationStreamProps {
  messages: ChatMessage[]
  streaming: boolean
  speakingMessageId?: string | null
  sourcesByMsg?: Record<string, Source[]>
  /** Tool-produced widgets per message: sandboxed-iframe HTML + optional link. */
  embedsByMsg?: Record<string, EmbedItem[]>
  /** Files attached to a turn. Rendered as chips so "what does this
   *  conversation have" stays answerable after a reload — the text is already
   *  in the model's context whether or not the UI shows it, and a silent
   *  attachment is worse than none. */
  attachmentsByMsg?: Record<string, AttachmentChipItem[]>
  /** POC-3 agent execution per message: stages, plan, evidence, verdict.
   *  Absent for ordinary turns, which is what keeps the panel off them. */
  agentByMsg?: Record<string, AgentProcessData>
  /** Channel picker commit — sends a follow-up asking to add the selection. */
  onAddChannels?: (names: string[], kind: string) => void
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
  messages, streaming, speakingMessageId, sourcesByMsg = {}, embedsByMsg = {},
  agentByMsg = {}, attachmentsByMsg = {},
  renderActionCards, onPlay, onRegenerate, agentMode = 'idle', onAddChannels,
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
            embeds={embedsByMsg[m.id]}
            attachments={attachmentsByMsg[m.id]}
            agent={agentByMsg[m.id]}
            onAddChannels={onAddChannels}
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
  msg, streaming, speaking, sources, embeds, attachments, agent, actionCards,
  onPlay, onRegenerate, isFirst, agentMode, onAddChannels,
}: {
  msg: ChatMessage
  streaming: boolean
  speaking: boolean
  sources?: Source[]
  embeds?: EmbedItem[]
  attachments?: AttachmentChipItem[]
  agent?: AgentProcessData
  onAddChannels?: (names: string[], kind: string) => void
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
  // Proactive (Aria-initiated) turns get a distinct entrance — they slide in
  // from the left edge ("came from elsewhere") with a longer, later settle —
  // signalling unsolicited but valuable input. Reactive replies rise softly.
  return (
    <motion.section
      initial={msg.proactive ? { opacity: 0, x: -14, y: 8 } : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, x: 0, y: 0 }}
      exit={{ opacity: 0 }}
      transition={msg.proactive
        ? { duration: 0.46, ease: [0.22, 1, 0.36, 1], delay: 0.06 }
        : { duration: 0.30, ease: [0.22, 1, 0.36, 1] }}
      className="group"
    >
      {/* Hairline separator above (except the very first message) */}
      {!isFirst && (
        <div className="h-px mb-3 bg-gradient-to-r from-transparent via-white/[0.06] to-transparent" />
      )}

      {/* Assistant label — solid cyan; amber + badge when Aria initiated it */}
      <div className="flex items-center gap-2 mb-1">
        <span className={`text-[10px] font-semibold uppercase tracking-[0.18em] opacity-80
                         ${msg.proactive ? 'text-[#F59E0B]' : 'text-[#00D4FF] opacity-70'}`}>
          Assistant{speaking ? '  ·  speaking' : ''}
        </span>
        {msg.proactive && (
          <span className="text-[9px] font-semibold uppercase tracking-[0.14em] px-1.5 py-0.5 rounded-full
                           bg-[#F59E0B]/15 border border-[#F59E0B]/30 text-[#F59E0B]">
            Aria initiated
          </span>
        )}
      </div>

      {/* Frosted slab — amber rail for proactive, mode-tinted otherwise */}
      <div className={`assistant-slab ${msg.proactive ? 'assistant-slab--proactive' : ''}`}
           data-mode={speaking ? 'speaking' : agentMode}>
        <div className="prose-chat text-[15px] leading-[1.7] text-[#E6EBF5]">
          {msg.content ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{msg.content}</ReactMarkdown>
          ) : streaming ? (
            <ThinkingPulse />
          ) : null}
          {streaming && msg.content && (
            <span className="inline-block w-[2px] h-4 bg-[#38DBFF] cursor-blink ml-0.5 align-middle" />
          )}

          {/* What this turn was given. Rendered from the SERVER's record, so it
              survives a reload — the extracted text stays in the model's context
              either way, and a document the assistant can see but the user
              cannot is the failure this replaces. */}
          {attachments && attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2" data-testid="attach-chips">
              {attachments.map((a, i) => (
                <span key={i}
                      data-attachment={a.filename}
                      title={`${a.total_chars.toLocaleString()} characters`
                             + (a.truncated ? ' (truncated)' : '')}
                      className="flex items-center gap-1 px-2 py-0.5 rounded-full
                                 bg-white/[0.04] border border-white/[0.06]
                                 text-[11px] text-[#9AA7BD] max-w-[220px]">
                  <Paperclip size={10} className="shrink-0 text-[#00D4FF]" />
                  <span className="truncate">{a.filename}</span>
                  {a.truncated && (
                    <span className="shrink-0 text-[#F0B429]" title="Only part of this file was read">·</span>
                  )}
                </span>
              ))}
            </div>
          )}

          {/* Tool widgets (YouTube player, …) — sandboxed iframes, never markdown */}
          {agent && <AgentProcess data={agent} />}

          {embeds && embeds.length > 0 && (
            <ToolEmbeds embeds={embeds} onAddChannels={onAddChannels} />
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
