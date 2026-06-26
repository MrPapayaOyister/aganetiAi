import { forwardRef } from 'react'
import { Loader2 } from 'lucide-react'

/**
 * Canonical button (Item 4). Soft, physical press — like a quality mechanical
 * key, not a hard click — via the .press helper (scale 0.975 + translateY +
 * shadow). All variants share hover tint, visible focus ring, disabled +
 * loading states. Motion uses the token timing system (see index.css .press).
 */
export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'icon' | 'pill' | 'danger'
export type ButtonSize = 'sm' | 'md' | 'lg'

interface PressButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  /** Soft click cue on press (wired by the sound model in Item 8). */
  silent?: boolean
}

const BASE =
  'press relative inline-flex items-center justify-center gap-2 font-medium select-none ' +
  'disabled:opacity-40 disabled:cursor-not-allowed disabled:pointer-events-none ' +
  'outline-none'

const SIZES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-xs rounded-lg',
  md: 'h-10 px-4 text-sm rounded-xl',
  lg: 'h-12 px-6 text-base rounded-xl',
}
const ICON_SIZES: Record<ButtonSize, string> = {
  sm: 'h-8 w-8 rounded-lg', md: 'h-10 w-10 rounded-xl', lg: 'h-12 w-12 rounded-xl',
}

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'bg-[#00D4FF]/15 border border-[#00D4FF]/35 text-[#00D4FF] ' +
    'hover:bg-[#00D4FF]/25 active:shadow-[inset_0_2px_4px_var(--shadow-press)]',
  secondary:
    'bg-transparent border border-white/[0.10] text-[#C8D3E5] ' +
    'hover:bg-white/[0.05] hover:border-white/[0.16] active:shadow-[inset_0_2px_4px_var(--shadow-press)]',
  ghost:
    'bg-transparent text-[#9AA7BD] hover:text-[#E6EBF5] hover:bg-white/[0.04]',
  danger:
    'bg-[#FF4466]/12 border border-[#FF4466]/25 text-[#FF4466] ' +
    'hover:bg-[#FF4466]/22 active:shadow-[inset_0_2px_4px_var(--shadow-press)]',
  icon:
    'neu text-[#9AA7BD] hover:text-[#E6EBF5] ' +
    'active:shadow-[inset_3px_3px_7px_var(--neu-dark),inset_-3px_-3px_7px_var(--neu-light)]',
  pill:
    'rounded-full neu-pill text-[#C8D3E5] hover:text-[#E6EBF5]',
}

export const PressButton = forwardRef<HTMLButtonElement, PressButtonProps>(function PressButton(
  { variant = 'secondary', size = 'md', loading = false, className = '', children, disabled, silent, ...rest },
  ref,
) {
  const isIcon = variant === 'icon'
  const sizeCls = isIcon ? ICON_SIZES[size] : SIZES[size]
  const radiusOverride = variant === 'pill' ? 'rounded-full' : ''
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      data-sound={silent ? undefined : 'click'}
      className={`${BASE} ${sizeCls} ${VARIANTS[variant]} ${radiusOverride} ${className}`}
      {...rest}
    >
      {loading ? (
        <Loader2 className="animate-spin" size={size === 'sm' ? 14 : 16} />
      ) : children}
    </button>
  )
})

export default PressButton
