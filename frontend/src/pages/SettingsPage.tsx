import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Settings, Clock, Users, Activity, User, Plus, Trash2, Loader2 } from 'lucide-react'
import { getSchedules, createSchedule, deleteSchedule, getContacts } from '../api/client'
import { SystemStatus } from '../components/SystemStatus'
import { SkeletonCard } from '../components/SkeletonCard'
import { useAppContext } from '../App'
import { useToast } from '../hooks/useToast'
import type { UserID } from '../api/client'

type Section = 'schedules' | 'contacts' | 'status' | 'profile'

const sections: { id: Section; label: string; icon: React.ElementType }[] = [
  { id: 'schedules', label: 'Schedules',     icon: Clock },
  { id: 'contacts',  label: 'Contacts',      icon: Users },
  { id: 'status',    label: 'System Status', icon: Activity },
  { id: 'profile',   label: 'User Profile',  icon: User },
]

// ── Schedules section ─────────────────────────────────
function SchedulesSection({ userId }: { userId: UserID }) {
  const { addToast } = useToast()
  const qc = useQueryClient()
  const [newText, setNewText] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['schedules', userId],
    queryFn: () => getSchedules(userId).then(r => r.data),
  })

  const createMut = useMutation({
    mutationFn: (text: string) => createSchedule(userId, text),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['schedules', userId] }); setNewText(''); addToast('Schedule created', 'success') },
    onError: () => addToast('Failed to create schedule', 'error'),
  })

  const deleteMut = useMutation({
    mutationFn: (id: string) => deleteSchedule(userId, id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['schedules', userId] }); addToast('Schedule deleted', 'info') },
  })

  const schedules: any[] = data?.schedules ?? []

  const friendlyCron = (expr: string) => {
    if (expr.startsWith('once:')) return `Once at ${new Date(expr.slice(5)).toLocaleString()}`
    if (expr === '0 8 * * *') return 'Daily at 8:00 AM'
    if (expr === '0 18 * * *') return 'Daily at 6:00 PM'
    return expr
  }

  return (
    <div className="space-y-4">
      {/* Add schedule */}
      <form
        onSubmit={e => { e.preventDefault(); if (newText.trim()) createMut.mutate(newText.trim()) }}
        className="flex gap-2"
      >
        <input
          value={newText}
          onChange={e => setNewText(e.target.value)}
          placeholder="Describe the schedule in plain English…"
          className="flex-1 glass-sm rounded-xl px-4 py-2.5 text-sm text-[#E2E8F0]
                     placeholder:text-[#4A6080] outline-none bg-transparent border border-[#1E3A5F]/40
                     focus:border-[#00D4FF]/40"
        />
        <button
          type="submit"
          disabled={createMut.isPending || !newText.trim()}
          className="flex items-center gap-1.5 px-4 py-2 rounded-xl text-sm font-medium
                     bg-[#00D4FF]/12 border border-[#00D4FF]/25 text-[#00D4FF]
                     hover:bg-[#00D4FF]/20 disabled:opacity-40 transition-colors"
        >
          {createMut.isPending ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}
          Add
        </button>
      </form>

      {isLoading && <SkeletonCard lines={3} />}

      {!isLoading && schedules.length === 0 && (
        <div className="glass rounded-2xl p-6 text-center">
          <Clock size={24} className="mx-auto text-[#1E3A5F] mb-2" />
          <p className="text-[#4A6080] text-sm">No schedules yet</p>
          <p className="text-xs text-[#4A6080] mt-1">Try: "Daily email digest at 8am" or "Remind me every Monday at 9am"</p>
        </div>
      )}

      <div className="space-y-2">
        {schedules.map((s: any) => (
          <motion.div
            key={s.id}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            className="glass-sm rounded-xl p-3.5 flex items-center justify-between gap-3"
          >
            <div className="min-w-0">
              <p className="text-sm text-[#E2E8F0] truncate">{s.label ?? s.action_type ?? 'Schedule'}</p>
              <p className="text-xs text-[#4A6080] mt-0.5 font-mono">
                {friendlyCron(s.cron_expression ?? '')}
              </p>
            </div>
            <motion.button
              whileTap={{ scale: 0.85 }}
              onClick={() => deleteMut.mutate(s.id)}
              className="shrink-0 text-[#4A6080] hover:text-[#FF4466] transition-colors"
            >
              <Trash2 size={14} />
            </motion.button>
          </motion.div>
        ))}
      </div>
    </div>
  )
}

// ── Contacts section ──────────────────────────────────
function ContactsSection() {
  const [search, setSearch] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['contacts'],
    queryFn: () => getContacts().then(r => r.data),
  })

  const contacts: any[] = (data?.contacts ?? []).filter((c: any) =>
    !search || c.full_name?.toLowerCase().includes(search.toLowerCase()) ||
    c.email?.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="space-y-4">
      {/* Search */}
      <input
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder="Search contacts…"
        className="w-full glass-sm rounded-xl px-4 py-2.5 text-sm text-[#E2E8F0]
                   placeholder:text-[#4A6080] outline-none bg-transparent border border-[#1E3A5F]/40
                   focus:border-[#00D4FF]/40"
      />

      {isLoading && <SkeletonCard lines={4} />}

      {!isLoading && contacts.length === 0 && (
        <div className="glass rounded-2xl p-6 text-center">
          <Users size={24} className="mx-auto text-[#1E3A5F] mb-2" />
          <p className="text-[#4A6080] text-sm">
            {search ? 'No contacts match your search' : 'No contacts yet'}
          </p>
        </div>
      )}

      <div className="space-y-2">
        {contacts.slice(0, 50).map((c: any) => (
          <div key={c.id} className="glass-sm rounded-xl px-4 py-3 flex items-center gap-3">
            <div
              className="w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold
                         bg-gradient-to-br from-[#00D4FF]/20 to-[#7B2FFF]/20
                         border border-[#1E3A5F]/40 text-[#00D4FF] shrink-0"
            >
              {(c.full_name ?? c.email ?? '?')[0].toUpperCase()}
            </div>
            <div className="min-w-0">
              <p className="text-sm text-[#E2E8F0] truncate">{c.full_name ?? '—'}</p>
              <p className="text-xs text-[#4A6080] truncate">{c.email ?? c.role ?? '—'}</p>
            </div>
            {c.company && (
              <span className="ml-auto shrink-0 text-xs text-[#4A6080]">{c.company}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Profile section ───────────────────────────────────
function ProfileSection() {
  const { userId, setUserId } = useAppContext()

  return (
    <div className="space-y-4">
      <div className="glass rounded-2xl p-5 space-y-4">
        <div className="flex items-center gap-4">
          <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-[#00D4FF] to-[#7B2FFF]
                          flex items-center justify-center text-white font-bold text-xl">
            {userId === 'user_1' ? '1' : '2'}
          </div>
          <div>
            <p className="font-semibold text-[#E2E8F0]">Active User</p>
            <p className="text-sm text-[#4A6080] font-mono">{userId}</p>
          </div>
        </div>

        <div className="border-t border-[#1E3A5F]/30 pt-4">
          <p className="text-xs text-[#4A6080] mb-3">Switch user context</p>
          <div className="flex gap-2">
            {(['user_1', 'user_2'] as UserID[]).map(id => (
              <button
                key={id}
                onClick={() => setUserId(id)}
                className={`flex-1 py-2.5 rounded-xl text-sm font-medium transition-all
                            ${userId === id
                              ? 'bg-[#00D4FF]/15 border border-[#00D4FF]/30 text-[#00D4FF]'
                              : 'glass-sm text-[#4A6080] hover:text-[#94A3B8]'}`}
              >
                {id === 'user_1' ? 'User 1 (Me)' : 'User 2 (Agent)'}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="glass-sm rounded-xl p-4 space-y-2">
        <p className="text-xs text-[#4A6080] font-semibold uppercase tracking-wide">Platform Info</p>
        {[
          ['Backend', 'FastAPI @ 192.168.1.155:8000'],
          ['LLM Smart', 'Qwen2.5-14B Q5_K_M'],
          ['LLM Fast', 'Qwen2.5-1.5B Q4_K_M'],
          ['Vector DB', 'Qdrant (fastembed)'],
        ].map(([k, v]) => (
          <div key={k} className="flex justify-between text-sm">
            <span className="text-[#4A6080]">{k}</span>
            <span className="text-[#94A3B8] text-xs font-mono">{v}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────
export default function SettingsPage() {
  const { userId } = useAppContext()
  const [activeSection, setActiveSection] = useState<Section>('schedules')

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-2xl mx-auto px-4 py-6 space-y-5 pb-24 md:pb-6">
        {/* Header */}
        <motion.div initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}>
          <div className="flex items-center gap-2">
            <Settings size={18} className="text-[#00D4FF]" />
            <h1 className="text-lg font-semibold text-[#E2E8F0]">Settings</h1>
          </div>
        </motion.div>

        {/* Section nav */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
          {sections.map(s => {
            const Icon = s.icon
            return (
              <button
                key={s.id}
                onClick={() => setActiveSection(s.id)}
                className={`flex items-center gap-2 px-3 py-2.5 rounded-xl text-sm font-medium
                            transition-all ${activeSection === s.id
                              ? 'bg-[#00D4FF]/12 border border-[#00D4FF]/25 text-[#00D4FF]'
                              : 'glass-sm text-[#4A6080] hover:text-[#94A3B8]'}`}
              >
                <Icon size={14} />
                {s.label}
              </button>
            )
          })}
        </div>

        {/* Section content */}
        <AnimatePresence mode="wait">
          <motion.div
            key={activeSection}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2 }}
          >
            {activeSection === 'schedules' && <SchedulesSection userId={userId} />}
            {activeSection === 'contacts' && <ContactsSection />}
            {activeSection === 'status' && <SystemStatus />}
            {activeSection === 'profile' && <ProfileSection />}
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  )
}
