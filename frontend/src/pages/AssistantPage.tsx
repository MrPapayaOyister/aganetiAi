import { useState, useRef, useCallback, useEffect } from 'react'
import { generateId } from '../utils/uuid'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Send, Paperclip, Volume2, VolumeX, StopCircle, Zap, Music,
  CheckCircle2, RefreshCw, Mail, SendHorizontal, Calendar, Clock, Brain, Radio, X, Network,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { type OrbMode } from '../components/OrbAnimation'
import IntelligenceOrb from '../components/IntelligenceOrb'
import { ConversationStream, type ChatMessage } from '../components/ConversationStream'
import { VoiceButton } from '../components/VoiceButton'
import { ConversationMenu } from '../components/ConversationMenu'
import { useConversations } from '../hooks/useConversations'
import { useStream } from '../hooks/useStream'
import { useVoice } from '../hooks/useVoice'
import { useLiveChat } from '../hooks/useLiveChat'
import { useInitiatives } from '../hooks/useInitiatives'
import { DelegationDock } from '../components/DelegationDock'
import { AgentNetworkRail } from '../components/AgentNetworkRail'
import { PressChip } from '../components/ui/PressChip'
import { playSound, SoundEvent } from '../lib/sound'
import { useSound } from '../hooks/useSound'
import { useToast } from '../hooks/useToast'
import { useAppContext } from '../App'
import { useAmbient } from '../contexts/AmbientContext'
import { useAgentField, type AgentFieldMode } from '../contexts/AgentFieldContext'
import { usePrefs } from '../contexts/PrefsContext'
import axios from 'axios'

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
  const { prefs } = usePrefs()
  const agentName = prefs.agentName || 'Aria'
  const { setAmbient } = useAmbient()
  const agentField = useAgentField()
  const { stream, streaming, abort } = useStream()
  const voice = useVoice()
  const live = useLiveChat()
  const sound = useSound()
  const lastTickRef = useRef(0)

  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [thinkingMsg, setThinkingMsg] = useState<string | null>(null)
  const [actionCards, setActionCards] = useState<ActionCard[]>([])
  const [sourcesByMsg, setSourcesByMsg] = useState<Record<string, { source: string }[]>>({})
  const [errorFlash, setErrorFlash] = useState(false)
  const [networkOpen, setNetworkOpen] = useState(false)
  const convos = useConversations(userId)
  const sessionId = convos.activeId

  // P3 — proactive: poll the initiative queue and inject Aria-initiated
  // messages into the conversation (amber-styled), plus a toast + field pulse.
  useInitiatives(userId, (items) => {
    setMessages(prev => {
      const existing = new Set(prev.map(m => m.initiativeId).filter(Boolean))
      const fresh = items
        .filter(i => !existing.has(i.id))
        .map(i => ({
          id: generateId(),
          role: 'assistant' as const,
          content: i.body,
          proactive: true,
          initiativeId: i.id,
          category: i.category,
        }))
      if (!fresh.length) return prev
      return [...prev, ...fresh]
    })
    if (items[0]) {
      addToast(items[0].title, 'info')
      agentField.setMode('acting', 0.8)
      agentField.pulse(0.5, 0.5, 1.4)
      playSound(SoundEvent.ResponseReady)   // soft "tap on the shoulder"
      setTimeout(() => agentField.setMode('idle', 0), 1500)
    }
  })

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const fullReplyRef = useRef('')
  const liveRef = useRef(false)

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
    : (live.active)                                   ? 'listening'  // live: awaiting speech
    : 'idle'

  // Drive the global particle field. Keyed on the discrete phase only (NOT the
  // per-frame voice amplitude) so we don't re-render the app shell at 60fps.
  useEffect(() => {
    const amp = orbMode === 'thinking' ? 0.8 : orbMode === 'speaking' ? 0.7
      : orbMode === 'listening' ? 0.6 : 0
    setAmbient(orbMode === 'error' ? 'idle' : orbMode, amp)
    agentField.setMode(orbMode as AgentFieldMode, amp)
  }, [orbMode, setAmbient, agentField])

  // RAF: pipe live voice amplitude into the field's ref (zero React re-renders).
  useEffect(() => {
    let raf = 0
    const tick = () => {
      raf = requestAnimationFrame(tick)
      agentField.setAmplitude(voice.amplitude ?? 0)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [voice, agentField])

  // Reset both fields to idle when leaving the page
  useEffect(() => () => {
    setAmbient('idle', 0)
    agentField.setMode('idle', 0)
  }, [setAmbient, agentField])

  // Live chat: richer particles — emit ambient ripples at random points
  // every ~900ms so the field feels alive while waiting for speech.
  useEffect(() => {
    if (!live.active) return
    const id = setInterval(() => {
      const nx = 0.25 + Math.random() * 0.5
      const ny = 0.35 + Math.random() * 0.4
      agentField.pulse(nx, ny, 0.9)
    }, 900)
    return () => clearInterval(id)
  }, [live.active, agentField])

  // Load the active conversation's messages on switch: localStorage first,
  // backend /chat/history as a fallback.
  useEffect(() => {
    let cancelled = false
    let local: ChatMessage[] = []
    try { local = JSON.parse(localStorage.getItem(`aria_msgs_${sessionId}`) || '[]') } catch { /* noop */ }
    if (local.length) { setMessages(local); return }
    setMessages([])
    fetch(`/api/chat/history?session_id=${encodeURIComponent(sessionId)}&limit=20`)
      .then(r => r.json())
      .then(data => {
        if (!cancelled && Array.isArray(data.messages) && data.messages.length > 0) {
          setMessages(data.messages.map((m: { role: string; content: string }) => ({
            id: generateId(),
            role: m.role === 'assistant' ? 'assistant' : 'user',
            content: m.content,
            streaming: false,
          })))
        }
      })
      .catch(() => { /* fresh session */ })
    return () => { cancelled = true }
  }, [sessionId])

  // Persist messages for the active conversation (skip while streaming).
  useEffect(() => {
    if (!streaming && messages.length) {
      try { localStorage.setItem(`aria_msgs_${sessionId}`, JSON.stringify(messages)) } catch { /* quota */ }
    }
  }, [messages, streaming, sessionId])

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

  // AudioContext pre-warm: unlock on the very first pointer gesture so
  // the browser's "first unlock" click is swallowed silently long before
  // any TTS or mic stream attaches.
  useEffect(() => {
    let unlocked = false
    const onceUnlock = () => {
      if (unlocked) return
      unlocked = true
      try { voice.unlock() } catch { /* ignore */ }
      window.removeEventListener('pointerdown', onceUnlock)
      window.removeEventListener('touchstart', onceUnlock)
      window.removeEventListener('keydown', onceUnlock)
    }
    window.addEventListener('pointerdown', onceUnlock, { passive: true })
    window.addEventListener('touchstart', onceUnlock, { passive: true })
    window.addEventListener('keydown', onceUnlock)
    return () => {
      window.removeEventListener('pointerdown', onceUnlock)
      window.removeEventListener('touchstart', onceUnlock)
      window.removeEventListener('keydown', onceUnlock)
    }
  }, [voice])

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

  // Pulse-target lookup → find a DOM anchor for per-tool ripples (e.g. inbox icon).
  const pulseAt = useCallback((selector: string | null, fallback: [number, number], intensity = 1.4) => {
    if (selector) {
      const el = document.querySelector(selector) as HTMLElement | null
      if (el) {
        const r = el.getBoundingClientRect()
        const nx = (r.left + r.width / 2) / window.innerWidth
        const ny = (r.top + r.height / 2) / window.innerHeight
        agentField.pulse(nx, ny, intensity)
        return
      }
    }
    agentField.pulse(fallback[0], fallback[1], intensity)
  }, [agentField])

  // Per-tool ripple coordinate map (anchor → fallback nx,ny).
  const toolPulse = useCallback((action: string) => {
    switch (action) {
      case 'reminder_set':
        // Toast region (top-right)
        pulseAt(null, [0.92, 0.10], 1.7); break
      case 'email_drafted':
      case 'email_sent':
        // Drafts/Inbox icons in the sidebar
        pulseAt('[data-pulse-target="/drafts"], [data-pulse-target="/inbox"]', [0.04, 0.45], 1.7); break
      case 'event_created':
        // Toward analytics/agenda
        pulseAt('[data-pulse-target="/analytics"]', [0.04, 0.30], 1.6); break
      case 'task_created':
      case 'task_updated':
        pulseAt(null, [0.5, 0.55], 1.5); break
      case 'memory_saved':
        pulseAt(null, [0.20, 0.85], 1.4); break
      default:
        pulseAt(null, [0.5, 0.55], 1.4)
    }
  }, [pulseAt])

  // Sentence-streaming TTS queue. Speaks each sentence as soon as it arrives
  // — text and voice come together, with no waiting for the full reply.
  const ttsQueueRef = useRef<string[]>([])
  const ttsActiveRef = useRef(false)
  const ttsBufferRef = useRef('')

  const drainTtsQueue = useCallback(async () => {
    if (ttsActiveRef.current) return
    ttsActiveRef.current = true
    try {
      while (ttsQueueRef.current.length) {
        const next = ttsQueueRef.current.shift()!
        await voice.speak(next)
      }
    } finally { ttsActiveRef.current = false }
  }, [voice])

  const flushTtsSentences = useCallback((force = false) => {
    const buf = ttsBufferRef.current
    if (!buf) return
    if (force) {
      const clean = buf.replace(/[*_`#>[\]()]/g, '').trim()
      if (clean) { ttsQueueRef.current.push(clean); drainTtsQueue() }
      ttsBufferRef.current = ''
      return
    }
    // Split on sentence boundaries (., !, ?, newline)
    const parts: string[] = []
    let rest = buf
    const re = /[^.!?\n]+[.!?\n]+/g
    let m: RegExpExecArray | null
    let lastIdx = 0
    while ((m = re.exec(buf)) !== null) {
      parts.push(m[0])
      lastIdx = re.lastIndex
    }
    rest = buf.slice(lastIdx)
    ttsBufferRef.current = rest
    for (const p of parts) {
      const clean = p.replace(/[*_`#>[\]()]/g, '').trim()
      if (clean) ttsQueueRef.current.push(clean)
    }
    if (ttsQueueRef.current.length) drainTtsQueue()
  }, [drainTtsQueue])

  const stopAllVoice = useCallback(() => {
    ttsQueueRef.current = []
    ttsBufferRef.current = ''
    voice.stopSpeaking()
  }, [voice])

  const handleSend = useCallback(async (text: string) => {
    if (!text.trim()) return
    // Allow interrupt while streaming or speaking — abort prior turn first.
    if (streaming) abort()
    stopAllVoice()

    const userMsg: ChatMessage = { id: generateId(), role: 'user', content: text }
    const assistantId = generateId()
    const assistantMsg: ChatMessage = { id: assistantId, role: 'assistant', content: '', streaming: true }

    fullReplyRef.current = ''
    setMessages(prev => [...prev, userMsg, assistantMsg])
    setInput('')
    convos.touch(sessionId, text)
    sound.playSend()

    const speakLive = ttsEnabled || liveRef.current

    await stream(text, sessionId, userId, {
      onToken: (t) => {
        setThinkingMsg(null)
        const now = performance.now()
        if (now - lastTickRef.current > 110) { lastTickRef.current = now; sound.playTick() }
        fullReplyRef.current += t
        setMessages(prev => prev.map(m =>
          m.id === assistantId ? { ...m, content: m.content + t } : m
        ))
        if (speakLive) {
          ttsBufferRef.current += t
          flushTtsSentences(false)
        }
      },
      onThinking: (msg) => setThinkingMsg(msg),
      onAction: (action, payload) => {
        setActionCards(prev => {
          const key = `${action}|${JSON.stringify(payload)}`
          if (prev.some(c => c.messageId === assistantId &&
                             `${c.action}|${JSON.stringify(c.payload)}` === key))
            return prev
          return [...prev, { id: generateId(), action, payload, messageId: assistantId }]
        })
        agentField.setMode('acting', 0.9)
        toolPulse(action)
      },
      onSources: (srcs) => {
        if (srcs.length) setSourcesByMsg(prev => ({ ...prev, [assistantId]: srcs }))
      },
      onError: (msg) => {
        addToast(msg, 'error')
        setErrorFlash(true)
        agentField.setMode('error', 0.7)
        agentField.pulse(0.5, 0.55, 1.4)
        setTimeout(() => setErrorFlash(false), 800)
      },
      onDone: async () => {
        setThinkingMsg(null)
        sound.playReceive()
        setMessages(prev => prev.map(m =>
          m.id === assistantId ? { ...m, streaming: false } : m
        ))
        agentField.setMode('success', 0.6)
        agentField.pulse(0.5, 0.5, 1.6)
        setTimeout(() => agentField.setMode('idle', 0), 1200)
        if (speakLive) flushTtsSentences(true)
      },
    })
  }, [
    streaming, abort, stopAllVoice, stream, sessionId, userId, ttsEnabled,
    addToast, sound, agentField, toolPulse, flushTtsSentences, convos,
  ])

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

  // Surface recognition errors as user-visible toasts (was silently dying).
  useEffect(() => {
    if (!voice.lastError) return
    const messages: Record<string, string> = {
      'no-speech':         "Didn't catch that — try again",
      'aborted':           '',                                  // user cancelled, no toast
      'audio-capture':     "Microphone not available",
      'not-allowed':       'Please allow microphone access',
      'service-not-allowed': 'Speech recognition is blocked on this network',
      'network':           'Speech recognition needs an internet connection',
      'no-match':          "Didn't catch that — try again",
      'bad-grammar':       'Speech recognition error',
      'language-not-supported': 'Language not supported',
    }
    const msg = messages[voice.lastError] ?? `Voice error: ${voice.lastError}`
    if (msg) addToast(msg, 'info')
  }, [voice.lastError, addToast])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend(input)
    }
  }

  // Live conversation toggle.
  const toggleLive = useCallback(() => {
    if (live.active) { liveRef.current = false; live.stop(); return }
    if (!window.isSecureContext) { addToast('Live chat needs a secure (HTTPS) connection', 'info'); return }
    if (!live.hasWebSpeech) { addToast('Live chat needs Chrome or Edge', 'info'); return }
    liveRef.current = true
    live.start(async (text) => { await handleSend(text) })
  }, [live, addToast, handleSend])

  // Regenerate: re-send the user message that preceded this assistant reply.
  const regenerate = (assistantId: string) => {
    const idx = messages.findIndex(m => m.id === assistantId)
    for (let i = idx - 1; i >= 0; i--) {
      if (messages[i].role === 'user') { handleSend(messages[i].content); return }
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
                <IntelligenceOrb
                  size={40}
                  mode={orbMode as OrbMode}
                  amplitude={voice.amplitude}
                  analyserNode={voice.analyserNode}
                />
              </motion.div>
            ) : (
              <motion.div key="icon" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                <Zap size={20} className="text-[#00D4FF]" />
              </motion.div>
            )}
          </AnimatePresence>
          <span className="font-semibold text-[#E2E8F0] text-sm">{agentName}</span>
          {streaming && !thinkingMsg && (
            <div className="flex items-center gap-1">
              {[0, 1, 2].map(i => (
                <motion.span
                  key={i}
                  className="w-1 h-1 rounded-full bg-[#4A6080]"
                  animate={{ opacity: [0.3, 1, 0.3] }}
                  transition={{ duration: 0.9, repeat: Infinity, delay: i * 0.2 }}
                />
              ))}
            </div>
          )}
          {voice.isSpeaking && (
            <motion.span
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="text-xs text-[#7B2FFF]/80"
            >
              Speaking…
            </motion.span>
          )}
        </div>

        <div className="flex items-center gap-1.5">
          <ConversationMenu
            sessions={convos.sessions}
            activeId={convos.activeId}
            onNew={convos.newConversation}
            onSwitch={convos.switchTo}
            onDelete={convos.remove}
          />
          <motion.button
            whileTap={{ scale: 0.88 }}
            onClick={() => setNetworkOpen(true)}
            className="w-8 h-8 rounded-lg flex items-center justify-center transition-colors
                       text-[#4A6080] hover:text-[#00D4FF] hover:bg-white/5"
            title="Agent network"
          >
            <Network size={15} />
          </motion.button>
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

          {(streaming || voice.isSpeaking) && (
            <motion.button
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              whileTap={{ scale: 0.88 }}
              onClick={() => { abort(); stopAllVoice() }}
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
              <IntelligenceOrb
                size={220}
                mode={orbMode as OrbMode}
                amplitude={voice.amplitude}
                analyserNode={voice.analyserNode}
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
                Ask anything. {agentName} handles the rest.
              </motion.p>

              <motion.div
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.5 }}
                className="flex flex-wrap justify-center gap-2 mt-8 max-w-sm"
              >
                {suggestions.map((s, i) => (
                  <motion.div
                    key={`${s}-${i}`}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.5 + i * 0.08, duration: 0.46, ease: [0.16, 1, 0.3, 1] }}
                  >
                    <PressChip data-mute-click onClick={() => handleSend(s)}>{s}</PressChip>
                  </motion.div>
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
              <ConversationStream
                messages={messages}
                streaming={streaming}
                speakingMessageId={voice.isSpeaking ? lastAssistantId : null}
                sourcesByMsg={sourcesByMsg}
                agentMode={orbMode}
                onPlay={(t) => voice.speak(t.replace(/[*_`#>[\]()]/g, '').slice(0, 600))}
                onRegenerate={(id) => regenerate(id)}
                renderActionCards={(messageId) => {
                  // Item 3 — staggered skew-unroll: each card unrolls from the
                  // assistant's reasoning (skewY + lift + scale), 80ms apart.
                  const cards = actionCards.filter(c => c.messageId === messageId)
                  return (
                    <>
                      {cards.map((card, i) => (
                        <motion.div
                          key={card.id}
                          initial={{ opacity: 0, y: 12, scale: 0.97, skewY: -1.5 }}
                          animate={{ opacity: 1, y: 0, scale: 1, skewY: 0 }}
                          transition={{ duration: 0.46, ease: [0.16, 1, 0.3, 1], delay: i * 0.08 }}
                          className="press group/ac flex items-center gap-2.5 px-3.5 py-2.5 rounded-xl w-fit max-w-sm
                                     neu cursor-default
                                     hover:-translate-y-px"
                          style={{ borderLeft: '2px solid rgba(0,212,255,0.5)' }}
                        >
                          <span className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0
                                           bg-[#00D4FF]/12 border border-[#00D4FF]/25
                                           transition-colors group-hover/ac:bg-[#00D4FF]/20">
                            <ActionIcon action={card.action} />
                          </span>
                          <span className="text-xs text-[#C8D3E5] font-medium">
                            <ActionLabel action={card.action} payload={card.payload} />
                          </span>
                          <CheckCircle2 size={13} className="text-[#00FF88] shrink-0 ml-1" />
                        </motion.div>
                      ))}
                    </>
                  )
                }}
              />
              <div ref={messagesEndRef} className="h-2" />
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

      {/* Live delegation capsules (Item 1) */}
      <DelegationDock userId={userId} />

      {/* Agent network rail (Item 2) — dismissible orchestration topology */}
      <AgentNetworkRail userId={userId} open={networkOpen} onClose={() => setNetworkOpen(false)} />

      {/* Input bar — premium glass dock at the bottom */}
      <div className="shrink-0 px-3 pt-1.5 pb-3">
        <div className="composer-dock rounded-[22px] flex items-center gap-1.5 px-2 py-2 max-w-3xl mx-auto
                        focus-within:shadow-[0_18px_50px_rgba(0,0,0,0.65),0_0_0_1px_rgba(0,212,255,0.25)]
                        transition-shadow"
             style={{ borderRadius: 22 }}>
          {/* Attach */}
          <motion.button
            whileTap={{ scale: 0.88 }}
            onClick={() => fileInputRef.current?.click()}
            className="shrink-0 size-10 flex items-center justify-center
                       rounded-xl text-[#4A6080] hover:text-[#00D4FF] hover:bg-white/[0.04] transition-colors"
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
            placeholder={`Ask ${agentName} anything…`}
            rows={1}
            disabled={voice.isListening}
            className="flex-1 bg-transparent resize-none outline-none text-sm text-[#E2E8F0]
                       placeholder:text-[#4A6080] leading-relaxed py-2 max-h-32 overflow-y-auto"
            onInput={e => {
              const el = e.currentTarget
              el.style.height = 'auto'
              el.style.height = Math.min(el.scrollHeight, 128) + 'px'
            }}
          />

          {/* Live conversation toggle */}
          <motion.button
            whileTap={{ scale: 0.88 }}
            onClick={toggleLive}
            title="Live conversation"
            className={`shrink-0 size-10 rounded-xl flex items-center justify-center transition-all
                        ${live.active ? 'text-[#00FF88] bg-[#00FF88]/10' : 'text-[#5C6B85] hover:text-[#38DBFF] hover:bg-white/[0.04]'}`}
          >
            <Radio size={16} />
          </motion.button>

          {/* Voice */}
          <VoiceButton
            voiceState={voice.state}
            analyserNode={voice.analyserNode}
            onStart={handleVoiceStart}
            onStop={voice.stopListening}
            disabled={streaming || live.active}
          />

          {/* Send */}
          <motion.button
            whileTap={{ scale: 0.88 }}
            data-mute-click
            onClick={() => handleSend(input)}
            disabled={!input.trim()}
            className="shrink-0 size-10 rounded-xl flex items-center justify-center
                       bg-[#00D4FF]/15 border border-[#00D4FF]/30 text-[#00D4FF]
                       hover:bg-[#00D4FF]/25 disabled:opacity-30 disabled:cursor-not-allowed
                       transition-all"
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

      {/* ── Live conversation overlay ── */}
      <AnimatePresence>
        {live.active && (
          <motion.div
            initial={{ opacity: 0, backdropFilter: 'blur(0px)' }}
            animate={{
              opacity: 1,
              // Background breathes with voice state: deeper blur while
              // listening (we recede), sharper while speaking (Aria steps forward).
              backdropFilter: voice.isSpeaking ? 'blur(3px)'
                : voice.isListening ? 'blur(9px)' : 'blur(6px)',
              backgroundColor: voice.isSpeaking ? 'rgba(18,21,30,0.80)'
                : 'rgba(18,21,30,0.88)',
            }}
            exit={{ opacity: 0, backdropFilter: 'blur(0px)' }}
            transition={{ type: 'spring', stiffness: 70, damping: 22 }}
            className="fixed inset-0 z-[120] flex flex-col items-center justify-center gap-8 px-6"
          >
            {/* Live overlay: orb scales UP visibly when speaking so the
                "alive" state reads at glance. Softer spring = organic, not bouncy. */}
            <motion.div
              animate={{
                scale: voice.isSpeaking ? 1.18
                  : voice.isListening ? 1.08
                  : streaming ? 1.05
                  : 1.0,
              }}
              transition={{ type: 'spring', stiffness: 60, damping: 24, delay: 0.05 }}
            >
              <IntelligenceOrb
                size={300}
                mode={orbMode as OrbMode}
                amplitude={voice.amplitude}
                analyserNode={voice.analyserNode}
              />
            </motion.div>
            <div className="text-center max-w-lg">
              <div className="t-label mb-2 flex items-center justify-center gap-2">
                <span className="relative flex h-2 w-2">
                  <span className="absolute inline-flex h-full w-full rounded-full bg-[#00FF88] opacity-60 animate-ping" />
                  <span className="relative inline-flex h-2 w-2 rounded-full bg-[#00FF88]" />
                </span>
                {voice.isSpeaking ? 'Aria is speaking…' : streaming ? 'Thinking…' : 'Listening…'}
              </div>
              <p className="t-body text-[var(--text-primary)] min-h-[1.5em]">
                {live.transcript || (streaming || voice.isSpeaking ? '' : 'Say something…')}
              </p>
            </div>
            <button
              onClick={toggleLive}
              className="neu-pill px-5 h-11 flex items-center gap-2 text-[#FF4466] t-label"
            >
              <X size={16} /> End live chat
            </button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
