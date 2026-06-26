/**
 * Skeleton (Item 6) — dual-layer loading placeholder: a base opacity pulse
 * plus a shimmer sweep (both defined in index.css as .skeleton-base). Variants
 * cover the common shapes; all respect prefers-reduced-motion (animation off).
 */
type SkeletonVariant = 'text' | 'title' | 'paragraph' | 'card' | 'chart' | 'avatar' | 'row'

interface SkeletonProps {
  variant?: SkeletonVariant
  className?: string
  /** lines for the paragraph variant */
  lines?: number
}

const Bar = ({ className = '', style }: { className?: string; style?: React.CSSProperties }) =>
  <div className={`skeleton-base ${className}`} style={style} />

export function Skeleton({ variant = 'text', className = '', lines = 3 }: SkeletonProps) {
  switch (variant) {
    case 'title':
      return <Bar className={`h-5 w-1/2 rounded-md ${className}`} />
    case 'paragraph':
      return (
        <div className={`space-y-2 ${className}`}>
          {Array.from({ length: lines }).map((_, i) => (
            <Bar key={i} className={`h-3.5 rounded ${i === lines - 1 ? 'w-2/3' : 'w-full'}`} />
          ))}
        </div>
      )
    case 'avatar':
      return <Bar className={`w-10 h-10 rounded-full ${className}`} />
    case 'row':
      return (
        <div className={`flex items-center gap-3 ${className}`}>
          <Bar className="w-9 h-9 rounded-lg shrink-0" />
          <div className="flex-1 space-y-2">
            <Bar className="h-3.5 w-2/5 rounded" />
            <Bar className="h-3 w-4/5 rounded" />
          </div>
        </div>
      )
    case 'card':
      return (
        <div className={`neu rounded-2xl p-4 space-y-3 ${className}`}>
          <Bar className="h-4 w-1/3 rounded" />
          <Bar className="h-3 w-full rounded" />
          <Bar className="h-3 w-4/5 rounded" />
        </div>
      )
    case 'chart':
      return (
        <div className={`neu rounded-2xl p-4 ${className}`}>
          <Bar className="h-3.5 w-1/3 rounded mb-4" />
          <div className="flex items-end gap-2 h-28">
            {[0.5, 0.8, 0.4, 0.95, 0.65, 0.75, 0.55].map((h, i) => (
              <Bar key={i} className="flex-1 rounded-t" style={{ height: `${h * 100}%` }} />
            ))}
          </div>
          <Bar className="h-2 w-full rounded mt-3 opacity-60" />
        </div>
      )
    case 'text':
    default:
      return <Bar className={`h-3.5 w-full rounded ${className}`} />
  }
}

export default Skeleton
