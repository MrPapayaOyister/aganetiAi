import { motion } from 'framer-motion'
import { CheckCircle, XCircle, Info, AlertTriangle, X } from 'lucide-react'
import type { Toast as ToastType } from '../hooks/useToast'

const config = {
  success: { icon: CheckCircle, color: '#00FF88', bg: 'rgba(0,255,136,0.1)', border: 'rgba(0,255,136,0.2)' },
  error:   { icon: XCircle,     color: '#FF4466', bg: 'rgba(255,68,102,0.1)', border: 'rgba(255,68,102,0.2)' },
  warning: { icon: AlertTriangle, color: '#FFB800', bg: 'rgba(255,184,0,0.1)', border: 'rgba(255,184,0,0.2)' },
  info:    { icon: Info,         color: '#00D4FF', bg: 'rgba(0,212,255,0.1)', border: 'rgba(0,212,255,0.2)' },
}

export function Toast({ toast, onDismiss }: { toast: ToastType; onDismiss: (id: string) => void }) {
  const { icon: Icon, color, bg, border } = config[toast.type]

  return (
    <motion.div
      initial={{ opacity: 0, y: -16, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -8, scale: 0.95 }}
      transition={{ type: 'spring', stiffness: 400, damping: 30 }}
      style={{ background: bg, border: `1px solid ${border}` }}
      className="fixed top-4 right-4 z-[200] flex items-center gap-3 px-4 py-3
                 rounded-xl backdrop-blur-xl shadow-2xl max-w-sm"
    >
      <Icon size={18} style={{ color }} className="shrink-0" />
      <span className="text-sm text-[#E2E8F0] leading-snug flex-1">{toast.message}</span>
      <button
        onClick={() => onDismiss(toast.id)}
        className="shrink-0 text-[#4A6080] hover:text-[#94A3B8] transition-colors"
      >
        <X size={14} />
      </button>
    </motion.div>
  )
}
