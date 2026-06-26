import { motion, AnimatePresence } from 'framer-motion'
import { PressButton } from './PressButton'

/**
 * Dual-stage confirmation modal (Item 9) — backdrop blurs in first, panel
 * scales/lifts in with gentle overshoot; reverses faster on close. Tier-3
 * elevated glass surface.
 */
const EASE = [0.16, 1, 0.3, 1] as const
const SOFT = [0.34, 1.56, 0.64, 1] as const

interface ConfirmModalProps {
  open: boolean
  title: string
  description?: string
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmModal({
  open, title, description, confirmLabel = 'Confirm', cancelLabel = 'Cancel',
  danger, onConfirm, onCancel,
}: ConfirmModalProps) {
  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-[210] flex items-center justify-center px-6" onClick={onCancel}>
          <motion.div
            className="absolute inset-0" style={{ background: 'rgba(4,6,11,0.6)' }}
            initial={{ opacity: 0, backdropFilter: 'blur(0px)' }}
            animate={{ opacity: 1, backdropFilter: 'blur(18px)' }}
            exit={{ opacity: 0, backdropFilter: 'blur(0px)', transition: { duration: 0.2, ease: EASE } }}
            transition={{ duration: 0.34, ease: EASE }}
          />
          <motion.div
            onClick={e => e.stopPropagation()}
            initial={{ opacity: 0, scale: 0.93, y: -8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: -8, transition: { duration: 0.18, ease: EASE } }}
            transition={{ duration: 0.4, ease: SOFT, delay: 0.08 }}
            className="relative w-full max-w-sm glass-strong rounded-2xl p-6"
            style={{ borderRadius: 20 }}
          >
            <h2 className="text-base font-semibold text-[#E2E8F0]">{title}</h2>
            {description && <p className="text-sm text-[#9AA7BD] mt-2 leading-relaxed">{description}</p>}
            <div className="flex items-center gap-3 mt-6">
              <PressButton variant={danger ? 'danger' : 'primary'} className="flex-1" onClick={onConfirm}>
                {confirmLabel}
              </PressButton>
              <PressButton variant="ghost" onClick={onCancel}>{cancelLabel}</PressButton>
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}

export default ConfirmModal
