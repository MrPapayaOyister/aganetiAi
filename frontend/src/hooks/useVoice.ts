import { useState, useRef, useCallback } from 'react'
import { tts, stt } from '../api/client'

export type VoiceState = 'idle' | 'listening' | 'processing' | 'speaking'

export function useVoice() {
  const [state, setState] = useState<VoiceState>('idle')
  const [transcript, setTranscript] = useState('')
  const [analyserNode, setAnalyserNode] = useState<AnalyserNode | null>(null)

  const mediaRecorderRef = useRef<MediaRecorder | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const audioSourceRef = useRef<AudioBufferSourceNode | null>(null)
  const recognitionRef = useRef<any>(null)
  const chunksRef = useRef<Blob[]>([])

  const hasWebSpeech = typeof window !== 'undefined' &&
    ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window)

  const startListening = useCallback(async (onResult: (text: string) => void) => {
    if (state !== 'idle') return
    setTranscript('')
    setState('listening')

    if (hasWebSpeech) {
      const SpeechRecognition = (window as any).SpeechRecognition ?? (window as any).webkitSpeechRecognition
      const recognition = new SpeechRecognition()
      recognitionRef.current = recognition
      recognition.continuous = false
      recognition.interimResults = false
      recognition.lang = 'en-US'

      recognition.onresult = (e: any) => {
        const text = e.results[0][0].transcript
        setTranscript(text)
        setState('idle')
        onResult(text)
      }
      recognition.onerror = () => setState('idle')
      recognition.onend = () => { setState('idle') }
      recognition.start()
      return
    }

    // Fallback: MediaRecorder → /stt
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })

      // Setup analyser for waveform
      if (!audioCtxRef.current || audioCtxRef.current.state === 'closed') {
        audioCtxRef.current = new AudioContext()
      }
      const analyser = audioCtxRef.current.createAnalyser()
      analyser.fftSize = 256
      const source = audioCtxRef.current.createMediaStreamSource(stream)
      source.connect(analyser)
      setAnalyserNode(analyser)

      chunksRef.current = []
      const recorder = new MediaRecorder(stream)
      mediaRecorderRef.current = recorder

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data)
      }
      recorder.onstop = async () => {
        stream.getTracks().forEach(t => t.stop())
        setAnalyserNode(null)
        setState('processing')
        try {
          const blob = new Blob(chunksRef.current, { type: 'audio/webm' })
          const text = await stt(blob)
          setTranscript(text)
          onResult(text)
        } catch { /* ignore */ } finally {
          setState('idle')
        }
      }
      recorder.start()
    } catch {
      setState('idle')
    }
  }, [state, hasWebSpeech])

  const stopListening = useCallback(() => {
    recognitionRef.current?.stop()
    if (mediaRecorderRef.current?.state === 'recording') {
      mediaRecorderRef.current.stop()
    }
  }, [])

  const speak = useCallback(async (text: string) => {
    if (!text || text.length > 2000) return
    setState('speaking')
    try {
      if (!audioCtxRef.current || audioCtxRef.current.state === 'closed') {
        audioCtxRef.current = new AudioContext()
      }
      const buffer = await tts(text)
      const audioBuffer = await audioCtxRef.current.decodeAudioData(buffer)
      const source = audioCtxRef.current.createBufferSource()
      audioSourceRef.current = source
      source.buffer = audioBuffer
      source.connect(audioCtxRef.current.destination)
      source.start()
      await new Promise<void>(resolve => { source.onended = () => resolve() })
    } catch { /* ignore TTS errors */ } finally {
      setState('idle')
    }
  }, [])

  const stopSpeaking = useCallback(() => {
    audioSourceRef.current?.stop()
    audioSourceRef.current = null
    setState('idle')
  }, [])

  return {
    state,
    transcript,
    analyserNode,
    isListening: state === 'listening',
    isSpeaking: state === 'speaking',
    isProcessing: state === 'processing',
    startListening,
    stopListening,
    speak,
    stopSpeaking,
  }
}
