import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Synthesized UI sound effects via Web Audio — no asset files, works on plain
 * HTTP (AudioContext is allowed after the first user gesture). Tasteful and
 * subtle by design. Respects a persisted mute preference.
 */
export function useSound() {
  const ctxRef = useRef<AudioContext | null>(null)
  const [enabled, setEnabledState] = useState<boolean>(
    () => localStorage.getItem('aria_sound') !== 'false'
  )
  const enabledRef = useRef(enabled)
  useEffect(() => { enabledRef.current = enabled }, [enabled])

  const ctx = () => {
    if (!ctxRef.current) {
      const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
      try { ctxRef.current = new AC() } catch { return null }
    }
    if (ctxRef.current && ctxRef.current.state === 'suspended') ctxRef.current.resume()
    return ctxRef.current
  }

  /** One short enveloped tone. */
  const tone = useCallback((
    freq: number, dur: number,
    type: OscillatorType = 'sine', gain = 0.05, startAt = 0, glideTo?: number,
  ) => {
    if (!enabledRef.current) return
    const c = ctx()
    if (!c) return
    const t0 = c.currentTime + startAt
    const osc = c.createOscillator()
    const g = c.createGain()
    osc.type = type
    osc.frequency.setValueAtTime(freq, t0)
    if (glideTo) osc.frequency.exponentialRampToValueAtTime(glideTo, t0 + dur)
    osc.connect(g); g.connect(c.destination)
    g.gain.setValueAtTime(0.0001, t0)
    g.gain.exponentialRampToValueAtTime(gain, t0 + 0.012)
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur)
    osc.start(t0)
    osc.stop(t0 + dur + 0.02)
  }, [])

  // ── Public cues ───────────────────────────────────────────
  /** Soft tick for generic button presses. */
  const playClick = useCallback(() => tone(660, 0.05, 'triangle', 0.035), [tone])

  /** Rising two-note "whoosh" when the user sends a message. */
  const playSend = useCallback(() => {
    tone(420, 0.12, 'sine', 0.05, 0, 880)
    tone(660, 0.10, 'sine', 0.03, 0.04, 1100)
  }, [tone])

  /** Very subtle high tick while the assistant streams tokens (throttle caller-side). */
  const playTick = useCallback(() => tone(1500, 0.03, 'sine', 0.012), [tone])

  /** Gentle descending chime when a reply completes. */
  const playReceive = useCallback(() => {
    tone(880, 0.12, 'sine', 0.04, 0, 620)
    tone(1320, 0.10, 'sine', 0.02, 0.02)
  }, [tone])

  const setEnabled = useCallback((v: boolean) => {
    setEnabledState(v)
    localStorage.setItem('aria_sound', String(v))
    if (v) tone(880, 0.08, 'sine', 0.04) // confirmation blip when enabling
  }, [tone])

  return { enabled, setEnabled, playClick, playSend, playTick, playReceive }
}
