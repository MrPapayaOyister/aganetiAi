import { motion } from 'framer-motion'
import { useQuery } from '@tanstack/react-query'
import { Mail, CheckSquare, Calendar, MessageSquare, Clock, Wrench, Sparkles } from 'lucide-react'
import { StatCard } from '../components/StatCard'
import { TaskBoard } from '../components/TaskBoard'
import { AgendaTimeline } from '../components/AgendaTimeline'
import { EmailDigestPanel } from '../components/EmailDigestPanel'
import {
  getInboxCount, getAgenda,
  getAnalyticsSummary, getAnalyticsTools, getAnalyticsTasks, getAnalyticsActiveHours,
} from '../api/client'
import { DonutChart, BarChart } from '../components/charts/Charts'
import { Skeleton } from '../components/ui/Skeleton'
import { useAppContext } from '../App'
import axios from 'axios'

const PRI_COLORS: Record<string, string> = {
  urgent: '#FF4466', high: '#FFB800', medium: '#38DBFF', low: '#5C6B85',
}

export default function AnalyticsPage() {
  const { userId } = useAppContext()

  const { data: summary } = useQuery({
    queryKey: ['analytics-summary', userId],
    queryFn: () => axios.get('/api/analytics', { params: { metric: 'summary', user_id: userId } }).then(r => r.data),
    refetchInterval: 60_000, retry: false,
  })
  const { data: contacts } = useQuery({
    queryKey: ['analytics-contacts', userId],
    queryFn: () => axios.get('/api/analytics', { params: { metric: 'top_contacts', user_id: userId } }).then(r => r.data),
    staleTime: 300_000, retry: false,
  })

  const done = summary?.done ?? 0
  const pending = summary?.pending ?? 0
  const byPri: Record<string, number> = summary?.pending_by_priority ?? {}
  const priBars = ['urgent', 'high', 'medium', 'low']
    .filter(p => byPri[p])
    .map(p => ({ label: p, value: byPri[p], color: PRI_COLORS[p] }))
  const contactBars = (contacts?.top ?? []).slice(0, 5).map((c: { sender: string; count: number }) => ({
    label: (c.sender || '').split('<')[0].split('@')[0].trim().slice(0, 14) || '—',
    value: c.count, color: '#7B2FFF',
  }))

  const { data: inboxData } = useQuery({
    queryKey: ['inbox-count', userId],
    queryFn: () => getInboxCount(userId).then(r => r.data),
    refetchInterval: 60_000,
  })

  const { data: agendaData } = useQuery({
    queryKey: ['agenda', userId],
    queryFn: () => getAgenda(userId).then(r => r.data),
    staleTime: 300_000,
  })

  const { data: agentInboxData } = useQuery({
    queryKey: ['agent-inbox-summary', userId],
    queryFn: () => axios.get('/api/agent/inbox/summary', { params: { user_id: userId } }).then(r => r.data),
    refetchInterval: 60_000,
  })

  const { data: tasksData } = useQuery({
    queryKey: ['tasks-count', userId],
    queryFn: () => axios.get('/api/tasks', { params: { user_id: userId, status: 'pending' } }).then(r => r.data),
  })

  const pendingTasks = Array.isArray(tasksData) ? tasksData.length : 0
  const todayMeetings = agendaData?.agenda?.length ?? 0
  const unreadEmails = inboxData?.unread ?? 0
  const agentMessages = agentInboxData?.total ?? agentInboxData?.pending ?? 0

  // ── Operational analytics (P5) ────────────────────────────────────
  const { data: opSummary } = useQuery({
    queryKey: ['ops-summary', userId],
    queryFn: () => getAnalyticsSummary('7d', userId).then(r => r.data),
    refetchInterval: 60_000, retry: false,
  })
  const { data: opTools, isLoading: opToolsLoading } = useQuery({
    queryKey: ['ops-tools', userId],
    queryFn: () => getAnalyticsTools('30d', userId).then(r => r.data),
    refetchInterval: 120_000, retry: false,
  })
  const { data: opTasks, isLoading: opTasksLoading } = useQuery({
    queryKey: ['ops-tasks', userId],
    queryFn: () => getAnalyticsTasks('30d', userId).then(r => r.data),
    refetchInterval: 120_000, retry: false,
  })
  const { data: opHours } = useQuery({
    queryKey: ['ops-hours', userId],
    queryFn: () => getAnalyticsActiveHours('30d', userId).then(r => r.data),
    refetchInterval: 300_000, retry: false,
  })

  const totalMessages = (opSummary?.messages_user ?? 0) + (opSummary?.messages_agent ?? 0)
  const toolCalls = opSummary?.tool_calls ?? 0
  const avgRespMs = opSummary?.avg_response_ms ?? null
  const avgRespS = avgRespMs != null ? Math.round(avgRespMs / 100) / 10 : 0
  const initiatives = opSummary?.initiatives ?? 0

  const toolBars = (opTools?.tools ?? []).slice(0, 6).map((t: { name: string; calls: number }) => ({
    label: t.name.replace(/_/g, ' ').slice(0, 14), value: t.calls, color: '#00D4FF',
  }))
  const opCreated = opTasks?.created ?? 0
  const opCompleted = opTasks?.completed ?? 0
  const byHour: number[] = opHours?.by_hour ?? []
  const maxHour = Math.max(1, ...byHour)

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-6xl mx-auto px-4 py-6 space-y-6 pb-24 md:pb-6">
        {/* Page title */}
        <motion.div initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}>
          <h1 className="text-lg font-semibold text-[#E2E8F0]">Analytics</h1>
          <p className="text-sm text-[#4A6080] mt-0.5">Your workspace at a glance</p>
        </motion.div>

        {/* Stat cards */}
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.1 }}
          className="grid grid-cols-2 lg:grid-cols-4 gap-3"
        >
          <StatCard label="Unread Emails"   value={unreadEmails}   icon={Mail}          color="#00D4FF" trend={unreadEmails > 5 ? 'up' : 'flat'} />
          <StatCard label="Pending Tasks"   value={pendingTasks}   icon={CheckSquare}   color="#7B2FFF" trend={pendingTasks > 3 ? 'up' : 'flat'} />
          <StatCard label="Meetings Today"  value={todayMeetings}  icon={Calendar}      color="#00FF88" />
          <StatCard label="Agent Messages"  value={agentMessages}  icon={MessageSquare} color="#FFB800" />
        </motion.div>

        {/* Charts row */}
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.15 }}
          className="grid grid-cols-1 md:grid-cols-2 gap-4"
        >
          <div className="neu rounded-2xl p-5">
            <h2 className="t-heading mb-4">Task completion</h2>
            <DonutChart
              segments={[
                { label: 'Done', value: done, color: '#00FF88' },
                { label: 'Pending', value: pending, color: '#38DBFF' },
              ]}
              centerValue={`${done + pending ? Math.round((done / (done + pending)) * 100) : 0}%`}
              centerLabel="complete"
            />
          </div>
          <div className="neu rounded-2xl p-5">
            <h2 className="t-heading mb-4">Open tasks by priority</h2>
            <BarChart data={priBars} />
          </div>
          <div className="neu rounded-2xl p-5 md:col-span-2">
            <h2 className="t-heading mb-4">Most-contacted senders</h2>
            <BarChart data={contactBars} />
          </div>
        </motion.div>

        {/* ── Operations (P5 — agent activity from the events log) ── */}
        <motion.div
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.18 }}
          className="space-y-3"
        >
          <h2 className="text-sm font-semibold text-[#94A3B8] px-1">Agent operations · last 7 days</h2>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
            <StatCard label="Messages"      value={totalMessages} icon={MessageSquare} color="#00D4FF" />
            <StatCard label="Tool Calls"    value={toolCalls}     icon={Wrench}        color="#7B2FFF" />
            <StatCard label="Avg Response"  value={avgRespS}      icon={Clock}         color="#00FF88" unit="s" />
            <StatCard label="Initiatives"   value={initiatives}   icon={Sparkles}      color="#F59E0B" />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {opToolsLoading ? <Skeleton variant="chart" /> : (
              <div className="neu rounded-2xl p-5">
                <h2 className="t-heading mb-4">Tool usage (30d)</h2>
                <BarChart data={toolBars} />
              </div>
            )}
            {opTasksLoading ? <Skeleton variant="chart" /> : (
              <div className="neu rounded-2xl p-5">
                <h2 className="t-heading mb-4">Task funnel (30d)</h2>
                <DonutChart
                  segments={[
                    { label: 'Completed', value: opCompleted, color: '#00FF88' },
                    { label: 'Created',   value: Math.max(0, opCreated - opCompleted), color: '#38DBFF' },
                  ]}
                  centerValue={`${opCreated ? Math.round((opCompleted / opCreated) * 100) : 0}%`}
                  centerLabel="completion"
                />
              </div>
            )}
            <div className="neu rounded-2xl p-5 md:col-span-2">
              <h2 className="t-heading mb-4">Most active hours (30d)</h2>
              {byHour.length === 24 ? (
                <div className="flex items-end gap-[3px] h-24">
                  {byHour.map((v, h) => (
                    <div key={h} className="flex-1 flex flex-col items-center justify-end gap-1" title={`${h}:00 — ${v}`}>
                      <motion.div
                        className="w-full rounded-sm"
                        style={{ background: v ? '#00D4FF' : 'rgba(255,255,255,0.04)' }}
                        initial={{ height: 0 }}
                        animate={{ height: `${(v / maxHour) * 100}%` }}
                        transition={{ delay: h * 0.01, type: 'spring', stiffness: 120, damping: 20 }}
                      />
                      {h % 6 === 0 && <span className="text-[8px] text-[#4A6080] tabular-nums">{h}</span>}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="t-caption">No activity yet</div>
              )}
            </div>
          </div>
        </motion.div>

        {/* Two-column layout */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {/* Task board — 2/3 width */}
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.2 }}
            className="lg:col-span-2 space-y-3"
          >
            <h2 className="text-sm font-semibold text-[#94A3B8] px-1">Task Board</h2>
            <TaskBoard userId={userId} />
          </motion.div>

          {/* Right column — agenda + digest */}
          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.3 }}
            className="space-y-4"
          >
            <AgendaTimeline userId={userId} />
            <EmailDigestPanel userId={userId} />
          </motion.div>
        </div>
      </div>
    </div>
  )
}
