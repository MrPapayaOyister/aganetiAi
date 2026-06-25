import { memo, useEffect, useRef } from 'react'
import * as THREE from 'three'

export type OrbMode = 'idle' | 'listening' | 'thinking' | 'speaking' | 'acting' | 'success' | 'error'

interface IntelligenceOrbProps {
  mode?: OrbMode
  /** 0..1 scalar amplitude (mic or TTS) — used as fallback when no analyserNode. */
  amplitude?: number
  /** Live AnalyserNode (from useVoice). When present, FFT bands drive per-vertex radial displacement. */
  analyserNode?: AnalyserNode | null
  size?: number
  className?: string
}

const GOLDEN = Math.PI * (3 - Math.sqrt(5))

// ─── Shaders ───────────────────────────────────────────────────────────────
const VERT = /* glsl */ `
  precision highp float;
  uniform float uTime;
  uniform float uAmp;
  uniform float uDisplace;
  uniform float uPointSize;
  uniform float uPixelRatio;
  uniform float uExpand;
  uniform float uShake;
  uniform float uFFT[32];

  varying float vDepth;
  varying float vDisp;

  float hash13(vec3 p) {
    p = fract(p * 0.1031);
    p += dot(p, p.yzx + 33.33);
    return fract((p.x + p.y) * p.z);
  }
  float vnoise(vec3 p) {
    vec3 i = floor(p), f = fract(p);
    vec3 u = f * f * (3.0 - 2.0 * f);
    float a = hash13(i + vec3(0.0, 0.0, 0.0));
    float b = hash13(i + vec3(1.0, 0.0, 0.0));
    float c = hash13(i + vec3(0.0, 1.0, 0.0));
    float d = hash13(i + vec3(1.0, 1.0, 0.0));
    float e = hash13(i + vec3(0.0, 0.0, 1.0));
    float g = hash13(i + vec3(1.0, 0.0, 1.0));
    float h = hash13(i + vec3(0.0, 1.0, 1.0));
    float k = hash13(i + vec3(1.0, 1.0, 1.0));
    return mix(mix(mix(a,b,u.x), mix(c,d,u.x), u.y),
               mix(mix(e,g,u.x), mix(h,k,u.x), u.y), u.z);
  }

  void main() {
    vec3 n = normalize(position);

    // Azimuthal angle → FFT band index (0..31)
    float azi = (atan(n.z, n.x) + 3.14159265) / 6.2831853;     // 0..1
    int band = int(floor(azi * 32.0));
    float fft = 0.0;
    for (int i = 0; i < 32; i++) {
      if (i == band) fft = uFFT[i];
    }

    float t = uTime * 0.45;
    float ns = (vnoise(n * 2.0 + vec3(t, t * 0.7, t * 0.3)) - 0.5) * 2.0; // -1..1

    float disp = uDisplace * 0.05 * ns
               + uAmp * 0.18
               + fft * 0.22
               + uExpand * 0.18;

    vec3 shake = vec3(
      hash13(vec3(uTime * 73.0)) - 0.5,
      hash13(vec3(uTime * 91.0)) - 0.5,
      hash13(vec3(uTime * 47.0)) - 0.5
    ) * uShake * 0.045;

    vec3 displaced = n * (1.0 + disp) + shake;
    vec4 mv = modelViewMatrix * vec4(displaced, 1.0);
    gl_Position = projectionMatrix * mv;

    // Manual size attenuation (perspective camera: in-front mv.z is negative).
    // Hard-capped at 11px — at larger sizes the smoothstep AA band
    // collapses to too few device pixels on mobile GPUs and renders
    // as squares.  11px keeps the soft-circle math working everywhere.
    float dist = max(0.1, -mv.z);
    gl_PointSize = min(11.0, uPointSize * uPixelRatio * (1.0 + disp * 0.30) / dist);

    // Depth proxy — closer to camera (smaller dist) → vDepth near 1.
    vDepth = clamp(1.0 - (dist - 2.1) / 2.0, 0.0, 1.0);
    vDisp = disp;
  }
`

const FRAG = /* glsl */ `
  precision highp float;
  uniform vec3  uColorFront;
  uniform vec3  uColorBack;
  uniform float uAlpha;
  varying float vDepth;
  varying float vDisp;
  void main() {
    // Hard-discard outside circle. AA band widened to 0.15→0.50 so even
    // at low point sizes (5-11 device px) the soft falloff covers
    // multiple pixels — eliminates the square-on-mobile bug.
    vec2 coord = gl_PointCoord - vec2(0.5);
    float dist = length(coord);
    if (dist > 0.5) discard;
    float edge = 1.0 - smoothstep(0.15, 0.50, dist);

    vec3 col = mix(uColorBack, uColorFront, vDepth);
    // Bright accent on ridges of positive displacement
    col += vec3(0.22, 0.55, 0.95) * max(vDisp, 0.0) * 0.55;

    float a = edge * (0.22 + vDepth * 0.85) * uAlpha;
    gl_FragColor = vec4(col, a);
  }
`

// ─── Mode targets ──────────────────────────────────────────────────────────
type ModeTarget = {
  spin: number; tilt: number; displace: number; alpha: number;
  cFront: [number, number, number]; cBack: [number, number, number];
  expand: number; shake: number;
}

const MODE_TARGETS: Record<OrbMode, ModeTarget> = {
  idle:      { spin: 0.10, tilt: 0.06, displace: 0.6, alpha: 0.95, cFront: [0.20, 0.92, 1.00], cBack: [0.48, 0.18, 1.00], expand: 0.0, shake: 0 },
  listening: { spin: 0.16, tilt: 0.10, displace: 0.7, alpha: 1.00, cFront: [0.00, 0.85, 1.00], cBack: [0.40, 0.30, 0.95], expand: 0.0, shake: 0 },
  thinking:  { spin: 0.30, tilt: 0.18, displace: 1.2, alpha: 1.00, cFront: [0.55, 0.30, 1.00], cBack: [0.18, 0.12, 0.85], expand: 0.0, shake: 0 },
  speaking:  { spin: 0.14, tilt: 0.08, displace: 0.8, alpha: 1.00, cFront: [0.00, 0.85, 1.00], cBack: [0.55, 0.22, 1.00], expand: 0.0, shake: 0 },
  acting:    { spin: 0.22, tilt: 0.12, displace: 1.0, alpha: 1.00, cFront: [0.65, 0.35, 1.00], cBack: [0.30, 0.12, 0.85], expand: 1.0, shake: 0 },
  success:   { spin: 0.12, tilt: 0.06, displace: 0.7, alpha: 1.00, cFront: [0.00, 1.00, 0.70], cBack: [0.00, 0.55, 0.45], expand: 0.6, shake: 0 },
  error:     { spin: 0.08, tilt: 0.06, displace: 0.6, alpha: 0.70, cFront: [1.00, 0.55, 0.30], cBack: [0.45, 0.18, 0.10], expand: 0.0, shake: 1 },
}

const lerp = (a: number, b: number, k: number) => a + (b - a) * k
const vlerp = (a: [number, number, number], b: [number, number, number], k: number): [number, number, number] =>
  [lerp(a[0], b[0], k), lerp(a[1], b[1], k), lerp(a[2], b[2], k)]

// ─── Component ─────────────────────────────────────────────────────────────
function IntelligenceOrbBase({
  mode = 'idle',
  amplitude = 0,
  analyserNode = null,
  size = 200,
  className = '',
}: IntelligenceOrbProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const modeRef = useRef<OrbMode>(mode)
  const ampRef = useRef(amplitude)
  const analyserRef = useRef<AnalyserNode | null>(analyserNode)
  useEffect(() => { modeRef.current = mode }, [mode])
  useEffect(() => { ampRef.current = amplitude }, [amplitude])
  useEffect(() => { analyserRef.current = analyserNode }, [analyserNode])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return

    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    const lowPower = (navigator.hardwareConcurrency ?? 8) < 4 || window.innerWidth < 768
    const compact = size < 60
    const N = compact ? 100 : lowPower ? 220 : reduce ? 220 : 600

    const dprCap = lowPower ? 1.5 : 2.0
    const dpr = Math.min(window.devicePixelRatio || 1, dprCap)

    const renderer = new THREE.WebGLRenderer({
      antialias: !lowPower,
      alpha: true,
      powerPreference: 'high-performance',
    })
    renderer.setPixelRatio(dpr)
    renderer.setSize(size, size, false)
    renderer.setClearAlpha(0)
    renderer.domElement.style.width = `${size}px`
    renderer.domElement.style.height = `${size}px`
    renderer.domElement.style.pointerEvents = 'none'
    container.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 100)
    camera.position.set(0, 0, 3.2)

    // Fibonacci lattice on unit sphere
    const positions = new Float32Array(N * 3)
    for (let i = 0; i < N; i++) {
      const y = 1 - (i / Math.max(1, N - 1)) * 2
      const r = Math.sqrt(Math.max(0, 1 - y * y))
      const th = i * GOLDEN
      positions[i * 3]     = Math.cos(th) * r
      positions[i * 3 + 1] = y
      positions[i * 3 + 2] = Math.sin(th) * r
    }
    const geo = new THREE.BufferGeometry()
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3))

    const initial = MODE_TARGETS.idle
    const uniforms = {
      uTime:       { value: 0 },
      uAmp:        { value: 0 },
      uDisplace:   { value: initial.displace },
      uPointSize:  { value: size * (compact ? 0.16 : 0.085) },
      uPixelRatio: { value: dpr },
      uExpand:     { value: 0 },
      uShake:      { value: 0 },
      uFFT:        { value: new Float32Array(32) },
      uColorFront: { value: new THREE.Vector3(...initial.cFront) },
      uColorBack:  { value: new THREE.Vector3(...initial.cBack) },
      uAlpha:      { value: initial.alpha },
    }
    const material = new THREE.ShaderMaterial({
      uniforms,
      vertexShader: VERT,
      fragmentShader: FRAG,
      transparent: true,
      depthTest: false,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    })
    const points = new THREE.Points(geo, material)
    scene.add(points)

    // Smoothed runtime state
    const cur: ModeTarget & { amp: number } = {
      spin: initial.spin, tilt: initial.tilt, displace: initial.displace, alpha: initial.alpha,
      expand: initial.expand, shake: initial.shake,
      cFront: [...initial.cFront], cBack: [...initial.cBack],
      amp: 0,
    }

    let raf = 0
    let last = performance.now()
    let angY = 0
    const fftScratch = new Uint8Array(64)

    const tick = (now: number) => {
      raf = requestAnimationFrame(tick)
      const dt = Math.min(0.05, (now - last) / 1000)
      last = now

      const target = MODE_TARGETS[modeRef.current] ?? MODE_TARGETS.idle
      const k = 1 - Math.exp(-dt * 4.0)         // ~250ms half-life smoothing

      cur.spin     = lerp(cur.spin,     target.spin,     k)
      cur.tilt     = lerp(cur.tilt,     target.tilt,     k)
      cur.displace = lerp(cur.displace, target.displace, k)
      cur.alpha    = lerp(cur.alpha,    target.alpha,    k)
      cur.expand   = lerp(cur.expand,   target.expand,   k * 0.6)  // expand eases slower for the pop feel
      cur.shake    = lerp(cur.shake,    target.shake,    k)
      cur.cFront   = vlerp(cur.cFront,  target.cFront,   k)
      cur.cBack    = vlerp(cur.cBack,   target.cBack,    k)
      cur.amp      = lerp(cur.amp, ampRef.current, k * 1.5)

      angY += cur.spin * dt
      points.rotation.y = angY
      points.rotation.x = Math.sin(now * 0.00030) * cur.tilt
                        + Math.cos(now * 0.00017) * cur.tilt * 0.45
      points.rotation.z = Math.sin(now * 0.00021) * 0.045

      // FFT sampling (real audio reactivity)
      const ana = analyserRef.current
      const uf = uniforms.uFFT.value as Float32Array
      if (ana) {
        const bin = Math.min(64, ana.frequencyBinCount)
        ana.getByteFrequencyData(fftScratch.subarray(0, bin))
        for (let i = 0; i < 32; i++) {
          const lo = Math.floor((i / 32) * bin)
          const hi = Math.max(lo + 1, Math.floor(((i + 1) / 32) * bin))
          let s = 0
          for (let j = lo; j < hi; j++) s += fftScratch[j]
          const avg = (s / Math.max(1, hi - lo)) / 255
          uf[i] += (avg - uf[i]) * 0.45                 // ease for jitter suppression
        }
      } else {
        // Idle decay when no analyser is connected
        for (let i = 0; i < 32; i++) uf[i] *= 0.85
      }

      uniforms.uTime.value      = now * 0.001
      uniforms.uAmp.value       = cur.amp
      uniforms.uDisplace.value  = cur.displace
      uniforms.uExpand.value    = cur.expand
      uniforms.uShake.value     = cur.shake
      uniforms.uAlpha.value     = cur.alpha
      uniforms.uColorFront.value.set(cur.cFront[0], cur.cFront[1], cur.cFront[2])
      uniforms.uColorBack.value.set(cur.cBack[0], cur.cBack[1], cur.cBack[2])

      renderer.render(scene, camera)
    }
    raf = requestAnimationFrame(tick)

    return () => {
      cancelAnimationFrame(raf)
      geo.dispose()
      material.dispose()
      renderer.dispose()
      renderer.forceContextLoss()
      if (renderer.domElement.parentElement === container) {
        container.removeChild(renderer.domElement)
      }
    }
  }, [size])

  return (
    <div
      ref={containerRef}
      aria-hidden
      className={`gpu ${className}`}
      style={{ width: size, height: size, display: 'block' }}
    />
  )
}

export const IntelligenceOrb = memo(IntelligenceOrbBase)
export default IntelligenceOrb
