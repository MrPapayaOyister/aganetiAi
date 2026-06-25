import { useState, useRef, useCallback, useEffect } from 'react'
import { generateId } from '../utils/uuid'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Send, Paperclip, Volume2, VolumeX, StopCircle, Zap, Music,
  CheckCircle2, RefreshCw, Mail, SendHorizontal, Calendar, Clock, Brain,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { OrbAnimation, type OrbMode } from '../components/OrbAnimation'
import { MessageBubble } from '../components/MessageBubble'
import { VoiceButton } from '../components/VoiceButton'
import { useStream } from '../hooks/useStream'
import { useVoice } from '../hooks/useVoice'
import { useSound } from '../hooks/useSound'
import { useToast } from '../hooks/useToast'
import { useAppContext } from '../App'
import { useAmbient } from '../contexts/AmbientContext'
import axios from 'axios'
import type { ChatMessage } from '../components/MessageBubble'

const FALLBACK_SUGGESTIONS = [
  "What's on my agenda today?",
  'Any urgent emails?',
  'Summarize my pending tasks',
]

interface ActionCard {
  id: string
  action: string
  payload: Record<string, unknown>
  messageId: string
}

function getGreeting() {
  const h = new Date().getHours()
  if (h < 12) return 'Good morning'
  if (h < 17) return 'Good afternoon'
  return 'Good evening'
}

// ── Action card helpers ─────────────────────────────────────────
const ACTION_ICONS: Record<string, React.ElementType> = {
  task_created: CheckCircle2, task_updated: RefreshCw, email_drafted: Mail,
  email_sent: SendHorizontal, event_created: Calendar, reminder_set: Clock, memory_saved: Brain,
}
function ActionIcon({ action }: { action: string }) {
  const Icon = ACTION_ICONS[action] ?? Zap
  return <Icon size={14} className="text-[#38DBFF] shrink-0" />
}

function ActionLabel({ action, payload }: { action: string; payload: Record<string, unknown> }) {
  const p = payload as Record<string, string>
  const labels: Record<string, () => string> = {
    task_created:  () => `Task created: "${p.title}"`,
    task_updated:  () => `Task updated: "${p.title ?? p.id}"`,
    email_drafted: () => `Email drafted to ${p.to}`,
    email_sent:    () => `Email sent: "${p.subject}"`,
    event_created: () => `Event scheduled: "${p.title}"`,
    reminder_set:  () => `Reminder set`,
    memory_saved:  () => `Memory saved`,
  }
  const fn = labels[action]
  return <span>{fn ? fn() : action.replace(/_/g, ' ')}</span>
}

export default function AssistantPage() {
  const { userId, ttsEnabled, setTtsEnabled } = useAppContext()
  const { addToast } = useToast()
  const { setAmbient } = useAmbient()
  const { stream, streaming, abort } = useStream()
  const voice = useVoice()
  const sound = useSound()
  const lastTickRef = useRef(0)

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [thinkingMsg, setThinkingMsg] = useState<string | null>(null)
  const [actionCards, setActionCards] = useState<ActionCard[]>([])
  const [errorFlash, setErrorFlash] = useState(false)
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
  const lastAssistant = messages[messages.length - 1]
  const lastAssistantId = [...messages].reverse().find(m => m.role === 'assistant')?.id
  const awaitingFirstToken =
    streaming && lastAssistant?.role === 'assistant' && !lastAssistant.content

  // Single derived orb/ambient phase — drives the whole "alive" loop.
  const orbMode: OrbMode =
    errorFlash                                        ? 'error'
    : voice.isListening                               ? 'listening'
    : voice.isSpeaking                                ? 'speaking'
    : (streaming && (thinkingMsg || awaitingFirstToken)) ? 'thinking'
    : streaming                                       ? 'speaking'
    : 'idle'

  // Drive the global particle field. Keyed on the discrete phase only (NOT the
  // per-frame voice amplitude) so we don't re-render the app shell at 60fps.
  useEffect(() => {
    const amp = orbMode === 'thinking' ? 0.8 : orbMode === 'speaking' ? 0.7
      : orbMode === 'listening' ? 0.6 : 0
    setAmbient(orbMode === 'error' ? 'idle' : orbMode, amp)
  }, [orbMode, setAmbient])

  // Reset the field to idle when leaving the page
  useEffect(() => () => setAmbient('idle', 0), [setAmbient])

  // Load prior conversation for this session on mount / session change
  useEffect(() => {
    let cancelled = false
    const loadHistory = async () => {
      try {
        const res = await fetch(`/api/chat/history?session_id=${encodeURIComponent(sessionId)}&limit=20`)
        const data = await res.json()
        if (!cancelled && Array.isArray(data.messages) && data.messages.length > 0) {
          setMessages(data.messages.map((m: { role: string; content: string }) => ({
            id: generateId(),
            role: m.role === 'assistant' ? 'assistant' : 'user',
            content: m.content,
            streaming: false,
          })))
        }
      } catch { /* ignore — fresh session */ }
    }
    loadHistory()
    return () => { cancelled = true }
  }, [sessionId])

  // Contextual quick-action suggestions (real data from the backend)
  const { data: suggestionsData } = useQuery({
    queryKey: ['suggestions', userId],
    queryFn: () =>
      fetch(`/api/chat/suggestions?user_id=${encodeURIComponent(userId)}`).then(r => r.json()),
    staleTime: 60_000,
    retry: false,
  })
  const suggestions: string[] = suggestionsData?.suggestions ?? FALLBACK_SUGGESTIONS

  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])

  useEffect(() => { scrollToBottom() }, [messages.length, scrollToBottom])
  useEffect(() => { scrollToBottom() }, [actionCards.length, scrollToBottom])

  // Subtle click cue on any button press (skip elements marked data-mute-click,
  // e.g. Send/chips which play their own richer cue).
  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      const el = (e.target as HTMLElement | null)?.closest('button,[role="button"]')
      if (el && !el.hasAttribute('data-mute-click')) sound.playClick()
    }
    window.addEventListener('pointerdown', onDown)
    return () => window.removeEventListener('pointerdown', onDown)
  }, [sound])

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
    sound.playSend()

    await stream(text, sessionId, userId, {
      onToken: (t) => {
        setThinkingMsg(null)        // clear thinking banner on first token
        // throttled "typing" tick
        const now = performance.now()
        if (now - lastTickRef.current > 110) { lastTickRef.current = now; sound.playTick() }
        fullReplyRef.current += t
        setMessages(prev => prev.map(m =>
          m.id === assistantId ? { ...m, content: m.content + t } : m
        ))
      },
      onThinking: (msg) => setThinkingMsg(msg),
      onAction: (action, payload) => {
        setActionCards(prev => [...prev, { id: generateId(), action, payload, messageId: assistantId }])
      },
      onError: (msg) => {
        addToast(msg, 'error')
        setErrorFlash(true)
        setTimeout(() => setErrorFlash(false), 800)
      },
      onDone: async () => {
        setThinkingMsg(null)
        sound.playReceive()
        setMessages(prev => prev.map(m =>
          m.id === assistantId ? { ...m, streaming: false } : m
        ))
        if (ttsEnabled && fullReplyRef.current) {
          const plain = fullReplyRef.current.replace(/[*_`#>[\]()]/g, '').substring(0, 600)
          await voice.speak(plain)
        }
      },
    })
  }, [streaming, stream, sessionId, userId, ttsEnabled, voice, addToast, sound])

  // Mic needs a secure context (HTTPS/localhost). On plain HTTP the browser
  // blocks getUserMedia + Web Speech, so guide the user instead of a dead button.
  const handleVoiceStart = useCallback(() => {
    if (!window.isSecureContext) {
      addToast('Voice input needs a secure (HTTPS) connection', 'info')
      return
    }
    if (!navigator.mediaDevices?.getUserMedia &&
        !('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
      addToast('Voice input is not supported in this browser', 'info')
      return
    }
    voice.startListening(handleSend)
  }, [addToast, voice, handleSend])

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
    <div className="flex flex-col overflow-hidden h-[var(--vvh,100dvh)] md:h-full
                    pb-[calc(var(--bottom-nav-height)+env(safe-area-inset-bottom))] md:pb-0">
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
                <OrbAnimation
                  size={36}
                  mode={orbMode}
                  amplitude={voice.amplitude}
                  amplitudeArray={voice.amplitudeArray}
                />
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
            data-mute-click
            onClick={() => sound.setEnabled(!sound.enabled)}
            className={`w-8 h-8 rounded-lg flex items-center justify-center transition-colors
                        ${sound.enabled ? 'text-[#7B2FFF] bg-[#7B2FFF]/10' : 'text-[#4A6080] hover:bg-white/5'}`}
            title={sound.enabled ? 'Mute sound effects' : 'Enable sound effects'}
          >
            {sound.enabled ? <Music size={15} /> : <VolumeX size={15} />}
          </motion.button>

          <motion.button
            whileTap={{ scale: 0.88 }}
            onClick={() => setTtsEnabled(!ttsEnabled)}
            className={`w-8 h-8 rounded-lg flex items-center justify-center transition-colors
                        ${ttsEnabled ? 'text-[#00D4FF] bg-[#00D4FF]/10' : 'text-[#4A6080] hover:bg-white/5'}`}
            title={ttsEnabled ? 'Disable voice replies' : 'Enable voice replies'}
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
              <OrbAnimation
                size={180}
                mode={orbMode}
                amplitude={voice.amplitude}
                amplitudeArray={voice.amplitudeArray}
              />

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
                {suggestions.map((s, i) => (
                  <motion.button
                    key={`${s}-${i}`}
                    data-mute-click
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.5 + i * 0.08 }}
                    whileHover={{ y: -2 }}
                    whileTap={{ scale: 0.97 }}
                    onClick={() => handleSend(s)}
                    className="px-4 py-2 glass-sm rounded-full text-sm text-[#94A3B8]
                               hover:text-[#E2E8F0] hover:border-[#00D4FF]/30 transition-all"
                  >
                    {s}
                  </motion.button>
                ))}
              </motion.div>
            </motion.div>
          ) : (
            <motion.div
              key="chat"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="h-full overflow-y-auto"
            >
              <div className="max-w-3xl mx-auto w-full px-2 sm:px-4 py-5">
                {messages.map(msg => (
                  <div key={msg.id}>
                    <MessageBubble
                      message={msg}
                      streaming={msg.streaming && streaming}
                      speaking={voice.isSpeaking && msg.role === 'assistant' && msg.id === lastAssistantId}
                    />
                    {/* Action cards belonging to this message */}
                    {actionCards.filter(c => c.messageId === msg.id).map(card => (
                      <motion.div
                        key={card.id}
                        initial={{ opacity: 0, y: 8, scale: 0.97 }}
                        animate={{ opacity: 1, y: 0, scale: 1 }}
                        transition={{ type: 'spring', stiffness: 300, damping: 24 }}
                        className="ml-14 mt-1 mb-2 flex items-center gap-2 px-3 py-2 rounded-xl
                                   bg-[rgba(0,212,255,0.06)] border border-[rgba(0,212,255,0.15)]
                                   text-xs text-[#94A3B8] w-fit max-w-sm"
                      >
                        <ActionIcon action={card.action} />
                        <ActionLabel action={card.action} payload={card.payload} />
                      </motion.div>
                    ))}
                  </div>
                ))}
                <div ref={messagesEndRef} className="h-2" />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Thinking banner (tool activity) */}
      <AnimatePresence>
        {thinkingMsg && (
          <motion.div
            key="thinking-banner"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            className="shrink-0 flex items-center gap-2 px-4 py-2 mx-4 mb-2 rounded-xl
                       bg-[rgba(123,47,255,0.08)] border border-[rgba(123,47,255,0.2)] max-w-3xl md:mx-auto"
          >
            <div className="flex gap-1">
              {[0, 1, 2].map(i => (
                <motion.div
                  key={i}
                  className="w-1.5 h-1.5 rounded-full bg-[#7B2FFF]"
                  animate={{ scale: [1, 1.4, 1], opacity: [0.4, 1, 0.4] }}
                  transition={{ duration: 0.8, repeat: Infinity, delay: i * 0.15 }}
                />
              ))}
            </div>
            <span className="text-[#7B2FFF] text-xs font-medium">{thinkingMsg}</span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Live transcript while listening */}
      <AnimatePresence>
        {voice.isListening && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            className="shrink-0 px-4 pb-1 max-w-3xl mx-auto w-full"
          >
            <div className="flex items-center gap-2 text-sm">
              <span className="relative flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full rounded-full bg-[#00D4FF] opacity-60 animate-ping" />
                <span className="relative inline-flex h-2 w-2 rounded-full bg-[#00D4FF]" />
              </span>
              <span className="text-[#4A6080]">Listening…</span>
              <span className="text-[#E2E8F0] truncate">{voice.transcript}</span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Input bar — floating elevated pill */}
      <div className="shrink-0 px-3 pt-1.5 pb-3">
        <div className="glass-strong rounded-[22px] flex items-end gap-2 px-3 py-2 max-w-3xl mx-auto
                        shadow-[0_8px_32px_rgba(0,0,0,0.45)] focus-within:border-[#00D4FF]/40
                        transition-colors"
             style={{ borderRadius: 22 }}>
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
            onStart={handleVoiceStart}
            onStop={voice.stopListening}
            disabled={streaming}
          />

          {/* Send */}
          <motion.button
            whileTap={{ scale: 0.88 }}
            data-mute-click
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
