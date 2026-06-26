import { useState } from 'react'
import { usePrefs } from '../contexts/PrefsContext'
import { isMuted, setMuted } from '../lib/sound'
import { PressButton } from '../components/ui/PressButton'
import { Toggle } from '../components/ui/Toggle'
import { ConfirmModal } from '../components/ui/ConfirmModal'
import { motion, AnimatePresence } from 'framer-motion'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Settings, Clock, Users, Activity, User,
  Plus, Trash2, Loader2, Link2, Link2Off, CheckCircle,
} from 'lucide-react'
import {
  getSchedules, createSchedule, deleteSchedule, getContacts,
  getProviderStatus, disconnectProvider,
} from '../api/client'
import { SystemStatus } from '../components/SystemStatus'
import { SkeletonCard } from '../components/SkeletonCard'
import { useAppContext } from '../App'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../hooks/useToast'

type Section = 'schedules' | 'contacts' | 'status' | 'account'

const sections: { id: Section; label: string; icon: React.ElementType }[] = [
  { id: 'schedules', label: 'Schedules',     icon: Clock },
  { id: 'contacts',  label: 'Contacts',      icon: Users },
  { id: 'status',    label: 'System Status', icon: Activity },
  { id: 'account',   label: 'Account',       icon: User },
]

// ── Schedules section ─────────────────────────────────
function SchedulesSection({ userId }: { userId: string }) {
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
function ContactsSection({ userId }: { userId: string }) {
  const [search, setSearch] = useState('')

  const { data, isLoading } = useQuery({
    queryKey: ['contacts', userId],
    queryFn: () => getContacts(userId).then(r => r.data),
    enabled: !!userId,
  })

  const contacts: any[] = (data?.contacts ?? []).filter((c: any) =>
    !search || c.full_name?.toLowerCase().includes(search.toLowerCase()) ||
    c.email?.toLowerCase().includes(search.toLowerCase())
  )

  return (
    <div className="space-y-4">
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

// ── Account section ───────────────────────────────────
function AccountSection() {
  const { user, userId, signOut } = useAuth()
  const { addToast } = useToast()
  const qc = useQueryClient()
  const { prefs, setPrefs } = usePrefs()
  const [agentNameInput, setAgentNameInput] = useState(prefs.agentName)
  const [displayNameInput, setDisplayNameInput] = useState(prefs.displayName)
  const [soundOn, setSoundOn] = useState(!isMuted())
  const [confirmSignOut, setConfirmSignOut] = useState(false)

  const email = user?.email ?? '—'
  const initials = email !== '—' ? email.slice(0, 2).toUpperCase() : 'AI'
  const shortId = userId ? `${userId.slice(0, 8)}…` : '—'

  const { data: providerData, isLoading: providerLoading } = useQuery({
    queryKey: ['provider-status', userId],
    queryFn: () => getProviderStatus(userId).then(r => r.data),
    enabled: !!userId,
    staleTime: 30_000,
  })

  const disconnectMut = useMutation({
    mutationFn: (provider: string) => disconnectProvider(provider, userId),
    onSuccess: (_, provider) => {
      qc.invalidateQueries({ queryKey: ['provider-status', userId] })
      addToast(`${provider} disconnected`, 'info')
    },
    onError: () => addToast('Failed to disconnect', 'error'),
  })

  const handleConnectGoogle = () => {
    window.location.href = `/api/auth/google/connect?user_id=${encodeURIComponent(userId)}&redirect_uri=/settings`
  }

  const google = providerData?.google

  return (
    <div className="space-y-4">
      {/* Background intensity */}
      <div className="glass rounded-2xl p-5 space-y-3">
        <p className="text-xs font-semibold text-[#4A6080] uppercase tracking-wide">Background intensity</p>
        <div className="grid grid-cols-4 gap-2">
          {(['off', 'calm', 'standard', 'cinematic'] as const).map(level => {
            const active = prefs.bgIntensity === level
            return (
              <button
                key={level}
                onClick={() => setPrefs({ bgIntensity: level })}
                className={`px-3 py-2 rounded-xl text-xs font-medium border transition-all capitalize
                  ${active
                    ? 'bg-[#00D4FF]/15 border-[#00D4FF]/40 text-[#00D4FF]'
                    : 'bg-white/[0.02] border-white/[0.06] text-[#9AA7BD] hover:border-white/[0.12] hover:text-[#E2E8F0]'}`}
              >
                {level}
              </button>
            )
          })}
        </div>
        <p className="text-xs text-[#4A6080]">
          Off saves battery · Cinematic enables liquid distortion + brighter colors.
        </p>
      </div>

      {/* Personalisation */}
      <div className="glass rounded-2xl p-5 space-y-3">
        <p className="text-xs font-semibold text-[#4A6080] uppercase tracking-wide">Personalisation</p>
        <div>
          <label className="text-xs font-medium text-[#9AA7BD] block mb-1.5">Assistant name</label>
          <input
            value={agentNameInput}
            onChange={e => setAgentNameInput(e.target.value)}
            onBlur={() => setPrefs({ agentName: agentNameInput.trim() || 'Aria' })}
            placeholder="Aria"
            maxLength={30}
            className="w-full neu-inset rounded-xl px-4 py-2.5 text-sm text-[#E2E8F0]
                       placeholder:text-[#4A6080] outline-none"
          />
        </div>
        <div>
          <label className="text-xs font-medium text-[#9AA7BD] block mb-1.5">
            Your display name <span className="text-[#4A6080] font-normal">(optional)</span>
          </label>
          <input
            value={displayNameInput}
            onChange={e => setDisplayNameInput(e.target.value)}
            onBlur={() => setPrefs({ displayName: displayNameInput.trim() })}
            placeholder="Your first name…"
            maxLength={50}
            className="w-full neu-inset rounded-xl px-4 py-2.5 text-sm text-[#E2E8F0]
                       placeholder:text-[#4A6080] outline-none"
          />
        </div>
        <p className="text-xs text-[#4A6080]">Saved automatically on blur.</p>

        {/* Sound effects toggle (Item 8) — shared Toggle component */}
        <div className="pt-1">
          <Toggle
            checked={soundOn}
            onChange={(next) => { setSoundOn(next); setMuted(!next) }}
            label="Interface sounds"
            description="Soft click & status cues"
          />
        </div>
      </div>

      {/* Identity card */}
      <div className="glass rounded-2xl p-5 space-y-4">
        <div className="flex items-center gap-4">
          <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-[#00D4FF] to-[#7B2FFF]
                          flex items-center justify-center text-white font-bold text-xl shrink-0">
            {initials}
          </div>
          <div className="min-w-0">
            <p className="font-semibold text-[#E2E8F0] truncate">{email}</p>
            <p className="text-xs text-[#4A6080] font-mono mt-0.5">{shortId}</p>
          </div>
        </div>

        <div className="border-t border-[#1E3A5F]/30 pt-4 flex justify-end">
          <PressButton variant="danger" size="sm" onClick={() => setConfirmSignOut(true)}>
            Sign out
          </PressButton>
        </div>
      </div>

      <ConfirmModal
        open={confirmSignOut}
        title="Sign out of Aria?"
        description="You'll need to sign in again to access your assistant, tasks and connected apps."
        confirmLabel="Sign out"
        danger
        onConfirm={() => { setConfirmSignOut(false); signOut() }}
        onCancel={() => setConfirmSignOut(false)}
      />

      {/* Connected Apps */}
      <div className="glass rounded-2xl p-5 space-y-3">
        <p className="text-xs font-semibold text-[#4A6080] uppercase tracking-wide">Connected Apps</p>

        {providerLoading && <SkeletonCard lines={2} />}

        {!providerLoading && (
          <div className="space-y-2">
            {/* Google */}
            <div className="glass-sm rounded-xl p-4 flex items-center gap-3">
              <svg className="w-6 h-6 shrink-0" viewBox="0 0 24 24">
                <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
                <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
                <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
                <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
              </svg>

              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-[#E2E8F0]">Google</p>
                {google?.connected ? (
                  <p className="text-xs text-[#4A6080] truncate">{google.email ?? 'Connected'}</p>
                ) : (
                  <p className="text-xs text-[#4A6080]">Gmail · Calendar · Contacts</p>
                )}
              </div>

              {google?.connected ? (
                <div className="flex items-center gap-2 shrink-0">
                  <CheckCircle size={14} className="text-[#00FF88]" />
                  <button
                    onClick={() => disconnectMut.mutate('google')}
                    disabled={disconnectMut.isPending}
                    className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs
                               text-[#FF4466] bg-[#FF4466]/10 border border-[#FF4466]/20
                               hover:bg-[#FF4466]/20 disabled:opacity-40 transition-colors"
                  >
                    {disconnectMut.isPending
                      ? <Loader2 size={12} className="animate-spin" />
                      : <Link2Off size={12} />}
                    Disconnect
                  </button>
                </div>
              ) : (
                <button
                  onClick={handleConnectGoogle}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium
                             bg-[#00D4FF]/12 border border-[#00D4FF]/25 text-[#00D4FF]
                             hover:bg-[#00D4FF]/20 transition-colors shrink-0"
                >
                  <Link2 size={12} />
                  Connect
                </button>
              )}
            </div>

            {/* Microsoft placeholder */}
            <div className="glass-sm rounded-xl p-4 flex items-center gap-3 opacity-50">
              <svg className="w-6 h-6 shrink-0" viewBox="0 0 24 24">
                <path fill="#F25022" d="M1 1h10v10H1z"/>
                <path fill="#7FBA00" d="M13 1h10v10H13z"/>
                <path fill="#00A4EF" d="M1 13h10v10H1z"/>
                <path fill="#FFB900" d="M13 13h10v10H13z"/>
              </svg>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-[#E2E8F0]">Microsoft 365</p>
                <p className="text-xs text-[#4A6080]">Outlook · Teams · OneDrive</p>
              </div>
              <span className="text-xs text-[#4A6080] shrink-0">Coming soon</span>
            </div>
          </div>
        )}

        {!google?.connected && !providerLoading && (
          <p className="text-xs text-[#4A6080] pt-1 leading-relaxed">
            Connect Google to unlock your real inbox, agenda, and contacts inside Aria.
          </p>
        )}
      </div>

      {/* Platform info */}
      <div className="glass-sm rounded-xl p-4 space-y-2">
        <p className="text-xs text-[#4A6080] font-semibold uppercase tracking-wide">Platform</p>
        {[
          ['LLM Smart', 'Qwen2.5-14B Q5_K_M'],
          ['LLM Fast', 'Qwen2.5-1.5B Q4_K_M'],
          ['Vector DB', 'Qdrant (fastembed)'],
          ['STT', 'faster-whisper (local)'],
          ['TTS', 'Kokoro (local)'],
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
            {activeSection === 'contacts'  && <ContactsSection userId={userId} />}
            {activeSection === 'status'    && <SystemStatus />}
            {activeSection === 'account'   && <AccountSection />}
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  )
}
