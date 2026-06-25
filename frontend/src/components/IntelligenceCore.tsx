import { motion } from 'framer-motion'
import { useAgentField } from '../contexts/AgentFieldContext'
import { usePrefs } from '../contexts/PrefsContext'
import AgentPixelField from './AgentPixelField'

/**
 * IntelligenceCore — premium composition for PixelBlast.
 *
 * Replaces the "uniform wallpaper" look with a layered intelligence-core
 * composition:
 *
 *   ┌──────────────────────────────────────────────────────────┐
 *   │   Layer 1 · deep core glow                               │
 *   │     ◆  cyan/violet radial wash, very low alpha           │
 *   │                                                          │
 *   │   Layer 2 · masked PixelBlast                            │
 *   │     ◆  full-bleed canvas, but mask concentrates the       │
 *   │        visible field at the central nucleus and thins    │
 *   │        outward via a multi-radial-gradient mask          │
 *   │        (two satellite blobs for asymmetry)               │
 *   │                                                          │
 *   │   Layer 3 · soft inner ring (state-tinted)               │
 *   │     ◆  subtle gradient ring around the core, color and   │
 *   │        opacity respond to assistant mode                 │
 *   │                                                          │
 *   │   Layer 4 · edge vignette                                │
 *   │     ◆  near-black corner shading for premium framing     │
 *   └──────────────────────────────────────────────────────────┘
 *
 *  All layers are pointer-events:none; clicks pass through to the UI.
 *  Only one WebGL context exists (Layer 2); other layers are pure CSS.
 */

// Mask shape: one large central ellipse + two off-center satellites for
// organic thinning/asymmetry. The center is offset ~52% so the visual
// weight balances against the sidebar at the left edge.
const FIELD_MASK = `
  radial-gradient(ellipse 70% 75% at 52% 48%, #000 12%, rgba(0,0,0,0.85) 32%, rgba(0,0,0,0.45) 58%, rgba(0,0,0,0.12) 82%, transparent 95%),
  radial-gradient(circle 28% at 28% 32%, rgba(0,0,0,0.38), transparent 65%),
  radial-gradient(circle 22% at 78% 68%, rgba(0,0,0,0.32), transparent 70%)
`.trim()

// Tint of the deep core glow shifts with mode — pure CSS, no canvas churn.
const CORE_TINTS: Record<string, { inner: string; outer: string; alpha: number }> = {
  idle:      { inner: 'rgba(70, 100, 180,', outer: 'rgba(30, 35, 70,',  alpha: 0.10 },
  listening: { inner: 'rgba(0, 200, 255,',  outer: 'rgba(0, 80, 180,',  alpha: 0.16 },
  thinking:  { inner: 'rgba(123, 47, 255,', outer: 'rgba(60, 20, 140,', alpha: 0.18 },
  speaking:  { inner: 'rgba(0, 212, 255,',  outer: 'rgba(80, 40, 200,', alpha: 0.20 },
  acting:    { inner: 'rgba(157, 91, 255,', outer: 'rgba(80, 30, 200,', alpha: 0.22 },
  success:   { inner: 'rgba(0, 255, 179,',  outer: 'rgba(0, 100, 90,',  alpha: 0.18 },
  error:     { inner: 'rgba(255, 138, 74,', outer: 'rgba(120, 40, 0,',  alpha: 0.16 },
}

export default function IntelligenceCore() {
  const { mode } = useAgentField()
  const { prefs } = usePrefs()
  const isOff = prefs.bgIntensity === 'off'
  const tint = CORE_TINTS[mode] ?? CORE_TINTS.idle
  const intensityMul =
    prefs.bgIntensity === 'cinematic' ? 1.3 :
    prefs.bgIntensity === 'standard'  ? 1.0 :
    prefs.bgIntensity === 'calm'      ? 0.70 : 0
  const tintAlpha = tint.alpha * intensityMul

  return (
    <div className="fixed inset-0 z-0 pointer-events-none overflow-hidden"
         style={{ background: 'radial-gradient(ellipse at center, #0A0E18 0%, #06080E 60%, #04060B 100%)' }}>

      {/* Layer 1 — deep core glow, mode-tinted, animated softly */}
      {!isOff && (
        <motion.div
          aria-hidden
          className="absolute inset-0"
          animate={{
            background: `
              radial-gradient(ellipse 55% 60% at 52% 48%, ${tint.inner}${tintAlpha}), transparent 60%),
              radial-gradient(ellipse 80% 80% at 52% 48%, ${tint.outer}${(tintAlpha * 0.5).toFixed(3)}), transparent 78%)
            `,
          }}
          transition={{ duration: 1.2, ease: 'easeOut' }}
        />
      )}

      {/* Layer 2 — masked PixelBlast: full-bleed canvas, but the mask shapes
          where it shows. Center stays full intensity; periphery thins out. */}
      {!isOff && (
        <div
          aria-hidden
          className="absolute inset-0"
          style={{
            maskImage: FIELD_MASK,
            WebkitMaskImage: FIELD_MASK,
            maskComposite: 'add',
            WebkitMaskComposite: 'source-over',
          }}
        >
          <AgentPixelField bridgeClicks />
        </div>
      )}

      {/* Layer 3 — soft state-tinted inner ring, very subtle */}
      {!isOff && (
        <motion.div
          aria-hidden
          className="absolute inset-0"
          animate={{
            background: `radial-gradient(ellipse 32% 36% at 52% 48%,
                          transparent 58%,
                          ${tint.inner}${(tintAlpha * 0.6).toFixed(3)}) 72%,
                          transparent 88%)`,
          }}
          transition={{ duration: 1.0, ease: 'easeOut' }}
        />
      )}

      {/* Layer 4 — edge vignette: deepens corners so the field reads centered */}
      <div
        aria-hidden
        className="absolute inset-0"
        style={{
          background: `
            radial-gradient(ellipse 100% 100% at 50% 50%, transparent 40%, rgba(4,6,11,0.55) 95%),
            linear-gradient(to bottom, rgba(4,6,11,0.40) 0%, transparent 18%, transparent 82%, rgba(4,6,11,0.55) 100%)
          `,
        }}
      />
    </div>
  )
}
