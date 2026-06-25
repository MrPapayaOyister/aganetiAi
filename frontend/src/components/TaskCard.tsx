import { useState } from 'react'
import { motion } from 'framer-motion'
import { Calendar, Trash2, ChevronDown, ChevronUp } from 'lucide-react'
import type { Task } from '../api/client'

const priorityConfig = {
  urgent: { label: 'URGENT', color: '#FF4466', bg: 'rgba(255,68,102,0.12)' },
  high:   { label: 'HIGH',   color: '#FFB800', bg: 'rgba(255,184,0,0.12)' },
  medium: { label: 'MED',    color: '#00D4FF', bg: 'rgba(0,212,255,0.12)' },
  low:    { label: 'LOW',    color: '#4A6080', bg: 'rgba(74,96,128,0.12)' },
}

interface TaskCardProps {
  task: Task
  onDelete?: (id: string) => void
  onUpdate?: (id: string, data: Partial<Task>) => void
  dragging?: boolean
}

export function TaskCard({ task, onDelete, dragging }: TaskCardProps) {
  const [expanded, setExpanded] = useState(false)
  const { label, color, bg } = priorityConfig[task.priority] ?? priorityConfig.medium

  const dueDate = task.due_date ? new Date(task.due_date) : null
  const isOverdue = dueDate && dueDate < new Date() && task.status !== 'done'

  return (
    <motion.div
      layout
      className={`glass-sm p-3 rounded-xl transition-all duration-200 select-none
                  ${dragging ? 'opacity-60 rotate-1 shadow-2xl' : ''}
                  ${task.status === 'done' ? 'opacity-50' : ''}`}
    >
      <div className="flex items-start gap-2">
        {/* Priority badge */}
        <span
          className="shrink-0 mt-0.5 text-[9px] font-bold px-1.5 py-0.5 rounded"
          style={{ color, background: bg }}
        >
          {label}
        </span>

        {/* Title */}
        <div className="flex-1 min-w-0">
          <p className={`text-sm leading-snug text-[#E2E8F0] break-words
                         ${task.status === 'done' ? 'line-through text-[#4A6080]' : ''}`}>
            {task.title}
          </p>

          {/* Due date */}
          {dueDate && (
            <div className={`flex items-center gap-1 mt-1 text-[11px]
                             ${isOverdue ? 'text-[#FF4466]' : 'text-[#4A6080]'}`}>
              <Calendar size={10} />
              <span>{dueDate.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}</span>
            </div>
          )}
        </div>

        {/* Expand / collapse notes */}
        {task.notes && (
          <button
            onClick={() => setExpanded(e => !e)}
            className="shrink-0 text-[#4A6080] hover:text-[#94A3B8] p-1"
          >
            {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
          </button>
        )}

        {/* Delete */}
        {onDelete && (
          <motion.button
            whileTap={{ scale: 0.85 }}
            onClick={() => onDelete(task.id)}
            className="shrink-0 text-[#4A6080] hover:text-[#FF4466] p-1 transition-colors"
          >
            <Trash2 size={13} />
          </motion.button>
        )}
      </div>

      {/* Notes expansion */}
      {expanded && task.notes && (
        <motion.div
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.2 }}
          className="mt-2 pl-10 text-xs text-[#4A6080] leading-relaxed"
        >
          {task.notes}
        </motion.div>
      )}
    </motion.div>
  )
}
