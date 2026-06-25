import { NavLink } from 'react-router-dom'
import { motion } from 'framer-motion'
import { MessageSquare, BarChart2, FolderOpen, Inbox, Settings } from 'lucide-react'

const links = [
  { to: '/',          icon: MessageSquare, label: 'Chat'      },
  { to: '/analytics', icon: BarChart2,     label: 'Analytics' },
  { to: '/files',     icon: FolderOpen,    label: 'Files'     },
  { to: '/inbox',     icon: Inbox,         label: 'Inbox'     },
  { to: '/settings',  icon: Settings,      label: 'Settings'  },
]

export default function BottomNav() {
  return (
    <nav className="fixed bottom-0 left-0 right-0 z-50
                    bg-[#0D1628]/85 backdrop-blur-xl border-t border-[#1E3A5F]/40
                    flex justify-around items-center px-2 py-1.5 pb-safe">
      {links.map(({ to, icon: Icon, label }) => (
        <NavLink key={to} to={to} end={to === '/'} className="flex-1">
          {({ isActive }) => (
            <motion.div
              whileTap={{ scale: 0.9 }}
              animate={{ scale: isActive ? 1.05 : 1 }}
              transition={{ type: 'spring', stiffness: 400, damping: 22 }}
              className={`relative flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl
                          min-h-[44px] justify-center
                          ${isActive ? 'text-[#00D4FF]' : 'text-[#4A6080]'}`}
            >
              {isActive && (
                <motion.div
                  layoutId="bottomnav-pill"
                  className="absolute inset-0 rounded-xl bg-[#00D4FF]/10"
                  transition={{ type: 'spring', stiffness: 400, damping: 30 }}
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
