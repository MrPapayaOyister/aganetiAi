import { NavLink } from 'react-router-dom'
import { motion } from 'framer-motion'
import { MessageSquare, BarChart2, LayoutDashboard, FolderOpen, Inbox, Settings } from 'lucide-react'

const links = [
  { to: '/',          icon: MessageSquare,   label: 'Chat'      },
  { to: '/analytics', icon: BarChart2,       label: 'Analytics' },
  { to: '/dashboard', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/files',     icon: FolderOpen,      label: 'Files'     },
  { to: '/inbox',     icon: Inbox,         label: 'Inbox'     },
  { to: '/settings',  icon: Settings,      label: 'Settings'  },
]

export default function BottomNav() {
  return (
    <nav className="fixed bottom-0 left-0 right-0 z-50
                    bg-[#232838]/85 backdrop-blur-xl border-t border-[#1E3A5F]/40
                    flex justify-around items-center px-2 py-1.5 pb-safe">
      {links.map(({ to, icon: Icon, label }) => (
        <NavLink key={to} to={to} end={to === '/'} className="flex-1">
          {({ isActive }) => (
            <motion.div
              whileTap={{ scale: 0.92, y: 2 }}
              animate={{ scale: isActive ? 1.05 : 1 }}
              transition={{ type: 'spring', stiffness: 300, damping: 24 }}
              className={`relative flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl
                          min-h-[44px] justify-center
                          ${isActive ? 'text-[#00D4FF]' : 'text-[#4A6080]'}`}
            >
              {isActive && (
                <motion.div
                  layoutId="bottomnav-pill"
                  className="absolute inset-0 rounded-xl bg-[#00D4FF]/10
                             shadow-[inset_0_2px_8px_rgba(0,212,255,0.14)]"
                  transition={{ type: 'spring', stiffness: 300, damping: 30 }}
                />
              )}
              <Icon size={20} className="relative z-10" />
              <span className="text-[10px] font-medium relative z-10">{label}</span>
            </motion.div>
          )}
        </NavLink>
      ))}
    </nav>
  )
}
