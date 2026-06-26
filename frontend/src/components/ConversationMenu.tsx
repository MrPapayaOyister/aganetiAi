import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Plus, MessageSquare, Trash2, ChevronDown } from 'lucide-react'
import type { Convo } from '../hooks/useConversations'

interface Props {
  sessions: Convo[]
  activeId: string
  onNew: () => void
  onSwitch: (id: string) => void
  onDelete: (id: string) => void
}

export function ConversationMenu({ sessions, activeId, onNew, onSwitch, onDelete }: Props) {
  const [open, setOpen] = useState(false)
  const active = sessions.find(s => s.id === activeId)

  return (
    <div className="relative">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 h-8 px-2.5 rounded-lg neu-flat t-label max-w-[160px]"
        title="Conversations"
      >
        <MessageSquare size={13} className="text-[#38DBFF] shrink-0" />
        <span className="truncate">{active?.title ?? 'New chat'}</span>
        <ChevronDown size={13} className="shrink-0 text-[#5C6B85]" />
      </button>

      <AnimatePresence>
        {open && (
          <>
            <div className="fixed inset-0 z-[90]" onClick={() => setOpen(false)} />
            <motion.div
              initial={{ opacity: 0, y: -8, scale: 0.96 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: -8, scale: 0.96 }}
              transition={{ type: 'spring', stiffness: 420, damping: 30 }}
              className="absolute left-0 mt-2 w-64 z-[100] neu-strong rounded-xl p-2 max-h-[60vh] overflow-y-auto"
            >
              <button
                onClick={() => { onNew(); setOpen(false) }}
                className="press w-full flex items-center gap-2 px-3 h-9 rounded-lg t-label text-[#38DBFF] hover:bg-white/[0.04]"
              >
                <Plus size={14} /> New chat
              </button>
              <div className="h-px bg-white/[0.06] my-1.5" />
              {sessions.length === 0 && <div className="t-caption px-3 py-2">No conversations yet</div>}
              {sessions.map((s, i) => (
                <motion.div
                  key={s.id}
                  initial={{ opacity: 0, y: -6 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 0.04 + i * 0.03, duration: 0.24, ease: [0.16, 1, 0.3, 1] }}
                  className={`group flex items-center gap-2 px-3 h-9 rounded-lg cursor-pointer
                    ${s.id === activeId ? 'bg-[#38DBFF]/10' : 'hover:bg-white/[0.04]'}`}
                  onClick={() => { onSwitch(s.id); setOpen(false) }}
                >
                  <span className={`truncate t-label flex-1 ${s.id === activeId ? 'text-[#E6EBF5]' : 'text-[var(--text-secondary)]'}`}>
                    {s.title}
                  </span>
                  <button
                    onClick={(e) => { e.stopPropagation(); onDelete(s.id) }}
                    className="press opacity-0 group-hover:opacity-100 text-[#5C6B85] hover:text-[#FF4466] transition-opacity"
                    title="Delete"
                  >
                    <Trash2 size={13} />
                  </button>
                </motion.div>
              ))}
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  )
}
