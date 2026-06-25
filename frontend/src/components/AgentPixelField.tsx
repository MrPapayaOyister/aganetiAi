import { useEffect, useRef } from 'react'
import PixelBlast from './PixelBlast'
import { useAgentField, type AgentFieldMode } from '../contexts/AgentFieldContext'

interface PixelBlastHandle {
  triggerRipple: (nx?: number, ny?: number) => void
  setColor: (hex: string) => void
  setSpeed: (v: number) => void
}

/**
 * Maps assistant mode → PixelBlast visual params.
 * The shader takes these as live-updated props (the `else` branch of the
 * PixelBlast effect mutates uniforms in place without re-init).
 */
interface ModeConfig {
  color: string                   // pixel tint
  patternDensity: number          // shader density bias
  patternScale: number            // noise scale (lower = bigger blobs)
  speed: number                   // animation time scale
  rippleIntensityScale: number    // ripple punch
  rippleSpeed: number             // ring propagation
  edgeFade: number                // vignette
  pixelJitter: number             // coverage variance
}

const BASE: ModeConfig = {
  color: '#5A4FCF',
  patternDensity: 1.0,
  patternScale: 3,
  speed: 0.4,
  rippleIntensityScale: 1.2,
  rippleSpeed: 0.35,
  edgeFade: 0.35,
  pixelJitter: 0.35,
}

const MODE_MAP: Record<AgentFieldMode, Partial<ModeConfig>> = {
  idle:      { color: '#5A4FCF', patternDensity: 0.92, speed: 0.35 },
  listening: { color: '#00C8FF', patternDensity: 1.05, speed: 0.50, rippleIntensityScale: 1.4 },
  thinking:  { color: '#7B2FFF', patternDensity: 1.15, speed: 0.65, rippleIntensityScale: 1.6, rippleSpeed: 0.45 },
  speaking:  { color: '#00D4FF', patternDensity: 1.18, speed: 0.75, rippleIntensityScale: 1.8, rippleSpeed: 0.55 },
  acting:    { color: '#7B2FFF', patternDensity: 1.22, speed: 0.85, rippleIntensityScale: 2.0, rippleSpeed: 0.50 },
  success:   { color: '#00FFB3', patternDensity: 1.10, speed: 0.55, rippleIntensityScale: 2.2 },
  error:     { color: '#FF8A4A', patternDensity: 1.05, speed: 0.45, rippleIntensityScale: 1.8 },
}

function resolveConfig(mode: AgentFieldMode, amplitude: number): ModeConfig {
  const overrides = MODE_MAP[mode] ?? {}
  const cfg: ModeConfig = { ...BASE, ...overrides }
  // Voice-amplitude pushes speaking and listening visuals harder.
  if (mode === 'speaking' || mode === 'listening') {
    const amp = Math.max(0, Math.min(1, amplitude))
    cfg.patternDensity += amp * 0.10
    cfg.rippleIntensityScale += amp * 0.6
    cfg.speed += amp * 0.20
  }
  return cfg
}

interface AgentPixelFieldProps {
  /** Reduce density/speed for utility pages (Settings, Files) — defaults to false. */
  calm?: boolean
  /** Disable pointer interaction (background layer is pointer-events:none in App). */
  interactive?: boolean
  className?: string
  style?: React.CSSProperties
}

export default function AgentPixelField({
  calm = false,
  interactive = false,
  className,
  style,
}: AgentPixelFieldProps) {
  const { mode, amplitude, lastPulse } = useAgentField()
  const pixelRef = useRef<PixelBlastHandle | null>(null)
  const lastPulseIdRef = useRef<number>(-1)

  // Subscribe to programmatic pulses from context.
  useEffect(() => {
    if (!lastPulse || lastPulse.id === lastPulseIdRef.current) return
    lastPulseIdRef.current = lastPulse.id
    const handle = pixelRef.current
    if (!handle) return
    handle.triggerRipple(lastPulse.nx, lastPulse.ny)
    // Punchier visuals get a quick second ripple for a bloom effect.
    if (lastPulse.intensity >= 1.5) {
      setTimeout(() => handle.triggerRipple(lastPulse.nx + 0.02, lastPulse.ny - 0.02), 90)
    }
  }, [lastPulse])

  const cfg = resolveConfig(mode, amplitude)
  const densityMul = calm ? 0.7 : 1.0
  const speedMul = calm ? 0.7 : 1.0

  return (
    <PixelBlast
      ref={pixelRef}
      variant="circle"
      pixelSize={calm ? 5 : 4}
      color={cfg.color}
      patternScale={cfg.patternScale}
      patternDensity={cfg.patternDensity * densityMul}
      pixelSizeJitter={cfg.pixelJitter}
      enableRipples
      rippleSpeed={cfg.rippleSpeed}
      rippleThickness={0.12}
      rippleIntensityScale={cfg.rippleIntensityScale}
      liquid={false}
      speed={cfg.speed * speedMul}
      edgeFade={cfg.edgeFade}
      transparent
      autoPauseOffscreen
      interactive={interactive}
      className={className}
      style={style}
    />
  )
}
