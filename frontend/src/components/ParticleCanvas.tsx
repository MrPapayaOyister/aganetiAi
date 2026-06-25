import { useEffect, useRef } from 'react'

export type ParticleMode = 'idle' | 'thinking' | 'listening' | 'speaking'

interface ParticleCanvasProps {
  mode?: ParticleMode
  amplitude?: number       // 0–1, drives listening pulse / speaking radiation
  className?: string
  /** Where convergence/radiation centers, as a fraction of canvas (0–1). Default center. */
  focusX?: number
  focusY?: number
}

interface P {
  x: number; y: number
  vx: number; vy: number
  bvx: number; bvy: number   // base drift velocity (idle home)
  r: number
  baseO: number              // base opacity
  twk: number                // twinkle phase
  cyan: boolean
}

const TAU = Math.PI * 2

export default function ParticleCanvas({
  mode = 'idle',
  amplitude = 0,
  className,
  focusX = 0.5,
  focusY = 0.5,
}: ParticleCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  // live refs so the RAF loop reads current values without re-subscribing
  const modeRef = useRef(mode)
  const ampRef  = useRef(amplitude)
  const focusRef = useRef({ x: focusX, y: focusY })
  useEffect(() => { modeRef.current = mode }, [mode])
  useEffect(() => { ampRef.current = amplitude }, [amplitude])
  useEffect(() => { focusRef.current = { x: focusX, y: focusY } }, [focusX, focusY])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d', { alpha: true })
    if (!ctx) return

    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    const lowPower = (navigator.hardwareConcurrency ?? 8) < 4
    const COUNT = reduce ? 36 : lowPower ? 60 : 110

    let dpr = Math.min(window.devicePixelRatio || 1, 2)
    let W = 0, H = 0
    let particles: P[] = []

    const rand = (a: number, b: number) => a + Math.random() * (b - a)

    function spawn(): P {
      const cyan = Math.random() > 0.38
      const speed = rand(0.04, 0.22)
      const ang = Math.random() * TAU
      return {
        x: Math.random() * W,
        y: Math.random() * H,
        vx: Math.cos(ang) * speed,
        vy: Math.sin(ang) * speed,
        bvx: Math.cos(ang) * speed,
        bvy: Math.sin(ang) * speed,
        r: rand(0.6, 2.1),
        baseO: cyan ? rand(0.06, 0.18) : rand(0.04, 0.1),
        twk: Math.random() * TAU,
        cyan,
      }
    }

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 2)
      W = canvas!.clientWidth
      H = canvas!.clientHeight
      canvas!.width = Math.floor(W * dpr)
      canvas!.height = Math.floor(H * dpr)
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0)
    }
    resize()
    particles = Array.from({ length: COUNT }, spawn)

    let raf = 0
    let t = 0
    // smoothed "energy" so transitions between modes are buttery
    let energy = 0

    function frame() {
      raf = requestAnimationFrame(frame)
      t += 0.016
      const m = modeRef.current
      const amp = ampRef.current
      const fx = focusRef.current.x * W
      const fy = focusRef.current.y * H

      // target energy per mode
      const targetEnergy =
        m === 'thinking'  ? 1 :
        m === 'speaking'  ? 0.6 + amp * 0.6 :
        m === 'listening' ? 0.35 + amp * 0.5 :
        0
      energy += (targetEnergy - energy) * 0.05

      ctx!.clearRect(0, 0, W, H)

      for (const p of particles) {
        // direction to focus center
        const dx = fx - p.x
        const dy = fy - p.y
        const dist = Math.hypot(dx, dy) || 1
        const nx = dx / dist, ny = dy / dist

        let tvx = p.bvx
        let tvy = p.bvy

        if (m === 'thinking') {
          // converge + swirl toward center
          const swirl = 0.6
          tvx = (nx * 0.9 - ny * swirl) * (0.5 + energy)
          tvy = (ny * 0.9 + nx * swirl) * (0.5 + energy)
        } else if (m === 'speaking') {
          // radiate outward in rhythm with amplitude
          const pulse = 0.3 + amp * 1.6
          tvx = -nx * pulse
          tvy = -ny * pulse
        } else if (m === 'listening') {
          // gentle inhale toward center, scaled by amplitude
          const inhale = 0.15 + amp * 0.5
          tvx = nx * inhale + p.bvx * 0.4
          tvy = ny * inhale + p.bvy * 0.4
        }

        // ease velocity toward target (physics feel, no snapping)
        const ease = m === 'idle' ? 0.02 : 0.06
        p.vx += (tvx - p.vx) * ease
        p.vy += (tvy - p.vy) * ease

        p.x += p.vx
        p.y += p.vy

        // wrap around edges
        if (p.x < -10) p.x = W + 10
        if (p.x > W + 10) p.x = -10
        if (p.y < -10) p.y = H + 10
        if (p.y > H + 10) p.y = -10

        // twinkle + energy brighten
        p.twk += 0.02
        const twinkle = 0.7 + Math.sin(p.twk) * 0.3
        const o = Math.min(0.9, p.baseO * twinkle * (1 + energy * 2.2))
        const r = p.r * (1 + energy * 0.6)

        ctx!.beginPath()
        ctx!.arc(p.x, p.y, r, 0, TAU)
        ctx!.fillStyle = p.cyan
          ? `rgba(0,212,255,${o})`
          : `rgba(123,47,255,${o})`
        ctx!.fill()
      }

      // soft central bloom that intensifies with energy
      if (energy > 0.02) {
        const g = ctx!.createRadialGradient(fx, fy, 0, fx, fy, Math.max(W, H) * 0.4)
        g.addColorStop(0, `rgba(0,212,255,${0.05 * energy})`)
        g.addColorStop(0.5, `rgba(123,47,255,${0.03 * energy})`)
        g.addColorStop(1, 'rgba(0,0,0,0)')
        ctx!.fillStyle = g
        ctx!.fillRect(0, 0, W, H)
      }
    }
    frame()

    let resizeT: number | undefined
    const onResize = () => {
      window.clearTimeout(resizeT)
      resizeT = window.setTimeout(resize, 150)
    }
    window.addEventListener('resize', onResize)

    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('resize', onResize)
      window.clearTimeout(resizeT)
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      aria-hidden
      className={className}
      style={{ width: '100%', height: '100%', display: 'block', pointerEvents: 'none' }}
    />
  )
}
