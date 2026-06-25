import { useEffect, useRef } from 'react'
import PixelBlast from './PixelBlast'
import { useAgentField, type AgentFieldMode } from '../contexts/AgentFieldContext'
import { usePrefs, type BgIntensity } from '../contexts/PrefsContext'

interface PixelBlastHandle {
  triggerRipple: (nx?: number, ny?: number) => void
  setColor: (hex: string) => void
  setSpeed: (v: number) => void
}

interface ModeConfig {
  color: string
  patternDensity: number
  patternScale: number
  speed: number
  rippleIntensityScale: number
  rippleSpeed: number
  edgeFade: number
  pixelJitter: number
}

// Base values tuned for the masked composition: the IntelligenceCore mask
// already concentrates the field around the center and fades the edges, so
// patternDensity needs to be higher (mask multiplies down what's visible)
// and edgeFade is small (the mask provides the falloff).
const BASE: ModeConfig = {
  color: '#7AA2F7',
  patternDensity: 1.25,
  patternScale: 2.4,
  speed: 0.4,
  rippleIntensityScale: 1.2,
  rippleSpeed: 0.35,
  edgeFade: 0.10,
  pixelJitter: 0.35,
}

const MODE_MAP: Record<AgentFieldMode, Partial<ModeConfig>> = {
  idle:      { color: '#5A6F9F', patternDensity: 1.05, speed: 0.30 },
  listening: { color: '#00C8FF', patternDensity: 1.30, speed: 0.55, rippleIntensityScale: 1.5 },
  thinking:  { color: '#7B2FFF', patternDensity: 1.38, speed: 0.65, rippleIntensityScale: 1.7, rippleSpeed: 0.45 },
  speaking:  { color: '#00D4FF', patternDensity: 1.42, speed: 0.80, rippleIntensityScale: 1.9, rippleSpeed: 0.55 },
  acting:    { color: '#9D5BFF', patternDensity: 1.48, speed: 0.90, rippleIntensityScale: 2.1, rippleSpeed: 0.50 },
  success:   { color: '#00FFB3', patternDensity: 1.30, speed: 0.55, rippleIntensityScale: 2.2 },
  error:     { color: '#FF8A4A', patternDensity: 1.25, speed: 0.45, rippleIntensityScale: 1.8 },
}

// Intensity preset → density/speed multipliers
const INTENSITY_PRESETS: Record<BgIntensity, { density: number; speed: number; ripple: number }> = {
  off:       { density: 0,    speed: 0,    ripple: 0 },
  calm:      { density: 0.55, speed: 0.55, ripple: 0.6 },
  standard:  { density: 0.85, speed: 0.85, ripple: 1.0 },
  cinematic: { density: 1.1,  speed: 1.15, ripple: 1.25 },
}

function resolveConfig(mode: AgentFieldMode, amplitude: number, intensity: BgIntensity): ModeConfig {
  const overrides = MODE_MAP[mode] ?? {}
  const cfg: ModeConfig = { ...BASE, ...overrides }
  const preset = INTENSITY_PRESETS[intensity]
  if (mode === 'speaking' || mode === 'listening') {
    const amp = Math.max(0, Math.min(1, amplitude))
    cfg.patternDensity += amp * 0.15
    cfg.rippleIntensityScale += amp * 0.8
    cfg.speed += amp * 0.25
  }
  cfg.patternDensity *= preset.density
  cfg.speed *= preset.speed
  cfg.rippleIntensityScale *= preset.ripple
  return cfg
}

function usePrefersReducedMotion(): boolean {
  const ref = useRef(false)
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    ref.current = mq.matches
    const cb = (e: MediaQueryListEvent) => { ref.current = e.matches }
    mq.addEventListener?.('change', cb)
    return () => mq.removeEventListener?.('change', cb)
  }, [])
  return ref.current
}

interface AgentPixelFieldProps {
  /** Bridge window pointer clicks into the field as ripples. */
  bridgeClicks?: boolean
  className?: string
  style?: React.CSSProperties
}

export default function AgentPixelField({
  bridgeClicks = true,
  className,
  style,
}: AgentPixelFieldProps) {
  const { mode, amplitudeRef, lastPulse } = useAgentField()
  const { prefs } = usePrefs()
  const reducedMotion = usePrefersReducedMotion()
  const pixelRef = useRef<PixelBlastHandle | null>(null)
  const lastPulseIdRef = useRef<number>(-1)

  // Programmatic pulses from context.
  useEffect(() => {
    if (!lastPulse || lastPulse.id === lastPulseIdRef.current) return
    lastPulseIdRef.current = lastPulse.id
    const handle = pixelRef.current
    if (!handle) return
    handle.triggerRipple(lastPulse.nx, lastPulse.ny)
    if (lastPulse.intensity >= 1.5) {
      setTimeout(() => handle.triggerRipple(lastPulse.nx + 0.02, lastPulse.ny - 0.02), 90)
    }
  }, [lastPulse])

  // Window-level click/touch bridge — fires ripples at pointer position even
  // though the canvas is pointer-events:none (so the UI on top stays clickable).
  useEffect(() => {
    if (!bridgeClicks) return
    const onPointer = (e: PointerEvent) => {
      const handle = pixelRef.current
      if (!handle) return
      const nx = e.clientX / window.innerWidth
      const ny = e.clientY / window.innerHeight
      handle.triggerRipple(nx, ny)
    }
    window.addEventListener('pointerdown', onPointer, { passive: true })
    return () => window.removeEventListener('pointerdown', onPointer)
  }, [bridgeClicks])

  // RAF amplitude → uniform sampler (zero React re-renders, ref-diff gated).
  useEffect(() => {
    let raf = 0
    let lastSpeed = -1
    const tick = () => {
      raf = requestAnimationFrame(tick)
      const handle = pixelRef.current
      if (!handle) return
      const amp = amplitudeRef.current
      if (mode === 'speaking' || mode === 'listening') {
        const cfg = resolveConfig(mode, amp, prefs.bgIntensity)
        const finalSpeed = reducedMotion ? 0 : cfg.speed
        if (Math.abs(finalSpeed - lastSpeed) > 0.02) {
          lastSpeed = finalSpeed
          handle.setSpeed(finalSpeed)
        }
      }
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [mode, prefs.bgIntensity, amplitudeRef, reducedMotion])

  // Render — uniforms that DON'T trigger a WebGL re-init mutate in place.
  // pixelSize, liquid, antialias, noiseAmount are intentionally CONSTANT.
  const cfg = resolveConfig(mode, amplitudeRef.current, prefs.bgIntensity)
  const speed = reducedMotion ? 0 : cfg.speed
  const ripplesOn = !reducedMotion && prefs.bgIntensity !== 'off'

  // When intensity is 'off', mount a transparent placeholder (zero GPU).
  if (prefs.bgIntensity === 'off') {
    return <div className={className} style={style} aria-hidden />
  }

  return (
    <PixelBlast
      ref={pixelRef}
      variant="circle"
      pixelSize={4}                          /* CONSTANT — re-init avoided */
      color={cfg.color}
      patternScale={cfg.patternScale}
      patternDensity={cfg.patternDensity}
      pixelSizeJitter={cfg.pixelJitter}
      enableRipples={ripplesOn}
      rippleSpeed={cfg.rippleSpeed}
      rippleThickness={0.12}
      rippleIntensityScale={cfg.rippleIntensityScale}
      liquid={true}                          /* CONSTANT — re-init avoided */
      liquidStrength={prefs.bgIntensity === 'cinematic' ? 0.12 : 0.06}
      liquidRadius={1.1}
      liquidWobbleSpeed={4.5}
      speed={speed}
      edgeFade={cfg.edgeFade}
      transparent
      autoPauseOffscreen
      interactive={false}
      className={className}
      style={style}
    />
  )
}
