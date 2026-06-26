import { forwardRef } from 'react'

/**
 * Suggestion / quick-action chip (Item 4). Pill shape, soft press, hover tint,
 * selected = accent fill. Shares the .press tactile helper + token motion.
 */
interface PressChipProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  selected?: boolean
}

export const PressChip = forwardRef<HTMLButtonElement, PressChipProps>(function PressChip(
  { selected = false, className = '', children, ...rest }, ref,
) {
  return (
    <button
      ref={ref}
      data-sound="click"
      className={`press inline-flex items-center gap-1.5 rounded-full px-3.5 py-2 text-sm font-medium
                  border outline-none
                  ${selected
                    ? 'bg-[#00D4FF]/18 border-[#00D4FF]/45 text-[#00D4FF]'
                    : 'border-white/[0.07] text-[#9AA7BD] hover:text-[#E2E8F0] hover:border-[#00D4FF]/35 hover:bg-[#00D4FF]/[0.05]'}
                  ${className}`}
      {...rest}
    >
      {children}
    </button>
  )
})

export default PressChip
