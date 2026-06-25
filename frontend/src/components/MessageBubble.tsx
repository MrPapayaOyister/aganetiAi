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
  userInitial?: string
}

function ThinkingDots() {
  return (
    <div className="flex items-center gap-1.5 h-5">
      <span className="text-[#4A6080] text-xs mr-1">Thinking</span>
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

function MessageBubbleBase({ message, streaming, speaking, userInitial = 'U' }: MessageBubbleProps) {
  const isUser = message.role === 'user'
  const isEmpty = !message.content && streaming

  return (
    <motion.div
      initial={{ opacity: 0, y: 16, scale: 0.97 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: 'spring', stiffness: 260, damping: 24 }}
      className={`flex gap-3 px-4 py-2 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}
    >
      {/* Avatar */}
      <div className="shrink-0 mt-0.5">
        {isUser ? (
          <div className="w-8 h-8 rounded-full bg-gradient-to-br from-[#00D4FF]/30 to-[#7B2FFF]/30
                          border border-[#00D4FF]/30 flex items-center justify-center
                          text-[#00D4FF] text-xs font-bold uppercase">
            {userInitial}
          </div>
        ) : (
          <OrbAnimation mode={speaking ? 'speaking' : streaming ? 'thinking' : 'idle'} size={30} />
        )}
      </div>

      {/* Bubble */}
      <div
        className={`max-w-[78%] md:max-w-[68%] px-4 py-3 text-sm leading-relaxed border
          ${isUser
            ? 'bg-[#00D4FF]/[0.06] border-l-2 border-[#00D4FF]/60 border-y-transparent border-r-transparent text-[#E2E8F0]'
            : `glass-strong text-[#E2E8F0] ${speaking ? 'border-pulse' : 'border-[#1E3A5F]/40'}`
          }`}
        style={{ borderRadius: isUser ? '16px 4px 16px 16px' : '4px 16px 16px 16px' }}
      >
        {isEmpty ? (
          <ThinkingDots />
        ) : isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
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
