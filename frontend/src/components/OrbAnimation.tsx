import { motion } from 'framer-motion'

interface OrbAnimationProps {
  size?: 'sm' | 'lg'
  speaking?: boolean
  streaming?: boolean
}

export function OrbAnimation({ size = 'lg', speaking = false, streaming = false }: OrbAnimationProps) {
  const dim = size === 'lg' ? 180 : 44
  const isActive = streaming || speaking

  return (
    <motion.div
      className="relative flex items-center justify-center"
      style={{ width: dim, height: dim }}
      animate={{ scale: isActive ? [1, 1.04, 1] : [1, 1.02, 1] }}
      transition={{ duration: isActive ? 1.2 : 3, repeat: Infinity, ease: 'easeInOut' }}
    >
      {/* Outer ring 1 */}
      <motion.div
        className="absolute rounded-full border border-[#00D4FF]/15"
        style={{ width: dim, height: dim }}
        animate={{ rotate: 360 }}
        transition={{ duration: 20, repeat: Infinity, ease: 'linear' }}
      />

      {/* Outer ring 2 */}
      <motion.div
        className="absolute rounded-full border border-[#7B2FFF]/20"
        style={{ width: dim * 0.82, height: dim * 0.82 }}
        animate={{ rotate: -360 }}
        transition={{ duration: 14, repeat: Infinity, ease: 'linear' }}
      />

      {/* Glow blur */}
      <motion.div
        className="absolute rounded-full"
        style={{
          width: dim * 0.56,
          height: dim * 0.56,
          background: isActive
            ? 'radial-gradient(circle, rgba(0,212,255,0.45) 0%, rgba(123,47,255,0.3) 60%, transparent 100%)'
            : 'radial-gradient(circle, rgba(0,212,255,0.2) 0%, rgba(123,47,255,0.15) 60%, transparent 100%)',
          filter: 'blur(8px)',
        }}
        animate={{ opacity: isActive ? [0.7, 1, 0.7] : [0.5, 0.8, 0.5] }}
        transition={{ duration: isActive ? 1 : 2.5, repeat: Infinity, ease: 'easeInOut' }}
      />

      {/* Core SVG */}
      <svg width={dim * 0.55} height={dim * 0.55} viewBox="0 0 100 100">
        <defs>
          <radialGradient id={`orbGrad-${size}`} cx="35%" cy="35%">
            <stop offset="0%" stopColor="#00D4FF" stopOpacity="0.9" />
            <stop offset="50%" stopColor="#7B2FFF" stopOpacity="0.7" />
            <stop offset="100%" stopColor="#070B14" stopOpacity="0.4" />
          </radialGradient>
        </defs>
        <circle cx="50" cy="50" r="46" fill={`url(#orbGrad-${size})`} />
        <circle cx="50" cy="50" r="46" fill="none" stroke="rgba(0,212,255,0.3)" strokeWidth="1.5" />
        {size === 'lg' && (
          <>
            <circle cx="35" cy="38" r="3" fill="rgba(255,255,255,0.6)" />
            <circle cx="65" cy="45" r="2" fill="rgba(255,255,255,0.4)" />
            <circle cx="50" cy="62" r="2.5" fill="rgba(255,255,255,0.35)" />
          </>
        )}
      </svg>

      {/* Active pulse ring */}
      {isActive && (
        <motion.div
          className="absolute rounded-full border border-[#00D4FF]/40"
          style={{ width: dim, height: dim }}
          animate={{ scale: [1, 1.35], opacity: [0.4, 0] }}
          transition={{ duration: 1.5, repeat: Infinity, ease: 'easeOut' }}
        />
      )}
    </motion.div>
  )
}
