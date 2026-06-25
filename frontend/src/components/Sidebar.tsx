import { NavLink } from 'react-router-dom'
import { motion } from 'framer-motion'
import { MessageSquare, BarChart2, FolderOpen, Inbox, Mail, Settings, LogOut } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useAppContext } from '../App'
import { useAuth } from '../contexts/AuthContext'
import axios from 'axios'

const HEALTH_COLORS: Record<string, string> = {
  ok: '#00FF88', degraded: '#FFB800', down: '#FF4466',
}

const links = [
  { to: '/',          icon: MessageSquare, label: 'Assistant' },
  { to: '/analytics', icon: BarChart2,     label: 'Analytics' },
  { to: '/files',     icon: FolderOpen,    label: 'Files'     },
  { to: '/inbox',     icon: Inbox,         label: 'Inbox'     },
  { to: '/drafts',    icon: Mail,          label: 'Drafts'    },
  { to: '/settings',  icon: Settings,      label: 'Settings'  },
]

export default function Sidebar() {
  const { userId } = useAppContext()
  const { user, signOut } = useAuth()

  const { data: agentSummary } = useQuery({
    queryKey: ['agent-inbox-summary', userId],
    queryFn: () => axios.get('/api/agent/inbox/summary', { params: { user_id: userId } }).then(r => r.data),
    refetchInterval: 60_000,
    retry: false,
  })

  const { data: health } = useQuery({
    queryKey: ['health-services'],
    queryFn: () => axios.get('/api/health/services').then(r => r.data),
    refetchInterval: 30_000,
    retry: false,
  })

  const overall: string = health?.overall ?? 'down'
  const healthColor = HEALTH_COLORS[overall] ?? '#4A6080'
  const initial = (user?.email?.[0] ?? 'A').toUpperCase()

  const badgeCounts: Record<string, number> = {
    '/inbox': agentSummary?.pending ?? agentSummary?.total ?? 0,
  }

  return (
    <aside className="w-20 h-screen flex flex-col items-center pt-6 pb-4 gap-1
                      bg-[#232838] border-r border-[#1E3A5F]/40">
      {/* Logo / orb */}
      <div className="mb-5">
        <motion.div
          whileHover={{ scale: 1.08 }}
          className="w-10 h-10 rounded-xl bg-gradient-to-br from-[#00D4FF] to-[#7B2FFF]
                     flex items-center justify-center text-white font-bold text-sm
                     shadow-[0_0_16px_rgba(0,212,255,0.3)]"
        >
          A
        </motion.div>
      </div>

      {links.map(({ to, icon: Icon, label }) => {
        const badge = badgeCounts[to] ?? 0
        return (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `relative flex flex-col items-center gap-1 w-14 py-2.5 rounded-xl
               transition-all duration-200
               ${isActive
                 ? 'bg-[#00D4FF]/10 text-[#00D4FF]'
                 : 'text-[#4A6080] hover:text-[#94A3B8] hover:bg-white/5'}`
            }
          >
            {({ isActive }) => (
              <>
                <div className="relative">
                  <Icon size={20} />
                  {badge > 0 && (
                    <span className="absolute -top-1 -right-1 w-3.5 h-3.5 rounded-full
                                     bg-[#FF4466] text-white text-[8px] font-bold
                                     flex items-center justify-center">
                      {badge > 9 ? '9+' : badge}
                    </span>
                  )}
                </div>
                <span className="text-[9px] font-medium">{label}</span>
                {isActive && (
                  <motion.div
                    layoutId="sidebar-indicator"
                    className="absolute left-0 top-2 bottom-2 w-0.5 rounded-full bg-[#00D4FF]"
                    transition={{ type: 'spring', stiffness: 400, damping: 30 }}
                  />
                )}
              </>
            )}
          </NavLink>
        )
      })}

      <div className="flex-1" />

      {/* System health */}
      <div
        className="flex items-center gap-1.5 mb-3 px-2 py-1 rounded-full bg-white/[0.03]"
        title={`System: ${overall}`}
      >
        <motion.span
          className="w-2 h-2 rounded-full"
          style={{ background: healthColor, boxShadow: `0 0 8px ${healthColor}` }}
          animate={{ opacity: [1, 0.4, 1] }}
          transition={{ duration: 2, repeat: Infinity }}
        />
      </div>

      {/* User avatar → sign out */}
      <motion.button
        whileHover={{ scale: 1.08 }}
        whileTap={{ scale: 0.92 }}
        onClick={signOut}
        title={`${user?.email ?? 'Account'} — sign out`}
        className="group relative w-9 h-9 rounded-full flex items-center justify-center
                   bg-gradient-to-br from-[#00D4FF]/30 to-[#7B2FFF]/30
                   border border-[#00D4FF]/30 text-[#00D4FF] text-sm font-bold"
      >
        <span className="group-hover:opacity-0 transition-opacity">{initial}</span>
        <LogOut size={15} className="absolute opacity-0 group-hover:opacity-100 transition-opacity text-[#FF4466]" />
      </motion.button>
    </aside>
  )
}
