import { motion } from 'framer-motion'
import { useQuery } from '@tanstack/react-query'
import { Mail, CheckSquare, Calendar, MessageSquare } from 'lucide-react'
import { StatCard } from '../components/StatCard'
import { TaskBoard } from '../components/TaskBoard'
import { AgendaTimeline } from '../components/AgendaTimeline'
import { EmailDigestPanel } from '../components/EmailDigestPanel'
import { getInboxCount, getAgenda } from '../api/client'
import { useAppContext } from '../App'
import axios from 'axios'

export default function AnalyticsPage() {
  const { userId } = useAppContext()

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
