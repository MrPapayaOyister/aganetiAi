import { memo, useEffect, useRef } from 'react'

export type OrbMode = 'idle' | 'listening' | 'thinking' | 'speaking' | 'error'

interface ParticleSphereProps {
  mode?: OrbMode
  amplitude?: number          // 0–1 (mic on listen, TTS on speak)
  amplitudeArray?: number[]   // per-point variation while speaking
  size?: number               // px, default 180
  className?: string
}

const TAU = Math.PI * 2
const GOLDEN = Math.PI * (3 - Math.sqrt(5))

function ParticleSphereBase({
  mode = 'idle',
  amplitude = 0,
  amplitudeArray,
  size = 180,
  className = '',
}: ParticleSphereProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const modeRef = useRef(mode)
  const ampRef = useRef(amplitude)
  const arrRef = useRef<number[] | undefined>(amplitudeArray)
  useEffect(() => { modeRef.current = mode }, [mode])
  useEffect(() => { ampRef.current = amplitude }, [amplitude])
  useEffect(() => { arrRef.current = amplitudeArray }, [amplitudeArray])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    const lowPower = (navigator.hardwareConcurrency ?? 8) < 4
    let N = Math.round(Math.min(260, Math.max(36, size * 1.4)))
    if (lowPower) N = Math.min(N, 80)
    if (reduce) N = Math.min(N, 40)

    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    canvas.width = Math.floor(size * dpr)
    canvas.height = Math.floor(size * dpr)
    ctx.scale(dpr, dpr)

    // Fibonacci-lattice unit sphere
    const pts = Array.from({ length: N }, (_, i) => {
      const y = 1 - (i / (N - 1)) * 2
      const r = Math.sqrt(Math.max(0, 1 - y * y))
      const th = i * GOLDEN
      return { x: Math.cos(th) * r, y, z: Math.sin(th) * r }
    })

    const cx = size / 2
    const cy = size / 2
    const baseR = (size / 2) * 0.82
    const dot = Math.max(0.8, size * 0.012)

    let raf = 0
    let t = 0
    let angY = 0
    let smoothAmp = 0       // smoothed amplitude
    let energy = 0          // smoothed per-mode energy

    function lerp(a: number, b: number, k: number) { return a + (b - a) * k }

    function frame() {
      raf = requestAnimationFrame(frame)
      const m = modeRef.current
      const amp = ampRef.current
      const arr = arrRef.current
      t += 0.016
      smoothAmp = lerp(smoothAmp, amp, 0.2)

      // per-mode targets
      const spin =
        m === 'thinking' ? 0.018 :
        m === 'speaking' ? 0.010 :
        m === 'listening' ? 0.006 : 0.0035
      angY += spin

      const targetEnergy =
        m === 'thinking' ? 1 :
        m === 'speaking' ? 0.6 + smoothAmp : 0
      energy = lerp(energy, targetEnergy, 0.06)

      // breathing / pulsing radius factor
      const breathe =
        m === 'idle'      ? 1 + Math.sin(t * 0.9) * 0.02 :                 // very subtle
        m === 'thinking'  ? 1 + Math.sin(t * 3.4) * 0.06 :                 // clear pulse
        m === 'listening' ? 1 + smoothAmp * 0.28 + Math.sin(t * 2) * 0.02 :
        m === 'speaking'  ? 1 + smoothAmp * 0.30 :
        /* error */         1 + Math.sin(t * 18) * 0.03
      const R = baseR * breathe

      const shake = m === 'error' ? (Math.random() - 0.5) * 6 : 0
      const ox = cx + shake
      const oy = cy + shake

      ctx!.clearRect(0, 0, size, size)

      // central bloom
      if (energy > 0.02 || m === 'idle') {
        const bloom = ctx!.createRadialGradient(cx, cy, 0, cx, cy, R * 1.15)
        const bi = 0.06 + energy * 0.12
        bloom.addColorStop(0, m === 'error'
          ? `rgba(255,68,102,${bi})`
          : `rgba(0,212,255,${bi})`)
        bloom.addColorStop(0.5, `rgba(123,47,255,${bi * 0.5})`)
        bloom.addColorStop(1, 'rgba(0,0,0,0)')
        ctx!.fillStyle = bloom
        ctx!.fillRect(0, 0, size, size)
      }

      const cosA = Math.cos(angY), sinA = Math.sin(angY)
      const tilt = Math.sin(t * 0.3) * 0.25  // gentle wobble around X

      // transform + sort by depth (back to front)
      const proj = pts.map((p, i) => {
        // rotate Y
        let x = p.x * cosA - p.z * sinA
        let z = p.z * cosA + p.x * sinA
        // rotate X (tilt)
        let y = p.y * Math.cos(tilt) - z * Math.sin(tilt)
        z = z * Math.cos(tilt) + p.y * Math.sin(tilt)
        // per-point radius variation while speaking
        let rr = R
        if (m === 'speaking' && arr && arr.length) {
          rr += arr[i % arr.length] * size * 0.05
        } else if (m === 'thinking') {
          rr += Math.sin(t * 5 + i) * size * 0.012
        }
        return { sx: ox + x * rr, sy: oy + y * rr, d: (z + 1) / 2, i }
      }).sort((a, b) => a.d - b.d)

      for (const q of proj) {
        const d = q.d                       // 0 back → 1 front
        const r = dot * (0.5 + d * 1.1) * (1 + energy * 0.4)
        const op = 0.18 + d * 0.8
        let col: string
        if (m === 'error') {
          col = `rgba(255,${Math.round(70 + d * 60)},${Math.round(90 + d * 40)},${op})`
        } else {
          // cyan (front) ↔ purple (back)
          const cr = Math.round(lerp(123, 56, d))
          const cg = Math.round(lerp(47, 219, d))
          const cb = Math.round(lerp(255, 255, d))
          col = `rgba(${cr},${cg},${cb},${op})`
        }
        ctx!.beginPath()
        ctx!.arc(q.sx, q.sy, r, 0, TAU)
        ctx!.fillStyle = col
        ctx!.fill()
      }
    }
    frame()
    return () => cancelAnimationFrame(raf)
  }, [size])

  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      className={`gpu ${className}`}
      style={{ width: size, height: size, display: 'block' }}
    />
  )
}

export const ParticleSphere = memo(ParticleSphereBase)
export default ParticleSphere
