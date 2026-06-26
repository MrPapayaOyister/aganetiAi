import { motion } from 'framer-motion'

/**
 * Shared toggle switch (Item 9) — smooth thumb slide on the token motion
 * system. Accessible (role=switch + aria-checked), keyboard-focusable.
 */
interface ToggleProps {
  checked: boolean
  onChange: (next: boolean) => void
  label?: string
  description?: string
  disabled?: boolean
}

export function Toggle({ checked, onChange, label, description, disabled }: ToggleProps) {
  const control = (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      data-sound="click"
      onClick={() => onChange(!checked)}
      className="press relative w-11 h-6 rounded-full shrink-0 outline-none
                 disabled:opacity-40 disabled:cursor-not-allowed"
      style={{ background: checked ? 'rgba(0,212,255,0.35)' : 'rgba(255,255,255,0.08)' }}
    >
      <motion.span
        className="absolute top-0.5 left-0.5 w-5 h-5 rounded-full bg-white shadow"
        animate={{ x: checked ? 20 : 0 }}
        transition={{ type: 'spring', stiffness: 500, damping: 34 }}
      />
    </button>
  )
  if (!label && !description) return control
  return (
    <div className="flex items-center justify-between gap-4">
      <div>
        {label && <label className="text-xs font-medium text-[#9AA7BD] block">{label}</label>}
        {description && <p className="text-[11px] text-[#4A6080] mt-0.5">{description}</p>}
      </div>
      {control}
    </div>
  )
}

export default Toggle
