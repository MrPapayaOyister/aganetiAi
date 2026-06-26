import { useState, useRef, cloneElement, type ReactElement } from 'react'
import { motion, AnimatePresence } from 'framer-motion'

/**
 * Lightweight hover tooltip (Gap C). Wraps a single child; shows a neu-strong
 * badge above (default) after a short delay. Pure transform/opacity motion.
 */
interface TooltipProps {
  label: string
  children: ReactElement
  side?: 'top' | 'bottom'
  delay?: number
}

export function Tooltip({ label, children, side = 'top', delay = 350 }: TooltipProps) {
  const [show, setShow] = useState(false)
  const timer = useRef<number | undefined>(undefined)

  const open = () => { timer.current = window.setTimeout(() => setShow(true), delay) }
  const close = () => { window.clearTimeout(timer.current); setShow(false) }

  const trigger = cloneElement(children, {
    onMouseEnter: open, onMouseLeave: close, onFocus: open, onBlur: close,
  } as Partial<React.HTMLAttributes<HTMLElement>>)

  return (
    <span className="relative inline-flex">
      {trigger}
      <AnimatePresence>
        {show && (
          <motion.span
            initial={{ opacity: 0, y: side === 'top' ? 4 : -4, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: side === 'top' ? 4 : -4, scale: 0.96 }}
            transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }}
            className={`pointer-events-none absolute left-1/2 -translate-x-1/2 z-[120]
                        whitespace-nowrap rounded-lg px-2.5 py-1 text-[11px] font-medium
                        neu-strong text-[#C8D3E5]
                        ${side === 'top' ? 'bottom-full mb-2' : 'top-full mt-2'}`}
          >
            {label}
          </motion.span>
        )}
      </AnimatePresence>
    </span>
  )
}

export default Tooltip
