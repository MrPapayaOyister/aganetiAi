import { NavLink } from 'react-router-dom'
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
                    bg-[#0D1628]/90 backdrop-blur-xl border-t border-[#1E3A5F]/40
                    flex justify-around items-center px-2 py-2 pb-safe">
      {links.map(({ to, icon: Icon, label }) => (
        <NavLink
          key={to}
          to={to}
          end={to === '/'}
          className={({ isActive }) =>
            `flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl transition-all duration-200
             ${isActive ? 'text-[#00D4FF]' : 'text-[#4A6080]'}`
          }
        >
          <Icon size={20} />
          <span className="text-[10px] font-medium">{label}</span>
        </NavLink>
      ))}
    </nav>
  )
}