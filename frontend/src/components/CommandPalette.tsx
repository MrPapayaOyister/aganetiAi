import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion, AnimatePresence } from 'framer-motion'
import {
  MessageSquare, BarChart2, FolderOpen, Inbox, Settings,
  LogOut, Search, CornerDownLeft, Command as CmdIcon,
} from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'

interface Cmd {
  id: string
  label: string
  hint?: string
  icon: React.ElementType
  run: () => void
  keywords?: string
}

/**
 * ⌘K / Ctrl+K command palette — Raycast/Linear style.
 * Mounted once at the app root.
 */
export default function CommandPalette() {
  const [open, setOpen] = useState(false)
  const [q, setQ] = useState('')
  const [active, setActive] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)
  const navigate = useNavigate()
  const { signOut } = useAuth()

  const commands: Cmd[] = useMemo(() => [
    { id: 'chat',      label: 'Go to Assistant',  hint: 'Chat',     icon: MessageSquare, run: () => navigate('/'),          keywords: 'home chat ask' },
    { id: 'analytics', label: 'Go to Analytics',  hint: 'Insights', icon: BarChart2,     run: () => navigate('/analytics'), keywords: 'stats tasks board' },
    { id: 'files',     label: 'Go to Files',      hint: 'Documents',icon: FolderOpen,    run: () => navigate('/files'),     keywords: 'upload ingest docs' },
    { id: 'inbox',     label: 'Go to Inbox',      hint: 'Agents',   icon: Inbox,         run: () => navigate('/inbox'),     keywords: 'agent messages' },
    { id: 'settings',  label: 'Go to Settings',   hint: 'Config',   icon: Settings,      run: () => navigate('/settings'),  keywords: 'schedules contacts profile' },
    { id: 'signout',   label: 'Sign out',         hint: 'Account',  icon: LogOut,        run: () => signOut(),              keywords: 'logout leave exit' },
  ], [navigate, signOut])

  const filtered = useMemo(() => {
    const s = q.trim().toLowerCase()
    if (!s) return commands
    return commands.filter(c =>
      c.label.toLowerCase().includes(s) || c.keywords?.includes(s)
    )
  }, [q, commands])

  // Global hotkey
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setOpen(o => !o)
      }
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    if (open) {
      setQ(''); setActive(0)
      setTimeout(() => inputRef.current?.focus(), 40)
    }
  }, [open])

  useEffect(() => { setActive(0) }, [q])

  const exec = (c?: Cmd) => {
    if (!c) return
    c.run()
    setOpen(false)
  }

  const onListKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(a => Math.min(a + 1, filtered.length - 1)) }
    if (e.key === 'ArrowUp')   { e.preventDefault(); setActive(a => Math.max(a - 1, 0)) }
    if (e.key === 'Enter')     { e.preventDefault(); exec(filtered[active]) }
  }

  const EASE = [0.16, 1, 0.3, 1] as const
  const SOFT = [0.34, 1.56, 0.64, 1] as const

  return (
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-[200] flex items-start justify-center px-4 pt-[18vh]"
             onClick={() => setOpen(false)}>
          {/* STAGE 1 — backdrop: blur 0→20px + fade, leads the panel */}
          <motion.div
            className="absolute inset-0"
            style={{ background: 'rgba(4,6,11,0.6)' }}
            initial={{ opacity: 0, backdropFilter: 'blur(0px)' }}
            animate={{ opacity: 1, backdropFilter: 'blur(20px)' }}
            exit={{ opacity: 0, backdropFilter: 'blur(0px)', transition: { duration: 0.24, ease: EASE } }}
            transition={{ duration: 0.34, ease: EASE }}
          />

          {/* STAGE 2 — content panel: scale + fade + lift, gentle overshoot, delayed */}
          <motion.div
            onClick={e => e.stopPropagation()}
            onKeyDown={onListKey}
            initial={{ opacity: 0, scale: 0.93, y: -8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.95, y: -8, transition: { duration: 0.18, ease: EASE } }}
            transition={{ duration: 0.46, ease: SOFT, delay: 0.1 }}
            className="relative w-full max-w-lg glass-strong rounded-2xl overflow-hidden shadow-2xl"
            style={{ borderRadius: 18 }}
          >
            {/* Search — ring glow appears on the input */}
            <motion.div
              initial={{ boxShadow: 'inset 0 0 0 0 rgba(0,212,255,0)' }}
              animate={{ boxShadow: 'inset 0 -1px 0 0 rgba(0,212,255,0.25)' }}
              transition={{ duration: 0.2, ease: EASE, delay: 0.3 }}
              className="flex items-center gap-3 px-4 py-3.5 border-b border-[#1E3A5F]/40"
            >
              <Search size={16} className="text-[#00D4FF]" />
              <input
                ref={inputRef}
                value={q}
                onChange={e => setQ(e.target.value)}
                placeholder="Type a command or search…"
                className="flex-1 bg-transparent outline-none text-sm text-[#E2E8F0] placeholder:text-[#4A6080]"
              />
              <kbd className="text-[10px] text-[#4A6080] border border-[#1E3A5F]/60 rounded px-1.5 py-0.5">ESC</kbd>
            </motion.div>

            {/* List */}
            <div className="max-h-[320px] overflow-y-auto py-2">
              {filtered.length === 0 && (
                <div className="px-4 py-8 text-center text-sm text-[#4A6080]">No matching commands</div>
              )}
              {filtered.map((c, i) => {
                const Icon = c.icon
                const isActive = i === active
                return (
                  <motion.button
                    key={c.id}
                    onMouseEnter={() => setActive(i)}
                    onClick={() => exec(c)}
                    initial={{ opacity: 0, y: 4 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.24, ease: EASE, delay: 0.18 + i * 0.03 }}
                    className="relative w-full flex items-center gap-3 px-4 py-2.5 text-left"
                  >
                    {/* smooth highlight that glides between rows on keyboard nav */}
                    {isActive && (
                      <motion.div layoutId="cmd-active"
                        className="absolute inset-x-2 inset-y-0.5 rounded-lg bg-[#00D4FF]/10"
                        transition={{ type: 'spring', stiffness: 500, damping: 38 }} />
                    )}
                    <div className={`relative z-10 w-7 h-7 rounded-lg flex items-center justify-center shrink-0
                      ${isActive ? 'bg-[#00D4FF]/15 text-[#00D4FF]' : 'bg-white/[0.04] text-[#94A3B8]'}`}>
                      <Icon size={15} />
                    </div>
                    <span className={`relative z-10 text-sm flex-1 ${isActive ? 'text-[#E2E8F0]' : 'text-[#94A3B8]'}`}>
                      {c.label}
                    </span>
                    {c.hint && <span className="relative z-10 text-[11px] text-[#4A6080]">{c.hint}</span>}
                    {isActive && <CornerDownLeft size={13} className="relative z-10 text-[#00D4FF]" />}
                  </motion.button>
                )
              })}
            </div>

            {/* Footer */}
            <div className="flex items-center justify-between px-4 py-2.5 border-t border-[#1E3A5F]/40 text-[10px] text-[#4A6080]">
              <span className="flex items-center gap-1.5"><CmdIcon size={11} /> Aria Command</span>
              <span className="flex items-center gap-2">
                <span>↑↓ navigate</span><span>↵ select</span>
              </span>
            </div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
