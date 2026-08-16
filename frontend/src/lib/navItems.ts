/**
 * The navigation, defined ONCE.
 *
 * There used to be two hand-maintained arrays — one in Sidebar.tsx, one in
 * BottomNav.tsx — and they had drifted. The sidebar carried 8 entries, the bottom
 * nav 6: Observability and Drafts existed on desktop and were reachable on a
 * phone only by typing the URL. Nothing was failing responsively; each nav
 * faithfully rendered its own list, and the lists disagreed. They had also drifted
 * on wording (`Assistant` vs `Chat` for the same route).
 *
 * So placement is a PROPERTY OF THE ITEM, declared here, and both navs read it:
 *
 *   primary      — a bottom-nav tab on mobile, and in the sidebar
 *   overflow     — behind the mobile "More" sheet, and in the sidebar
 *   desktopOnly  — sidebar only, and DELIBERATELY unreachable on mobile
 *
 * `desktopOnly` exists so that decision has to be typed out. Nothing uses it
 * today; it is here so a future page that genuinely cannot work on a phone is
 * marked as such rather than quietly missing, which is what happened before.
 *
 * tests/test_nav_coverage.py asserts every route in App.tsx appears here, so the
 * next page cannot land unreachable the way these two did.
 */
import {
  BarChart2, FolderOpen, Gauge, Inbox, LayoutDashboard, Mail, MessageSquare,
  Settings, type LucideIcon,
} from 'lucide-react'

export type NavPlacement = 'primary' | 'overflow' | 'desktopOnly'

export interface NavItem {
  to: string
  icon: LucideIcon
  /** Sidebar wording. */
  label: string
  /** Bottom-nav wording — a tab is ~64px wide, so "Assistant" does not fit.
   *  Absent means the label is short enough to reuse. */
  shortLabel?: string
  placement: NavPlacement
}

// Order is the render order in both navs. Primary items lead so the bottom bar
// keeps its most-used tabs leftmost without a second sort.
export const NAV_ITEMS: NavItem[] = [
  { to: '/',              icon: MessageSquare,   label: 'Assistant',     shortLabel: 'Chat', placement: 'primary' },
  { to: '/analytics',     icon: BarChart2,       label: 'Analytics',     placement: 'primary' },
  { to: '/dashboard',     icon: LayoutDashboard, label: 'Dashboard',     placement: 'primary' },
  { to: '/inbox',         icon: Inbox,           label: 'Inbox',         placement: 'primary' },
  { to: '/files',         icon: FolderOpen,      label: 'Files',         placement: 'overflow' },
  { to: '/drafts',        icon: Mail,            label: 'Drafts',        placement: 'overflow' },
  // Engineering/admin view. Deliberately a SEPARATE entry from Analytics: that
  // page stays the business dashboard, this one surfaces failures.
  { to: '/observability', icon: Gauge,           label: 'Observability', placement: 'overflow' },
  { to: '/settings',      icon: Settings,        label: 'Settings',      placement: 'overflow' },
]

/** Sidebar order — everything, desktop shows the lot. */
export const SIDEBAR_ITEMS = NAV_ITEMS

/** Bottom-nav tabs. "More" is appended by the component, not listed here. */
export const PRIMARY_ITEMS = NAV_ITEMS.filter(i => i.placement === 'primary')

/** Behind the "More" sheet. */
export const OVERFLOW_ITEMS = NAV_ITEMS.filter(i => i.placement === 'overflow')

export const navLabel = (i: NavItem, compact = false) =>
  compact ? (i.shortLabel ?? i.label) : i.label
