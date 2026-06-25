import { memo } from 'react'
import { motion } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { OrbAnimation } from './OrbAnimation'

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  streaming?: boolean
}

interface MessageBubbleProps {
  message: ChatMessage
  streaming?: boolean
  speaking?: boolean         // active TTS playback → animated border
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1.5 h-5">
      {[0, 0.15, 0.3].map((d, i) => (
        <span
          key={i}
          className="w-1.5 h-1.5 rounded-full bg-[#00D4FF]"
          style={{ animation: `blink 1.1s ${d}s ease-in-out infinite` }}
        />
      ))}
    </div>
  )
}

function MessageBubbleBase({ message, streaming, speaking }: MessageBubbleProps) {
  const isUser = message.role === 'user'
  const isEmpty = !message.content && streaming

  if (isUser) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 14, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: 'spring', stiffness: 280, damping: 24 }}
        className="flex justify-end px-3 py-1.5"
      >
        <div className="max-w-[82%] px-4 py-2.5 rounded-2xl rounded-tr-md text-sm leading-relaxed
                        text-white bg-gradient-to-br from-[#00D4FF]/25 to-[#7B2FFF]/20
                        border border-[#00D4FF]/25 shadow-[0_2px_12px_rgba(0,212,255,0.08)]">
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
      className="flex gap-3 px-3 py-1.5"
    >
      {/* Orb avatar */}
      <div className="shrink-0 mt-0.5">
        <OrbAnimation mode={speaking ? 'speaking' : streaming ? 'thinking' : 'idle'} size={28} />
      </div>

      {/* Content */}
      <div
        className={`min-w-0 flex-1 px-4 py-3 rounded-2xl rounded-tl-md text-sm leading-relaxed
          border transition-colors
          ${speaking
            ? 'border-pulse bg-white/[0.03]'
            : 'border-white/[0.06] bg-white/[0.02]'}`}
      >
        {isEmpty ? (
          <ThinkingDots />
        ) : (
          <div className="prose-chat">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
            {streaming && (
              <span className="inline-block w-[2px] h-4 bg-[#00D4FF] cursor-blink ml-0.5 align-middle" />
            )}
          </div>
        )}
      </div>
    </motion.div>
  )
}

export const MessageBubble = memo(MessageBubbleBase)
export default MessageBubble
