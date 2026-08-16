import { useEffect, useRef, useState } from 'react'
import { NavLink, useLocation } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { MoreHorizontal } from 'lucide-react'
import { OVERFLOW_ITEMS, PRIMARY_ITEMS, navLabel, type NavItem } from '../lib/navItems'

/**
 * Mobile navigation: four primary tabs plus a "More" sheet.
 *
 * The item list is NOT defined here. It used to be, in a second array that had
 * drifted from the sidebar's — Observability and Drafts were on desktop only and
 * reachable on a phone solely by typing the URL. Placement now lives on the item
 * (src/lib/navItems.ts) and both navs read it.
 *
 * Why a sheet rather than a seventh tab: eight destinations across a ~360px
 * viewport is ~45px each, below the 44px touch target the rest of this bar keeps,
 * and it would need re-deciding every time a page is added. The sheet holds four
 * comfortably and scales.
 */
function OverflowSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const panelRef = useRef<HTMLDivElement>(null)

  // Escape and outside-tap close it. Without these the sheet can cover the bar
  // that opened it, which on a phone reads as the app being stuck.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            onClick={onClose}
            data-testid="nav-more-backdrop"
            className="fixed inset-0 z-[55] bg-black/50 backdrop-blur-sm"
          />
          <motion.div
            ref={panelRef}
            initial={{ y: '100%' }} animate={{ y: 0 }} exit={{ y: '100%' }}
            transition={{ type: 'spring', stiffness: 320, damping: 32 }}
            data-testid="nav-more-sheet"
            role="dialog"
            aria-label="More destinations"
            // z-[60]: ABOVE the nav bar, which is z-50 and rendered after this
            // in the DOM. At equal z-index the bar painted on top and swallowed
            // taps on the sheet's own links — the sheet looked fine and simply
            // did not respond, which no amount of reading the markup shows.
            className="fixed bottom-0 left-0 right-0 z-[60] rounded-t-2xl
                       border-t border-[#1E3A5F]/40 bg-[#232838]/95 backdrop-blur-xl
                       px-3 pb-[calc(env(safe-area-inset-bottom)+12px)] pt-3"
          >
            <div className="mx-auto mb-3 h-1 w-10 rounded-full bg-[#4A6080]/50" />
            <div className="grid grid-cols-2 gap-2">
              {OVERFLOW_ITEMS.map(({ to, icon: Icon, label }: NavItem) => (
                <NavLink
                  key={to}
                  to={to}
                  onClick={onClose}
                  data-nav={to}
                  className={({ isActive }) =>
                    `flex min-h-[52px] items-center gap-3 rounded-xl px-3 py-2.5
                     ${isActive ? 'bg-[#00D4FF]/10 text-[#00D4FF]'
                                : 'text-[#9AA7BD] hover:bg-white/[0.04]'}`}
                >
                  <Icon size={18} className="shrink-0" />
                  <span className="text-[13px] font-medium">{label}</span>
                </NavLink>
              ))}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  )
}

export default function BottomNav() {
  const [moreOpen, setMoreOpen] = useState(false)
  const { pathname } = useLocation()

  // Close on navigation, or the sheet stays over the page it just opened.
  useEffect(() => { setMoreOpen(false) }, [pathname])

  // "More" reads as active when the current route lives behind it — otherwise
  // the bar shows nothing selected and the user cannot tell where they are.
  const inOverflow = OVERFLOW_ITEMS.some(i => pathname === i.to
    || (i.to !== '/' && pathname.startsWith(i.to + '/')))

  return (
    <>
      <OverflowSheet open={moreOpen} onClose={() => setMoreOpen(false)} />
      <nav className="fixed bottom-0 left-0 right-0 z-50
                      bg-[#232838]/85 backdrop-blur-xl border-t border-[#1E3A5F]/40
                      flex justify-around items-center px-2 py-1.5 pb-safe">
        {PRIMARY_ITEMS.map((item: NavItem) => {
          const { to, icon: Icon } = item
          return (
            <NavLink key={to} to={to} end={to === '/'} className="flex-1" data-nav={to}>
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
                  <span className="text-[10px] font-medium relative z-10">
                    {navLabel(item, true)}
                  </span>
                </motion.div>
              )}
            </NavLink>
          )
        })}

        <button
          type="button"
          onClick={() => setMoreOpen(o => !o)}
          aria-expanded={moreOpen}
          aria-label="More destinations"
          data-testid="nav-more"
          className="flex-1"
        >
          <motion.div
            whileTap={{ scale: 0.92, y: 2 }}
            animate={{ scale: inOverflow || moreOpen ? 1.05 : 1 }}
            transition={{ type: 'spring', stiffness: 300, damping: 24 }}
            className={`relative flex flex-col items-center gap-0.5 px-3 py-2 rounded-xl
                        min-h-[44px] justify-center
                        ${inOverflow || moreOpen ? 'text-[#00D4FF]' : 'text-[#4A6080]'}`}
          >
            <MoreHorizontal size={20} className="relative z-10" />
            <span className="text-[10px] font-medium relative z-10">More</span>
          </motion.div>
        </button>
      </nav>
    </>
  )
}
