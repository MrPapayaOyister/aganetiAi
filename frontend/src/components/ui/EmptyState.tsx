import { motion } from 'framer-motion'
import type { LucideIcon } from 'lucide-react'

/**
 * EmptyState (Item 7) — never abrupt. Icon + title + description fade up over
 * ~600ms behind a soft accent radial glow so the empty state has presence
 * without weight. Optional CTA.
 */
const EASE = [0.16, 1, 0.3, 1] as const

interface EmptyStateProps {
  icon: LucideIcon
  title: string
  description?: string
  action?: React.ReactNode
  accent?: string
}

export function EmptyState({ icon: Icon, title, description, action, accent = '#00D4FF' }: EmptyStateProps) {
  return (
    <div className="relative flex flex-col items-center justify-center text-center px-6 py-12">
      {/* soft radial glow */}
      <div className="absolute inset-0 pointer-events-none"
           style={{ background: `radial-gradient(ellipse 50% 45% at 50% 42%, ${accent}0d, transparent 70%)` }} />
      <motion.div
        initial={{ opacity: 0, scale: 0.8, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={{ duration: 0.6, ease: EASE }}
        className="relative w-14 h-14 rounded-2xl flex items-center justify-center mb-4 neu"
        style={{ color: accent }}
      >
        <Icon size={24} />
      </motion.div>
      <motion.h3
        initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: EASE, delay: 0.12 }}
        className="relative text-sm font-semibold text-[#E2E8F0]"
      >
        {title}
      </motion.h3>
      {description && (
        <motion.p
          initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: EASE, delay: 0.2 }}
          className="relative text-xs text-[#5C6B85] mt-1.5 max-w-xs leading-relaxed"
        >
          {description}
        </motion.p>
      )}
      {action && (
        <motion.div
          initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, ease: EASE, delay: 0.3 }}
          className="relative mt-5"
        >
          {action}
        </motion.div>
      )}
    </div>
  )
}

export default EmptyState
