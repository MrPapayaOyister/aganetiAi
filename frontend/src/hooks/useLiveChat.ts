import { useCallback, useRef, useState } from 'react'

/**
 * Hands-free voice conversation loop.
 *   listen (until silence) → onUtterance(text) [caller streams + speaks] → re-listen
 * The mic is naturally paused while the assistant replies (the loop awaits
 * onUtterance, which resolves only after TTS finishes), so the agent never hears
 * itself. Web Speech (Chrome/Edge) auto-finalizes an utterance on a brief pause.
 */
export function useLiveChat() {
  const [active, setActive] = useState(false)
  const [transcript, setTranscript] = useState('')
  const activeRef = useRef(false)
  const recogRef = useRef<any>(null)

  const hasWebSpeech = typeof window !== 'undefined' &&
    ('SpeechRecognition' in window || 'webkitSpeechRecognition' in window)

  // Listen for a single utterance; resolves with the final text ('' on silence/error).
  const listenOnce = useCallback(() => new Promise<string>((resolve) => {
    const SR = (window as any).SpeechRecognition ?? (window as any).webkitSpeechRecognition
    if (!SR) { resolve(''); return }
    const rec = new SR()
    recogRef.current = rec
    rec.continuous = false       // auto-ends after a short pause (built-in VAD)
    rec.interimResults = true
    rec.lang = 'en-US'
    let finalText = ''
    let settled = false
    const done = (t: string) => { if (!settled) { settled = true; setTranscript(''); resolve(t) } }
    rec.onresult = (e: any) => {
      let interim = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i]
        if (r.isFinal) finalText += r[0].transcript
        else interim += r[0].transcript
      }
      setTranscript((finalText + ' ' + interim).trim())
    }
    rec.onerror = () => done('')
    rec.onend = () => done(finalText.trim())
    try { rec.start() } catch { done('') }
  }), [])

  const start = useCallback((onUtterance: (text: string) => Promise<void>) => {
    if (!hasWebSpeech || activeRef.current) return false
    setActive(true); activeRef.current = true
    ;(async () => {
      while (activeRef.current) {
        const text = await listenOnce()
        if (!activeRef.current) break
        if (text.trim()) {
          try { await onUtterance(text.trim()) } catch { /* keep the loop alive */ }
        }
        await new Promise(r => setTimeout(r, 250))  // brief gap before re-listening
      }
    })()
    return true
  }, [hasWebSpeech, listenOnce])

  const stop = useCallback(() => {
    activeRef.current = false
    setActive(false)
    setTranscript('')
    try { recogRef.current?.stop() } catch { /* already stopped */ }
  }, [])

  return { active, transcript, start, stop, hasWebSpeech }
}
