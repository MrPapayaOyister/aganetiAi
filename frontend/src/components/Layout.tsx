import type { ReactNode } from 'react'
import BottomNav from './BottomNav'
import Sidebar from './Sidebar'

export default function Layout({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-dvh w-screen overflow-hidden bg-transparent">
      {/* Desktop sidebar */}
      <div className="hidden md:flex">
        <Sidebar />
      </div>

      {/* Main content — takes remaining width */}
      <main className="flex-1 overflow-hidden flex flex-col min-w-0">
        {children}
      </main>

      {/* Mobile bottom nav */}
      <div className="md:hidden">
        <BottomNav />
      </div>
    </div>
  )
}
