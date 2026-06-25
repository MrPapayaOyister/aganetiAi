import { memo, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'

export type OrbMode = 'idle' | 'listening' | 'thinking' | 'speaking' | 'error'

interface OrbAnimationProps {
  mode?: OrbMode
  amplitude?: number          // 0–1, listening ring scale / speaking core brightness
  amplitudeArray?: number[]   // ~24 values for speaking bars
  size?: number               // px, default 200
  className?: string
}

const RING = {
  idle:      { glow: 0.22, ringO: 0.18 },
  listening: { glow: 0.5,  ringO: 0.4  },
  thinking:  { glow: 0.45, ringO: 0.32 },
  speaking:  { glow: 0.55, ringO: 0.4  },
  error:     { glow: 0.5,  ringO: 0.4  },
}

function OrbBase({
  mode = 'idle',
  amplitude = 0,
  amplitudeArray,
  size = 200,
  className = '',
}: OrbAnimationProps) {
  const cfg = RING[mode]
  const isThinking = mode === 'thinking'
  const isListening = mode === 'listening'
  const isSpeaking = mode === 'speaking'
  const isError = mode === 'error'

  // 24 speaking bars — use provided FFT array or synthesize from amplitude
  const bars = useMemo(() => {
    const N = 24
    if (amplitudeArray && amplitudeArray.length) {
      return Array.from({ length: N }, (_, i) =>
        amplitudeArray[Math.floor((i / N) * amplitudeArray.length)] ?? 0
      )
    }
    return Array.from({ length: N }, (_, i) =>
      isSpeaking ? Math.max(0.05, amplitude * (0.45 + 0.55 * Math.abs(Math.sin(i * 1.7)))) : 0
    )
  }, [amplitudeArray, amplitude, isSpeaking])

  const core = size * 0.46
  const coreBright = isSpeaking ? 0.6 + amplitude * 0.4 : isListening ? 0.7 + amplitude * 0.3 : 1

  return (
    <motion.div
      className={`relative flex items-center justify-center gpu ${className}`}
      style={{ width: size, height: size }}
      animate={
        isError
          ? { x: [-8, 8, -5, 5, -2, 2, 0] }
          : { scale: isThinking ? 1 : [1, 1.04, 1] }
      }
      transition={
        isError
          ? { duration: 0.5, ease: 'easeOut' }
          : { duration: 3.5, repeat: Infinity, ease: 'easeInOut' }
      }
    >
      {/* ── Ambient glow ── */}
      <motion.div
        className="absolute rounded-full"
        style={{
          width: size * 0.7, height: size * 0.7,
          background: isError
            ? 'radial-gradient(circle, rgba(255,68,102,0.5), transparent 70%)'
            : isThinking
            ? 'radial-gradient(circle, rgba(123,47,255,0.45), rgba(0,212,255,0.2) 60%, transparent 75%)'
            : 'radial-gradient(circle, rgba(0,212,255,0.4), rgba(123,47,255,0.22) 60%, transparent 75%)',
          filter: `blur(${size * 0.06}px)`,
        }}
        animate={{ opacity: [cfg.glow * 0.7, cfg.glow, cfg.glow * 0.7] }}
        transition={{ duration: isThinking ? 1 : 2.6, repeat: Infinity, ease: 'easeInOut' }}
      />

      {/* ── Concentric rings (counter-rotating) ── */}
      <motion.div
        className="absolute rounded-full border"
        style={{ width: size * 0.94, height: size * 0.94, borderColor: `rgba(0,212,255,${cfg.ringO})` }}
        animate={{ rotate: 360 }}
        transition={{ duration: isThinking ? 9 : 22, repeat: Infinity, ease: 'linear' }}
      />
      <motion.div
        className="absolute rounded-full border"
        style={{ width: size * 0.78, height: size * 0.78, borderColor: `rgba(123,47,255,${cfg.ringO + 0.05})` }}
        animate={{ rotate: -360 }}
        transition={{ duration: isThinking ? 7 : 16, repeat: Infinity, ease: 'linear' }}
      />
      <motion.div
        className="absolute rounded-full border border-dashed"
        style={{ width: size * 0.62, height: size * 0.62, borderColor: `rgba(0,255,179,${cfg.ringO * 0.6})` }}
        animate={{ rotate: 360 }}
        transition={{ duration: isThinking ? 5 : 28, repeat: Infinity, ease: 'linear' }}
      />

      {/* ── LISTENING: sonar ripples ── */}
      <AnimatePresence>
        {isListening && [0, 1, 2].map(i => (
          <motion.div
            key={`ripple-${i}`}
            className="absolute rounded-full border"
            style={{ width: size * 0.6, height: size * 0.6, borderColor: 'rgba(180,235,255,0.7)' }}
            initial={{ scale: 1, opacity: 0.8 }}
            animate={{ scale: 1.9 + amplitude * 0.5, opacity: 0 }}
            transition={{ duration: 2.4, repeat: Infinity, delay: i * 0.8, ease: 'easeOut' }}
          />
        ))}
      </AnimatePresence>

      {/* ── THINKING: orbiting satellites ── */}
      {isThinking && (
        <motion.div
          className="absolute"
          style={{ width: size, height: size }}
          animate={{ rotate: 360 }}
          transition={{ duration: 3.2, repeat: Infinity, ease: 'linear' }}
        >
          {[0, 1, 2, 3].map(i => (
            <motion.div
              key={i}
              className="absolute rounded-full"
              style={{
                width: size * 0.045, height: size * 0.045,
                top: '50%', left: '50%',
                background: i % 2 ? '#7B2FFF' : '#00D4FF',
                boxShadow: '0 0 8px currentColor',
                transform: `rotate(${i * 90}deg) translateX(${size * 0.42}px)`,
                transformOrigin: '0 0',
              }}
              animate={{ scale: [1, 1.6, 1] }}
              transition={{ duration: 1.4, repeat: Infinity, delay: i * 0.35, ease: 'easeInOut' }}
            />
          ))}
        </motion.div>
      )}

      {/* ── SPEAKING: radial bars ── */}
      {isSpeaking && (
        <svg
          className="absolute"
          width={size} height={size} viewBox={`0 0 ${size} ${size}`}
          style={{ overflow: 'visible' }}
        >
          {bars.map((v, i) => {
            const angle = (i / bars.length) * Math.PI * 2
            const r0 = size * 0.36
            const len = size * 0.05 + v * size * 0.16
            const cx = size / 2, cy = size / 2
            const x1 = cx + Math.cos(angle) * r0
            const y1 = cy + Math.sin(angle) * r0
            const x2 = cx + Math.cos(angle) * (r0 + len)
            const y2 = cy + Math.sin(angle) * (r0 + len)
            return (
              <line
                key={i}
                x1={x1} y1={y1} x2={x2} y2={y2}
                stroke={i % 3 === 0 ? '#7B2FFF' : '#00D4FF'}
                strokeWidth={size * 0.012}
                strokeLinecap="round"
                opacity={0.5 + v * 0.5}
                style={{ transition: 'all 0.08s linear' }}
              />
            )
          })}
        </svg>
      )}

      {/* ── Thinking conic core ── */}
      {isThinking && (
        <motion.div
          className="absolute rounded-full"
          style={{
            width: core, height: core,
            background: 'conic-gradient(from 0deg, #00D4FF, #7B2FFF, #00FFB3, #00D4FF)',
            filter: 'blur(2px)', opacity: 0.85,
          }}
          animate={{ rotate: 360 }}
          transition={{ duration: 2.4, repeat: Infinity, ease: 'linear' }}
        />
      )}

      {/* ── Core orb ── */}
      <motion.svg
        width={core} height={core} viewBox="0 0 100 100"
        className="relative"
        animate={{ rotate: isThinking ? 0 : 360 }}
        transition={{ duration: 40, repeat: Infinity, ease: 'linear' }}
        style={{ filter: `brightness(${coreBright})` }}
      >
        <defs>
          <radialGradient id="orbCore" cx="36%" cy="34%">
            <stop offset="0%"  stopColor={isError ? '#FF6B8A' : '#7FE7FF'} stopOpacity="0.95" />
            <stop offset="45%" stopColor={isError ? '#FF4466' : '#00D4FF'} stopOpacity="0.85" />
            <stop offset="80%" stopColor={isError ? '#8B1E3A' : '#7B2FFF'} stopOpacity="0.7" />
            <stop offset="100%" stopColor="#1E2230" stopOpacity="0.55" />
          </radialGradient>
        </defs>
        <circle cx="50" cy="50" r="46" fill="url(#orbCore)" />
        <circle cx="50" cy="50" r="46" fill="none" stroke="rgba(255,255,255,0.18)" strokeWidth="1" />
        <circle cx="36" cy="36" r="4" fill="rgba(255,255,255,0.55)" />
        <circle cx="64" cy="44" r="2.4" fill="rgba(255,255,255,0.4)" />
        <circle cx="52" cy="64" r="3" fill="rgba(255,255,255,0.3)" />
      </motion.svg>
    </motion.div>
  )
}

export const OrbAnimation = memo(OrbBase)
export default OrbAnimation
