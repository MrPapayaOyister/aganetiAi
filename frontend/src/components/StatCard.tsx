import { useEffect, useRef } from 'react'
import { motion, useMotionValue, useSpring } from 'framer-motion'
import { TrendingUp, TrendingDown, Minus } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

interface StatCardProps {
  label: string
  value: number
  icon: LucideIcon
  color: string
  trend?: 'up' | 'down' | 'flat'
  unit?: string
  onClick?: () => void
  loading?: boolean
}

export function StatCard({ label, value, icon: Icon, color, trend, unit, onClick, loading }: StatCardProps) {
  const mv = useMotionValue(0)
  const spring = useSpring(mv, { stiffness: 100, damping: 20 })
  const displayRef = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    mv.set(value)
  }, [value, mv])

  useEffect(() => {
    return spring.on('change', v => {
      if (displayRef.current) {
        displayRef.current.textContent = Math.round(v).toString()
      }
    })
  }, [spring])

  const TrendIcon = trend === 'up' ? TrendingUp : trend === 'down' ? TrendingDown : Minus
  const trendColor = trend === 'up' ? '#00FF88' : trend === 'down' ? '#FF4466' : '#4A6080'

  if (loading) {
    return (
      <div className="glass p-5 rounded-2xl">
        <div className="skeleton h-3 w-20 rounded bg-[#1E3A5F]/40 mb-4" />
        <div className="skeleton h-8 w-16 rounded bg-[#1E3A5F]/40 mb-3" />
        <div className="skeleton h-3 w-12 rounded bg-[#1E3A5F]/40" />
      </div>
    )
  }

  return (
    <motion.div
      whileHover={{ y: -2, boxShadow: `0 0 20px ${color}20, 0 8px 32px rgba(0,0,0,0.3)` }}
      onClick={onClick}
      className={`glass p-5 rounded-2xl transition-shadow duration-300
                  ${onClick ? 'cursor-pointer' : ''}`}
    >
      <div className="flex items-start justify-between mb-3">
        <span className="text-xs text-[#4A6080] font-medium uppercase tracking-wide">{label}</span>
        <div
          className="w-8 h-8 rounded-xl flex items-center justify-center"
          style={{ background: `${color}18`, border: `1px solid ${color}30` }}
        >
          <Icon size={15} style={{ color }} />
        </div>
      </div>

      <div className="flex items-end gap-1 mb-2">
        <span ref={displayRef} className="text-3xl font-bold text-[#E2E8F0]">
          {Math.round(value)}
        </span>
        {unit && <span className="text-[#4A6080] text-sm mb-1">{unit}</span>}
      </div>

      {trend && (
        <div className="flex items-center gap-1">
          <TrendIcon size={12} style={{ color: trendColor }} />
          <span className="text-xs" style={{ color: trendColor }}>
            {trend === 'up' ? 'Higher' : trend === 'down' ? 'Lower' : 'Stable'} than last week
          </span>
        </div>
      )}
    </motion.div>
  )
}
