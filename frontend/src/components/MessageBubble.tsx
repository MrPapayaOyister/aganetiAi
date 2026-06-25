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
}

export function MessageBubble({ message, streaming }: MessageBubbleProps) {
  const isUser = message.role === 'user'
  const isEmpty = !message.content && streaming

  return (
    <div className={`flex gap-3 px-4 py-2 ${isUser ? 'flex-row-reverse' : 'flex-row'}`}>
      {/* Avatar */}
      <div className="shrink-0 mt-1">
        {isUser ? (
          <div className="w-8 h-8 rounded-full bg-gradient-to-br from-[#00D4FF]/30 to-[#7B2FFF]/30
                          border border-[#00D4FF]/30 flex items-center justify-center
                          text-[#00D4FF] text-xs font-bold">
            U
          </div>
        ) : (
          <OrbAnimation size="sm" streaming={streaming} />
        )}
      </div>

      {/* Bubble */}
      <div
        className={`max-w-[75%] md:max-w-[65%] rounded-2xl px-4 py-3 text-sm leading-relaxed
          ${isUser
            ? 'bg-[#00D4FF]/10 border border-[#00D4FF]/25 text-[#E2E8F0] rounded-tr-sm'
            : 'glass border border-[#1E3A5F]/40 text-[#E2E8F0] rounded-tl-sm'
          }`}
      >
        {isEmpty ? (
          <div className="flex gap-1 items-center h-4">
            {[0, 0.2, 0.4].map((d, i) => (
              <span
                key={i}
                className="w-1.5 h-1.5 rounded-full bg-[#4A6080]"
                style={{ animation: `blink 1.2s ${d}s ease-in-out infinite` }}
              />
            ))}
          </div>
        ) : (
          <>
            {isUser ? (
              <p className="whitespace-pre-wrap">{message.content}</p>
            ) : (
              <div className="prose-chat">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {message.content}
                </ReactMarkdown>
              </div>
            )}
            {streaming && !isUser && (
              <span className="inline-block w-0.5 h-4 bg-[#00D4FF] cursor-blink ml-0.5 align-middle" />
            )}
          </>
        )}
      </div>
    </div>
  )
}
