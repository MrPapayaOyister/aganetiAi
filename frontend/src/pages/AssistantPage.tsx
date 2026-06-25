import { useState, useRef, useCallback, useEffect } from 'react'
import { generateId } from '../utils/uuid'
import { motion, AnimatePresence } from 'framer-motion'
import { Send, Paperclip, Volume2, VolumeX, StopCircle, Zap } from 'lucide-react'
import { OrbAnimation } from '../components/OrbAnimation'
import { MessageBubble } from '../components/MessageBubble'
import { VoiceButton } from '../components/VoiceButton'
import { useStream } from '../hooks/useStream'
import { useVoice } from '../hooks/useVoice'
import { useToast } from '../hooks/useToast'
import { useAppContext } from '../App'
import axios from 'axios'
import type { ChatMessage } from '../components/MessageBubble'

const QUICK_CHIPS = [
  { label: "What's on my agenda?",  prompt: 'What meetings do I have today?' },
  { label: 'Any urgent emails?',     prompt: 'Are there any urgent or high-priority emails in my inbox?' },
  { label: 'Summarize my tasks',     prompt: 'Give me a summary of my pending tasks.' },
]

function getGreeting() {
  const h = new Date().getHours()
  if (h < 12) return 'Good morning'
  if (h < 17) return 'Good afternoon'
  return 'Good evening'
}

export default function AssistantPage() {
  const { userId, ttsEnabled, setTtsEnabled } = useAppContext()
  const { addToast } = useToast()
  const { stream, streaming, abort } = useStream()
  const voice = useVoice()

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [sessionId] = useState(() => {
    const k = `aria_session_${userId}`
    const stored = sessionStorage.getItem(k)
    if (stored) return stored
    const id = generateId()
    sessionStorage.setItem(k, id)
    return id
  })

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const fullReplyRef = useRef('')

  const isIdle = messages.length === 0 && !streaming

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])

  useEffect(() => { scrollToBottom() }, [messages.length, scrollToBottom])

  // Mobile keyboard: track visual viewport height
  useEffect(() => {
    if (!window.visualViewport) return
    const handler = () => {
      document.documentElement.style.setProperty('--vvh', `${window.visualViewport!.height}px`)
    }
    window.visualViewport.addEventListener('resize', handler)
    handler()
    return () => window.visualViewport!.removeEventListener('resize', handler)
  }, [])

  const handleSend = useCallback(async (text: string) => {
    if (!text.trim() || streaming) return

    const userMsg: ChatMessage = { id: generateId(), role: 'user', content: text }
    const assistantId = generateId()
    const assistantMsg: ChatMessage = { id: assistantId, role: 'assistant', content: '', streaming: true }

    fullReplyRef.current = ''
    setMessages(prev => [...prev, userMsg, assistantMsg])
    setInput('')

    await stream(
      text,
      sessionId,
      userId,
      (token) => {
        fullReplyRef.current += token
        setMessages(prev => prev.map(m =>
          m.id === assistantId ? { ...m, content: m.content + token } : m
        ))
      },
      async () => {
        setMessages(prev => prev.map(m =>
          m.id === assistantId ? { ...m, streaming: false } : m
        ))
        if (ttsEnabled && fullReplyRef.current) {
          const plain = fullReplyRef.current.replace(/[*_`#>[\]()]/g, '').substring(0, 600)
          await voice.speak(plain)
        }
      }
    )
  }, [streaming, stream, sessionId, userId, ttsEnabled, voice])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend(input)
    }
  }

  const handleFileAttach = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('user_id', userId)
      await axios.post('/api/ingest/upload', form)
      addToast(`${file.name} indexed`, 'success')
      handleSend(`I've uploaded "${file.name}" — please acknowledge.`)
    } catch {
      addToast('Upload failed', 'error')
    }
    e.target.value = ''
  }

  return (
    <div className="flex flex-col overflow-hidden" style={{ height: 'var(--vvh, 100vh)' }}>
      {/* Header bar */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-[#1E3A5F]/30 shrink-0">
        <div className="flex items-center gap-3">
          <AnimatePresence mode="wait">
            {!isIdle ? (
              <motion.div
                key="orb-sm"
                initial={{ opacity: 0, scale: 0.5 }}
                animate={{ opacity: 1, scale: 1 }}
                exit={{ opacity: 0, scale: 0.5 }}
                transition={{ type: 'spring', stiffness: 400, damping: 30 }}
              >
                <OrbAnimation size="sm" streaming={streaming} speaking={voice.isSpeaking} />
              </motion.div>
            ) : (
              <motion.div key="icon" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                <Zap size={20} className="text-[#00D4FF]" />
              </motion.div>
            )}
          </AnimatePresence>
          <span className="font-semibold text-[#E2E8F0] text-sm">Aria</span>
          {streaming && (
            <span className="text-xs text-[#4A6080] animate-pulse">Thinking…</span>
          )}
          {voice.isSpeaking && (
            <span className="text-xs text-[#7B2FFF]">Speaking…</span>
          )}
        </div>

        <div className="flex items-center gap-1">
          <motion.button
            whileTap={{ scale: 0.88 }}
            onClick={() => setTtsEnabled(!ttsEnabled)}
            className={`w-8 h-8 rounded-lg flex items-center justify-center transition-colors
                        ${ttsEnabled ? 'text-[#00D4FF] bg-[#00D4FF]/10' : 'text-[#4A6080] hover:bg-white/5'}`}
            title={ttsEnabled ? 'Disable TTS' : 'Enable TTS'}
          >
            {ttsEnabled ? <Volume2 size={15} /> : <VolumeX size={15} />}
          </motion.button>

          {streaming && (
            <motion.button
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              whileTap={{ scale: 0.88 }}
              onClick={abort}
              className="w-8 h-8 rounded-lg flex items-center justify-center
                         text-[#FF4466] hover:bg-[#FF4466]/10 transition-colors"
              title="Stop"
            >
              <StopCircle size={15} />
            </motion.button>
          )}
        </div>
      </div>

      {/* Messages / Idle */}
      <div className="flex-1 overflow-hidden relative">
        <AnimatePresence mode="wait">
          {isIdle ? (
            <motion.div
              key="idle"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0, scale: 0.97 }}
              className="h-full flex flex-col items-center justify-center px-6 text-center"
            >
              <OrbAnimation size="lg" speaking={voice.isSpeaking} />

              <motion.h1
                initial={{ opacity: 0, y: 16 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.2 }}
                className="mt-6 text-2xl font-semibold text-[#E2E8F0]"
              >
                {getGreeting()}
              </motion.h1>
              <motion.p
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.32 }}
                className="mt-2 text-[#4A6080] text-sm"
              >
                Your AI assistant is ready.
              </motion.p>

              <motion.div
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.5 }}
                className="flex flex-wrap justify-center gap-2 mt-8 max-w-sm"
              >
                {QUICK_CHIPS.map(chip => (
                  <motion.button
                    key={chip.label}
                    whileHover={{ y: -2 }}
                    whileTap={{ scale: 0.97 }}
                    onClick={() => handleSend(chip.prompt)}
                    className="px-4 py-2 glass-sm rounded-full text-sm text-[#94A3B8]
                               hover:text-[#E2E8F0] transition-all"
                  >
                    {chip.label}
                  </motion.button>
                ))}
              </motion.div>
            </motion.div>
          ) : (
            <motion.div
              key="chat"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="h-full overflow-y-auto py-2"
            >
              <AnimatePresence initial={false}>
                {messages.map(msg => (
                  <motion.div
                    key={msg.id}
                    initial={{ opacity: 0, y: 12, scale: 0.98 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    transition={{ type: 'spring', stiffness: 420, damping: 30 }}
                  >
                    <MessageBubble
                      message={msg}
                      streaming={msg.streaming && streaming}
                    />
                  </motion.div>
                ))}
              </AnimatePresence>
              <div ref={messagesEndRef} />
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Input bar */}
      <div className="shrink-0 px-3 py-2.5 pb-safe border-t border-[#1E3A5F]/30 bg-[#070B14]/80">
        <div className="glass-sm rounded-2xl flex items-end gap-2 px-3 py-2 max-w-3xl mx-auto">
          {/* Attach */}
          <motion.button
            whileTap={{ scale: 0.88 }}
            onClick={() => fileInputRef.current?.click()}
            className="shrink-0 mb-1 w-8 h-8 flex items-center justify-center
                       rounded-full text-[#4A6080] hover:text-[#00D4FF] transition-colors min-w-[44px] min-h-[44px]"
            title="Attach file"
          >
            <Paperclip size={16} />
          </motion.button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf,.docx,.doc,.txt,.md"
            onChange={handleFileAttach}
            className="hidden"
          />

          {/* Textarea */}
          <textarea
            ref={inputRef}
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask Aria anything…"
            rows={1}
            disabled={voice.isListening}
            className="flex-1 bg-transparent resize-none outline-none text-sm text-[#E2E8F0]
                       placeholder:text-[#4A6080] leading-relaxed py-1.5 max-h-32 overflow-y-auto"
            onInput={e => {
              const el = e.currentTarget
              el.style.height = 'auto'
              el.style.height = Math.min(el.scrollHeight, 128) + 'px'
            }}
          />

          {/* Voice */}
          <VoiceButton
            voiceState={voice.state}
            analyserNode={voice.analyserNode}
            onStart={() => voice.startListening(handleSend)}
            onStop={voice.stopListening}
            disabled={streaming}
          />

          {/* Send */}
          <motion.button
            whileTap={{ scale: 0.88 }}
            onClick={() => handleSend(input)}
            disabled={!input.trim() || streaming}
            className="shrink-0 mb-0.5 w-9 h-9 rounded-full flex items-center justify-center
                       bg-[#00D4FF]/15 border border-[#00D4FF]/30 text-[#00D4FF]
                       hover:bg-[#00D4FF]/25 disabled:opacity-30 disabled:cursor-not-allowed
                       transition-all min-w-[44px] min-h-[44px]"
          >
            {streaming ? (
              <motion.div
                className="w-3 h-3 rounded-full border-2 border-[#00D4FF] border-t-transparent"
                animate={{ rotate: 360 }}
                transition={{ duration: 0.8, repeat: Infinity, ease: 'linear' }}
              />
            ) : (
              <Send size={15} />
            )}
          </motion.button>
        </div>
      </div>
    </div>
  )
}
