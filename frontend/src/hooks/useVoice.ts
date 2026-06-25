import { useState, useRef, useCallback, useEffect } from 'react'
import { tts, stt } from '../api/client'

export type VoiceState = 'idle' | 'listening' | 'processing' | 'speaking'

const isIOS = typeof navigator !== 'undefined' &&
  (/iPad|iPhone|iPod/.test(navigator.userAgent) ||
   (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1))

export function useVoice() {
  const [state, setState] = useState<VoiceState>('idle')
  const [transcript, setTranscript] = useState('')        // live interim + final
  const [analyserNode, setAnalyserNode] = useState<AnalyserNode | null>(null)
  const [amplitude, setAmplitude] = useState(0)           // 0–1 scalar (RMS)
  const [amplitudeArray, setAmplitudeArray] = useState<number[]>([])

  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const audioSourceRef = useRef<AudioBufferSourceNode | null>(null)
  const recognitionRef = useRef<any>(null)
  const chunksRef = useRef<Blob[]>([])

  // shared analyser + RAF sampler (drives orb/particle amplitude from real audio)
  const analyserRef = useRef<AnalyserNode | null>(null)
  const rafRef = useRef<number>(0)
  const tickRef = useRef(0)

  // Use Web Speech only on capable, non-iOS browsers
  const hasWebSpeech = typeof window !== 'undefined' && !isIOS &&
    ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window)

  const ensureCtx = () => {
    if (!audioCtxRef.current || audioCtxRef.current.state === 'closed') {
      audioCtxRef.current = new (window.AudioContext || (window as any).webkitAudioContext)()
    }
    // iOS: resume on user gesture
    if (audioCtxRef.current.state === 'suspended') audioCtxRef.current.resume()
    return audioCtxRef.current
  }

  const startSampler = useCallback((analyser: AnalyserNode) => {
    analyserRef.current = analyser
    const freq = new Uint8Array(analyser.frequencyBinCount)
    const loop = () => {
      rafRef.current = requestAnimationFrame(loop)
      const a = analyserRef.current
      if (!a) return
      a.getByteFrequencyData(freq)
      // RMS-ish energy
      let sum = 0
      for (let i = 0; i < freq.length; i++) sum += freq[i] * freq[i]
      const rms = Math.sqrt(sum / freq.length) / 255
      setAmplitude(Math.min(1, rms * 1.8))
      // 24-bucket downsample (throttle array updates to ~30fps)
      if ((tickRef.current++ & 1) === 0) {
        const N = 24, step = Math.floor(freq.length / N) || 1
        const arr = new Array(N)
        for (let i = 0; i < N; i++) arr[i] = (freq[i * step] ?? 0) / 255
        setAmplitudeArray(arr)
      }
    }
    loop()
  }, [])

  const stopSampler = useCallback(() => {
    cancelAnimationFrame(rafRef.current)
    analyserRef.current = null
    setAmplitude(0)
    setAmplitudeArray([])
  }, [])

  useEffect(() => () => stopSampler(), [stopSampler])

  const startListening = useCallback(async (onResult: (text: string) => void) => {
    if (state !== 'idle') return
    setTranscript('')
    setState('listening')

    // ── Web Speech API path (live interim) ──
    if (hasWebSpeech) {
      const SR = (window as any).SpeechRecognition ?? (window as any).webkitSpeechRecognition
      const recognition = new SR()
      recognitionRef.current = recognition
      recognition.continuous = false
      recognition.interimResults = true     // ← live transcript
      recognition.lang = 'en-US'

      let finalText = ''
      recognition.onresult = (e: any) => {
        let interim = ''
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const r = e.results[i]
          if (r.isFinal) finalText += r[0].transcript
          else interim += r[0].transcript
        }
        setTranscript((finalText + ' ' + interim).trim())
      }
      recognition.onerror = () => { setState('idle'); stopSampler() }
      recognition.onend = () => {
        setState('idle'); stopSampler()
        const out = finalText.trim()
        if (out) onResult(out)
      }

      // Mic analyser for the live waveform + amplitude
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        const ctx = ensureCtx()
        const analyser = ctx.createAnalyser()
        analyser.fftSize = 256
        ctx.createMediaStreamSource(stream).connect(analyser)
        setAnalyserNode(analyser)
        startSampler(analyser)
        recognition.onend = () => {
          stream.getTracks().forEach(t => t.stop())
          setAnalyserNode(null); setState('idle'); stopSampler()
          const out = finalText.trim()
          if (out) onResult(out)
        }
      } catch { /* mic viz optional */ }

      recognition.start()
      return
    }

    // ── MediaRecorder → /stt fallback (iOS/Firefox) ──
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = ensureCtx()
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 256
      ctx.createMediaStreamSource(stream).connect(analyser)
      setAnalyserNode(analyser)
      startSampler(analyser)

      chunksRef.current = []
      const recorder = new MediaRecorder(stream)
      mediaRecorderRef.current = recorder
      recorder.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data) }
      recorder.onstop = async () => {
        stream.getTracks().forEach(t => t.stop())
        setAnalyserNode(null); stopSampler(); setState('processing')
        try {
          const blob = new Blob(chunksRef.current, { type: 'audio/webm' })
          const text = await stt(blob)
          setTranscript(text)
          if (text) onResult(text)
        } catch { /* ignore */ } finally { setState('idle') }
      }
      recorder.start()
    } catch {
      setState('idle'); stopSampler()
    }
  }, [state, hasWebSpeech, startSampler, stopSampler])

  const stopListening = useCallback(() => {
    recognitionRef.current?.stop()
    if (mediaRecorderRef.current?.state === 'recording') mediaRecorderRef.current.stop()
  }, [])

  const speak = useCallback(async (text: string) => {
    if (!text || text.length > 2000) return
    setState('speaking')
    try {
      const ctx = ensureCtx()
      const buffer = await tts(text)
      const audioBuffer = await ctx.decodeAudioData(buffer)
      const source = ctx.createBufferSource()
      audioSourceRef.current = source
      source.buffer = audioBuffer

      // analyser on the TTS chain → real speaking amplitude
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 256
      source.connect(analyser)
      analyser.connect(ctx.destination)
      setAnalyserNode(analyser)
      startSampler(analyser)

      source.start()
      await new Promise<void>(resolve => { source.onended = () => resolve() })
    } catch { /* TTS optional */ } finally {
      stopSampler(); setAnalyserNode(null); setState('idle')
    }
  }, [startSampler, stopSampler])

  const stopSpeaking = useCallback(() => {
    try { audioSourceRef.current?.stop() } catch { /* already stopped */ }
    audioSourceRef.current = null
    stopSampler(); setAnalyserNode(null); setState('idle')
  }, [stopSampler])

  return {
    state,
    transcript,
    analyserNode,
    amplitude,
    amplitudeArray,
    isListening: state === 'listening',
    isSpeaking: state === 'speaking',
    isProcessing: state === 'processing',
    startListening,
    stopListening,
    speak,
    stopSpeaking,
  }
}
