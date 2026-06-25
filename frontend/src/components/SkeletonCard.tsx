interface SkeletonCardProps {
  lines?: number
  height?: string
  className?: string
}

export function SkeletonCard({ lines = 3, height = 'h-4', className = '' }: SkeletonCardProps) {
  return (
    <div className={`glass p-4 space-y-3 ${className}`}>
      {Array.from({ length: lines }, (_, i) => (
        <div
          key={i}
          className={`skeleton rounded-lg bg-[#1E3A5F]/40 ${height}`}
          style={{ width: i === lines - 1 ? '60%' : '100%' }}
        />
      ))}
    </div>
  )
}

export function SkeletonText({ className = '' }: { className?: string }) {
  return <div className={`skeleton rounded bg-[#1E3A5F]/40 h-3 ${className}`} />
}

export function SkeletonAvatar({ size = 10 }: { size?: number }) {
  return (
    <div
      className="skeleton rounded-full bg-[#1E3A5F]/40"
      style={{ width: `${size * 4}px`, height: `${size * 4}px` }}
    />
  )
}
