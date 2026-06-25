import { memo, useState } from 'react'
import { motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Copy, Check, RefreshCw, Volume2, FileText } from 'lucide-react'
import { OrbAnimation } from './OrbAnimation'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
}

export interface Source { source: string }

interface MessageBubbleProps {
  message: ChatMessage
  streaming?: boolean
  speaking?: boolean
  sources?: Source[]
  onRegenerate?: () => void
  onPlay?: (text: string) => void
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1.5 h-5">
      {[0, 0.15, 0.3].map((d, i) => (
        <span key={i} className="w-1.5 h-1.5 rounded-full bg-[#38DBFF]"
          style={{ animation: `blink 1.1s ${d}s ease-in-out infinite` }} />
      ))}
    </div>
  )
}

function MessageBubbleBase({ message, streaming, speaking, sources, onRegenerate, onPlay }: MessageBubbleProps) {
  const isUser = message.role === 'user'
  const isEmpty = !message.content && streaming
  const [copied, setCopied] = useState(false)

  const copy = () => {
    navigator.clipboard?.writeText(message.content).then(() => {
      setCopied(true); setTimeout(() => setCopied(false), 1400)
    }).catch(() => {})
  }

  if (isUser) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 14, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: 'spring', stiffness: 280, damping: 24 }}
        className="flex justify-end px-3 py-1.5"
      >
        <div className="max-w-[82%] px-4 py-2.5 rounded-2xl rounded-tr-md t-body text-white neu-pill"
             style={{ background: 'linear-gradient(135deg, rgba(0,212,255,0.18), rgba(123,47,255,0.16))' }}>
          <p className="whitespace-pre-wrap">{message.content}</p>
        </div>
      </motion.div>
    )
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: 'spring', stiffness: 280, damping: 26 }}
      className="group flex gap-3 px-3 py-1.5"
    >
      <div className="shrink-0 mt-0.5">
        <OrbAnimation mode={speaking ? 'speaking' : streaming ? 'thinking' : 'idle'} size={28} />
      </div>

      <div className="min-w-0 flex-1">
        <div className={`px-4 py-3 rounded-2xl rounded-tl-md t-body neu ${speaking ? 'speaking-pulse' : ''}`}>
          {isEmpty ? (
            <ThinkingDots />
          ) : (
            <div className="prose-chat">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
              {streaming && (
                <span className="inline-block w-[2px] h-4 bg-[#38DBFF] cursor-blink ml-0.5 align-middle" />
              )}
            </div>
          )}

          {/* RAG citations */}
          {sources && sources.length > 0 && (
            <div className="flex flex-wrap gap-1.5 mt-2.5 pt-2.5 border-t border-white/[0.06]">
              {sources.map((s, i) => (
                <span key={i} className="flex items-center gap-1 px-2 py-0.5 rounded-full neu-inset t-caption">
                  <FileText size={10} className="text-[#38DBFF]" />
                  {s.source}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Hover actions (assistant, not while streaming) */}
        {!streaming && message.content && (
          <div className="flex items-center gap-1 mt-1 ml-1 opacity-0 group-hover:opacity-100 transition-opacity">
            <ActionBtn title={copied ? 'Copied' : 'Copy'} onClick={copy}>
              {copied ? <Check size={13} className="text-[#00FF88]" /> : <Copy size={13} />}
            </ActionBtn>
            {onPlay && (
              <ActionBtn title="Play" onClick={() => onPlay(message.content)}><Volume2 size={13} /></ActionBtn>
            )}
            {onRegenerate && (
              <ActionBtn title="Regenerate" onClick={onRegenerate}><RefreshCw size={13} /></ActionBtn>
            )}
          </div>
        )}
      </div>
    </motion.div>
  )
}

function ActionBtn({ children, title, onClick }: { children: React.ReactNode; title: string; onClick: () => void }) {
  return (
    <button
      title={title}
      onClick={onClick}
      className="w-7 h-7 rounded-lg flex items-center justify-center text-[#5C6B85]
                 hover:text-[#E6EBF5] transition-colors"
    >
      {children}
    </button>
  )
}

export const MessageBubble = memo(MessageBubbleBase)
export default MessageBubble
