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
  /** Last recognition error type ('no-speech' | 'network' | 'not-allowed' | …).
   *  AssistantPage subscribes via a useEffect and shows a toast. */
  const [lastError, setLastError] = useState<string | null>(null)

  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const audioSourceRef = useRef<AudioBufferSourceNode | null>(null)
  const recognitionRef = useRef<any>(null)
  const chunksRef = useRef<Blob[]>([])

  // PERSISTENT audio chain — built once per AudioContext, never disconnected.
  // Eliminates the click/pop produced by connect/disconnect on every speak()
  // or listen() call. Topology:
  //   ttsSource → ttsGain ┐
  //   micStream → micSplit (via analyser) ┴→ analyser → destination
  // We swap inputs by stopping/starting source nodes, not by rewiring.
  const ttsGainRef = useRef<GainNode | null>(null)
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
    const ctx = audioCtxRef.current
    if (ctx.state === 'suspended') ctx.resume()
    // Build the persistent chain once: gain → analyser → destination.
    if (!ttsGainRef.current) {
      const gain = ctx.createGain()
      gain.gain.value = 1
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 256
      gain.connect(analyser)
      analyser.connect(ctx.destination)
      ttsGainRef.current = gain
      analyserRef.current = analyser
      setAnalyserNode(analyser)
      startSampler(analyser)
    }
    return ctx
  }

  const startSampler = useCallback((analyser: AnalyserNode) => {
    // Cancel any prior RAF loop before starting a new one — otherwise
    // each ensureCtx/startListening leaks a parallel loop that fights
    // for the same amplitude state, causing audio-visual jitter.
    cancelAnimationFrame(rafRef.current)
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
    if (state !== 'idle') {
      console.warn('[voice] startListening bailed — state is', state)
      return
    }
    setTranscript('')
    setLastError(null)
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
      let lastInterim = ''            // ← fallback when no isFinal fires on mobile
      let micStream: MediaStream | null = null
      let resultDelivered = false      // ← guard so onResult fires at most once

      const deliver = () => {
        if (resultDelivered) return
        const out = (finalText.trim() || lastInterim.trim())
        if (out) {
          resultDelivered = true
          console.info('[voice] delivering transcript:', JSON.stringify(out))
          onResult(out)
        } else {
          console.warn('[voice] no transcript captured (empty finalText AND interim)')
        }
      }

      recognition.onresult = (e: any) => {
        let interim = ''
        try {
          for (let i = e.resultIndex; i < e.results.length; i++) {
            const r = e.results[i]
            if (r.isFinal) finalText += r[0].transcript
            else interim += r[0].transcript
          }
        } catch (err) {
          console.warn('[voice] onresult parse error:', err)
        }
        if (interim) lastInterim = interim
        setTranscript((finalText + ' ' + interim).trim())
      }
      recognition.onerror = (ev: any) => {
        const err = ev?.error ?? 'unknown'
        console.warn('[voice] recognition.onerror:', err, ev?.message ?? '')
        setLastError(err)
        if (micStream) micStream.getTracks().forEach(t => t.stop())
        setState('idle'); stopSampler()
        // Even on error, try to deliver any captured interim — "no-speech"
        // and "aborted" sometimes fire AFTER words were already streamed.
        deliver()
      }
      recognition.onend = () => {
        console.info('[voice] recognition.onend, final=', JSON.stringify(finalText), 'interim=', JSON.stringify(lastInterim))
        if (micStream) micStream.getTracks().forEach(t => t.stop())
        setState('idle'); stopSampler()
        deliver()
      }

      // STEP 1: request mic permission FIRST. Chrome routes Web Speech
      // mic capture through the same permission grant that getUserMedia
      // uses — if we don't getUserMedia first, recognition can start but
      // capture nothing on cold-permission browsers. The await here
      // preserves the gesture context (browser permission popups extend
      // the gesture window).
      try {
        micStream = await navigator.mediaDevices.getUserMedia({ audio: true })
      } catch {
        // Permission denied or no mic device. Continue WITHOUT mic stream —
        // recognition.start() will trigger its own prompt as a fallback.
        micStream = null
      }

      // STEP 2: start recognition IMMEDIATELY after permission resolves,
      // still inside the gesture window. Do NOT add any awaits between
      // here and recognition.start() — ctx.resume() in particular has been
      // known to hang on Chrome Android and prevent recognition from
      // starting.
      try {
        recognition.start()
      } catch {
        if (micStream) micStream.getTracks().forEach(t => t.stop())
        setState('idle')
        return
      }

      // STEP 3: cosmetic analyser setup — non-blocking, errors ignored.
      if (micStream) {
        try {
          const ctx = ensureCtx()
          const analyser = ctx.createAnalyser()
          analyser.fftSize = 256
          ctx.createMediaStreamSource(micStream).connect(analyser)
          setAnalyserNode(analyser)
          startSampler(analyser)
        } catch { /* mic viz optional — recognition keeps working */ }
      }
      return
    }

    // ── MediaRecorder → /stt fallback (iOS/Firefox) ──
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = ensureCtx()
      if (ctx.state === 'suspended') await ctx.resume()
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

  // ── Trim leading/trailing silence from a decoded TTS buffer.
  // Higher threshold (0.015) catches Kokoro's quiet hiss too — the
  // 0.005 threshold left audible noise tails that produced clicks.
  const trimSilence = (ctx: AudioContext, buf: AudioBuffer, threshold = 0.015): AudioBuffer => {
    const ch = buf.getChannelData(0)
    let start = 0
    let end = ch.length - 1
    while (start < ch.length && Math.abs(ch[start]) < threshold) start++
    while (end > start && Math.abs(ch[end]) < threshold) end--
    if (start >= end) return buf
    const len = end - start + 1
    if (len === ch.length) return buf
    const out = ctx.createBuffer(buf.numberOfChannels, len, buf.sampleRate)
    for (let c = 0; c < buf.numberOfChannels; c++) {
      const src = buf.getChannelData(c)
      const dst = out.getChannelData(c)
      for (let i = 0; i < len; i++) dst[i] = src[start + i]
    }
    return out
  }

  const speak = useCallback(async (text: string) => {
    if (!text || text.length > 2000) return
    setState('speaking')
    try {
      const ctx = ensureCtx()                                          // builds persistent chain
      const persistentGain = ttsGainRef.current!
      const buffer = await tts(text)
      const decoded = await ctx.decodeAudioData(buffer)
      const audioBuffer = trimSilence(ctx, decoded)

      // Per-utterance gain envelope.  10ms attack + 18ms release prevents
      // any DC step from clicking when the buffer connects/disconnects.
      // This temporary gain feeds into the PERSISTENT analyser/destination
      // chain — we never disconnect from the destination, so no system-
      // level click fires at end-of-speech.
      const envGain = ctx.createGain()
      const ATTACK = 0.010
      const RELEASE = 0.018
      const t0 = ctx.currentTime + 0.020                               // 20ms scheduling lead
      const dur = audioBuffer.duration
      envGain.gain.setValueAtTime(0, t0)
      envGain.gain.linearRampToValueAtTime(1, t0 + ATTACK)
      envGain.gain.setValueAtTime(1, t0 + Math.max(ATTACK, dur - RELEASE))
      envGain.gain.linearRampToValueAtTime(0, t0 + dur)

      const source = ctx.createBufferSource()
      audioSourceRef.current = source
      source.buffer = audioBuffer
      source.connect(envGain)
      envGain.connect(persistentGain)                                  // join persistent chain

      source.start(t0)
      await new Promise<void>(resolve => {
        source.onended = () => {
          // Disconnect the per-utterance node AFTER the buffer is fully done.
          // Persistent gain/analyser/destination remain attached — no click.
          try { envGain.disconnect() } catch { /* ignore */ }
          resolve()
        }
      })
    } catch { /* TTS optional */ } finally {
      setState('idle')
    }
  }, [])

  // Public: pre-warm the AudioContext on first user gesture.
  // Mobile Safari silences the very first AudioContext output (the "unlock"
  // click) — calling this from a pointerdown handler swallows it silently
  // long before any TTS or mic capture fires.
  const unlock = useCallback(() => {
    const ctx = ensureCtx()
    if (ctx.state === 'suspended') ctx.resume().catch(() => {})
    // Play a 1-frame silent buffer to fully unlock on iOS WebKit.
    try {
      const silent = ctx.createBuffer(1, 1, 22050)
      const src = ctx.createBufferSource()
      src.buffer = silent
      src.connect(ctx.destination)
      src.start(0)
    } catch { /* ignore */ }
  }, [])

  const stopSpeaking = useCallback(() => {
    // Stop the source — onended fires and disconnects the per-utterance gain.
    // Do NOT touch the persistent analyser/destination chain or stop the
    // sampler; the orb keeps reading amplitude (which decays to 0 naturally
    // since there's no signal) and no click fires.
    try { audioSourceRef.current?.stop() } catch { /* already stopped */ }
    audioSourceRef.current = null
    setState('idle')
  }, [])

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
    unlock,
    lastError,
  }
}
