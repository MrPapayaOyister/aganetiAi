import { NavLink } from 'react-router-dom'
import { motion } from 'framer-motion'
import { MessageSquare, BarChart2, FolderOpen, Inbox, Settings } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { useAppContext } from '../App'
import axios from 'axios'

const links = [
  { to: '/',          icon: MessageSquare, label: 'Assistant' },
  { to: '/analytics', icon: BarChart2,     label: 'Analytics' },
  { to: '/files',     icon: FolderOpen,    label: 'Files'     },
  { to: '/inbox',     icon: Inbox,         label: 'Inbox'     },
  { to: '/settings',  icon: Settings,      label: 'Settings'  },
]

export default function Sidebar() {
  const { userId, setUserId } = useAppContext()

  const { data: agentSummary } = useQuery({
    queryKey: ['agent-inbox-summary', userId],
    queryFn: () => axios.get('/api/agent/inbox/summary', { params: { user_id: userId } }).then(r => r.data),
    refetchInterval: 60_000,
    retry: false,
  })

  const badgeCounts: Record<string, number> = {
    '/inbox': agentSummary?.pending ?? agentSummary?.total ?? 0,
  }

  return (
    <aside className="w-20 h-screen flex flex-col items-center pt-6 pb-4 gap-1
                      bg-[#0D1628] border-r border-[#1E3A5F]/40">
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

      {/* User switcher */}
      <div className="flex flex-col items-center gap-1">
        {(['user_1', 'user_2'] as const).map(id => (
          <button
            key={id}
            onClick={() => setUserId(id)}
            title={id}
            className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold
                        transition-all ${userId === id
                          ? 'bg-gradient-to-br from-[#00D4FF]/40 to-[#7B2FFF]/40 text-[#00D4FF] border border-[#00D4FF]/30'
                          : 'bg-[#1E3A5F]/30 text-[#4A6080] hover:text-[#94A3B8]'}`}
          >
            {id.slice(-1)}
          </button>
        ))}
      </div>
    </aside>
  )
}
