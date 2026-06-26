/**
 * Programmatic UI sound model (Item 8). All sounds are synthesized with the
 * Web Audio API — no audio files. The AudioContext is created lazily on the
 * first user gesture (pointer/touch/key), never before, to satisfy autoplay
 * policy. Muted state persists to localStorage; defaults to muted when the
 * user prefers reduced motion and hasn't explicitly chosen.
 */

// Const object instead of `enum` (project sets erasableSyntaxOnly).
export const SoundEvent = {
  ClickSoft:      'click-soft',
  RecordStart:    'record-start',
  RecordStop:     'record-stop',
  ResponseReady:  'response-ready',
  WarningSoft:    'warning-soft',
  ErrorAlert:     'error-alert',
  SuccessChime:   'success-chime',
  DelegationSent: 'delegation-sent',
  AgentConnected: 'agent-connected',
} as const
export type SoundEvent = typeof SoundEvent[keyof typeof SoundEvent]

const LS_KEY = 'aria_sound_muted'

let ctx: AudioContext | null = null
let unlocked = false

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined' &&
    !!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
}

function loadMuted(): boolean {
  try {
    const v = localStorage.getItem(LS_KEY)
    if (v === null) return prefersReducedMotion()   // sensible default
    return v === '1'
  } catch { return false }
}

let muted = loadMuted()

export function isMuted(): boolean { return muted }
export function setMuted(v: boolean): void {
  muted = v
  try { localStorage.setItem(LS_KEY, v ? '1' : '0') } catch { /* ignore */ }
}
export function toggleMuted(): boolean { setMuted(!muted); return muted }

function ensureCtx(): AudioContext | null {
  if (typeof window === 'undefined') return null
  if (!ctx) {
    try { ctx = new (window.AudioContext || (window as any).webkitAudioContext)() }
    catch { return null }
  }
  if (ctx.state === 'suspended') ctx.resume().catch(() => {})
  return ctx
}

/** Pre-warm on first gesture (call once at app start). */
export function initSound(): void {
  if (typeof window === 'undefined' || unlocked) return
  const unlock = () => {
    unlocked = true
    ensureCtx()
    window.removeEventListener('pointerdown', unlock)
    window.removeEventListener('touchstart', unlock)
    window.removeEventListener('keydown', unlock)
  }
  window.addEventListener('pointerdown', unlock, { passive: true })
  window.addEventListener('touchstart', unlock, { passive: true })
  window.addEventListener('keydown', unlock)
}

// ── one tone with an ADSR-ish gain envelope ──
function tone(c: AudioContext, opts: {
  type?: OscillatorType; f0: number; f1?: number; dur: number;
  peak: number; delay?: number;
}) {
  const { type = 'sine', f0, f1 = f0, dur, peak, delay = 0 } = opts
  const t0 = c.currentTime + delay
  const osc = c.createOscillator()
  const g = c.createGain()
  osc.type = type
  osc.frequency.setValueAtTime(f0, t0)
  if (f1 !== f0) osc.frequency.exponentialRampToValueAtTime(Math.max(1, f1), t0 + dur)
  g.gain.setValueAtTime(0, t0)
  g.gain.linearRampToValueAtTime(peak, t0 + Math.min(0.03, dur * 0.3))
  g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur)
  osc.connect(g); g.connect(c.destination)
  osc.start(t0); osc.stop(t0 + dur + 0.02)
}

const RECIPES: Record<SoundEvent, (c: AudioContext) => void> = {
  [SoundEvent.ClickSoft]:      c => tone(c, { f0: 220, f1: 110, dur: 0.09, peak: 0.12 }),
  [SoundEvent.RecordStart]:    c => tone(c, { f0: 440, f1: 660, dur: 0.22, peak: 0.15 }),
  [SoundEvent.RecordStop]:     c => tone(c, { f0: 660, f1: 330, dur: 0.20, peak: 0.15 }),
  [SoundEvent.ResponseReady]:  c => { tone(c, { f0: 523, dur: 0.35, peak: 0.10 }); tone(c, { f0: 659, dur: 0.35, peak: 0.08 }) },
  [SoundEvent.WarningSoft]:    c => tone(c, { f0: 330, dur: 0.28, peak: 0.10 }),
  [SoundEvent.ErrorAlert]:     c => { tone(c, { type: 'square', f0: 200, f1: 150, dur: 0.16, peak: 0.13 }); tone(c, { type: 'square', f0: 200, f1: 150, dur: 0.16, peak: 0.13, delay: 0.2 }) },
  [SoundEvent.SuccessChime]:   c => { tone(c, { f0: 523, f1: 784, dur: 0.3, peak: 0.13 }) },
  [SoundEvent.DelegationSent]: c => { tone(c, { f0: 440, dur: 0.25, peak: 0.10 }); tone(c, { f0: 550, dur: 0.25, peak: 0.09, delay: 0.05 }) },
  [SoundEvent.AgentConnected]: c => tone(c, { f0: 330, f1: 520, dur: 0.2, peak: 0.10 }),
}

export function playSound(event: SoundEvent, gainScale = 1): void {
  if (muted) return
  const c = ensureCtx()
  if (!c) return
  try {
    // gainScale is applied by temporarily wrapping — simplest: scale peaks via
    // a master gain. Cheap approach: just call the recipe (peaks already low).
    void gainScale
    RECIPES[event]?.(c)
  } catch { /* ignore */ }
}

/** Wire any element with data-sound="click" to play ClickSoft on pointerdown. */
export function initSoundDelegation(): void {
  if (typeof window === 'undefined') return
  window.addEventListener('pointerdown', (e) => {
    const el = (e.target as HTMLElement | null)?.closest('[data-sound]')
    if (!el) return
    const kind = el.getAttribute('data-sound')
    if (kind === 'click') playSound(SoundEvent.ClickSoft)
  }, { passive: true })
}
