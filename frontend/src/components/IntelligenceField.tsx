import { useRef, useEffect } from 'react'

type AmbientMode = 'idle' | 'thinking' | 'listening' | 'speaking'

interface IntelligenceFieldProps {
  mode?: AmbientMode
  amplitude?: number
}

const BLOBS = [
  { nx: 0.12, ny: 0.25, nr: 0.55, rgb: [0,  190, 230] as [number,number,number], baseAlpha: 0.040, phase: 0.00, driftX: 0.09, driftY: 0.06, freqX: 0.00025, freqY: 0.00020, breatheFreq: 0.00045 },
  { nx: 0.88, ny: 0.72, nr: 0.50, rgb: [95,  40, 215] as [number,number,number], baseAlpha: 0.048, phase: 2.09, driftX: 0.07, driftY: 0.10, freqX: 0.00018, freqY: 0.00032, breatheFreq: 0.00038 },
  { nx: 0.50, ny: 0.45, nr: 0.70, rgb: [22,  12, 155] as [number,number,number], baseAlpha: 0.028, phase: 4.19, driftX: 0.04, driftY: 0.04, freqX: 0.00012, freqY: 0.00010, breatheFreq: 0.00030 },
  { nx: 0.08, ny: 0.78, nr: 0.42, rgb: [0,  210, 245] as [number,number,number], baseAlpha: 0.032, phase: 1.26, driftX: 0.11, driftY: 0.07, freqX: 0.00030, freqY: 0.00025, breatheFreq: 0.00052 },
  { nx: 0.92, ny: 0.18, nr: 0.46, rgb: [70,  25, 195] as [number,number,number], baseAlpha: 0.036, phase: 3.67, driftX: 0.06, driftY: 0.09, freqX: 0.00022, freqY: 0.00028, breatheFreq: 0.00042 },
]

const MODE_BOOST: Record<string, number> = {
  idle: 1.00, listening: 1.18, thinking: 1.50, speaking: 1.38,
}

export default function IntelligenceField({ mode = 'idle', amplitude = 0 }: IntelligenceFieldProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const modeRef  = useRef(mode)
  const ampRef   = useRef(amplitude)
  const rafRef   = useRef(0)

  useEffect(() => { modeRef.current = mode },      [mode])
  useEffect(() => { ampRef.current  = amplitude }, [amplitude])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    let W = 0, H = 0

    const sync = () => {
      W = window.innerWidth
      H = window.innerHeight
      canvas.width        = W * dpr
      canvas.height       = H * dpr
      canvas.style.width  = W + 'px'
      canvas.style.height = H + 'px'
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    sync()
    const ro = new ResizeObserver(sync)
    ro.observe(document.documentElement)

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches

    const renderFrame = (t: number) => {
      if (!W || !H) return
      ctx.clearRect(0, 0, W, H)
      const boost   = (MODE_BOOST[modeRef.current] ?? 1.0) * (1 + ampRef.current * 0.28)
      const minDim  = Math.min(W, H)

      for (const b of BLOBS) {
        const cx      = (b.nx + Math.sin(t * b.freqX + b.phase) * b.driftX) * W
        const cy      = (b.ny + Math.cos(t * b.freqY + b.phase * 1.33) * b.driftY) * H
        const breathe = 1 + 0.07 * Math.sin(t * b.breatheFreq + b.phase)
        const r       = b.nr * minDim * breathe
        const a       = Math.min(b.baseAlpha * boost * breathe, 0.14)

        const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r)
        const [R, G, B] = b.rgb
        g.addColorStop(0,    `rgba(${R},${G},${B},${a.toFixed(4)})`)
        g.addColorStop(0.45, `rgba(${R},${G},${B},${(a * 0.35).toFixed(4)})`)
        g.addColorStop(1,    `rgba(${R},${G},${B},0)`)

        ctx.fillStyle = g
        ctx.beginPath()
        ctx.arc(cx, cy, r, 0, Math.PI * 2)
        ctx.fill()
      }
    }

    if (reduced) {
      renderFrame(5000)
      return () => ro.disconnect()
    }

    const TARGET_FPS = 15
    const FRAME_MS   = 1000 / TARGET_FPS
    let last = 0

    const loop = (ts: number) => {
      rafRef.current = requestAnimationFrame(loop)
      if (ts - last < FRAME_MS) return
      last = ts
      renderFrame(ts)
    }
    rafRef.current = requestAnimationFrame(loop)

    return () => {
      cancelAnimationFrame(rafRef.current)
      ro.disconnect()
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
    />
  )
}
