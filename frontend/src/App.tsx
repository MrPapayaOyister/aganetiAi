import { createContext, useContext, useState, useEffect } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { initSound, initSoundDelegation } from './lib/sound'
import { AnimatePresence, motion } from 'framer-motion'
import Layout from './components/Layout'
import { Toast } from './components/Toast'
import CommandPalette from './components/CommandPalette'
import IntelligenceCore from './components/IntelligenceCore'
import AssistantPage from './pages/AssistantPage'
import AnalyticsPage from './pages/AnalyticsPage'
import FilesPage from './pages/FilesPage'
import InboxPage from './pages/InboxPage'
import DraftsPage from './pages/DraftsPage'
import SettingsPage from './pages/SettingsPage'
import LoginPage from './pages/LoginPage'
import ProtectedRoute from './components/ProtectedRoute'
import { ToastContext, useToastState } from './hooks/useToast'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import { AmbientProvider } from './contexts/AmbientContext'
import { AgentFieldProvider } from './contexts/AgentFieldContext'
import { PrefsProvider } from './contexts/PrefsContext'
import { OnboardingModal } from './components/OnboardingModal'

// ── App context (userId = real Supabase UUID + TTS pref) ────────
interface AppCtxType {
  userId: string
  setUserId: (id: string) => void
  ttsEnabled: boolean
  setTtsEnabled: (v: boolean) => void
}
const AppCtx = createContext<AppCtxType>({
  userId: '', setUserId: () => {}, ttsEnabled: true, setTtsEnabled: () => {},
})
export function useAppContext() { return useContext(AppCtx) }

// ── Page transition wrapper ─────────────────────────────────────
// Cinematic page transition: the page settles into focus (blur→sharp, lifts up)
// and defocuses on exit (sharpens away, drifts back). AnimatePresence mode="wait"
// gives the layered "shell first, content after" gap. Expo-out easing reads as
// premium and deliberate — slower than a daily-driver app, still clear.
const PAGE_EASE = [0.22, 1, 0.36, 1] as const
function Page({ children }: { children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 18, filter: 'blur(8px)' }}
      animate={{ opacity: 1, y: 0, filter: 'blur(0px)' }}
      exit={{ opacity: 0, y: -12, filter: 'blur(6px)' }}
      transition={{ duration: 0.52, ease: PAGE_EASE }}
      className="h-full"
    >
      {children}
    </motion.div>
  )
}

// ── Animated routes (needs router context for useLocation) ──────
function AnimatedRoutes() {
  const location = useLocation()
  return (
    <AnimatePresence mode="wait">
      <Routes location={location} key={location.pathname}>
        <Route path="/"          element={<Page><AssistantPage /></Page>} />
        <Route path="/analytics" element={<Page><AnalyticsPage /></Page>} />
        <Route path="/files"     element={<Page><FilesPage /></Page>} />
        <Route path="/inbox"     element={<Page><InboxPage /></Page>} />
        <Route path="/drafts"    element={<Page><DraftsPage /></Page>} />
        <Route path="/settings"  element={<Page><SettingsPage /></Page>} />
        <Route path="*"          element={<Navigate to="/" replace />} />
      </Routes>
    </AnimatePresence>
  )
}

// ── Authenticated shell ─────────────────────────────────────────
function AuthenticatedShell() {
  const { userId } = useAuth()
  const location = useLocation()
  const [ttsEnabled, setTtsEnabledState] = useState<boolean>(
    () => localStorage.getItem('aria_tts') !== 'false'   // default ON
  )

  // userId comes from auth — no switching, no mutation.
  const setUserId = (_: string) => {}
  const setTtsEnabled = (v: boolean) => {
    setTtsEnabledState(v)
    localStorage.setItem('aria_tts', String(v))
  }

  // Utility pages get a slightly quieter feel via opacity — but the
  // IntelligenceCore itself remains mounted (only ONE WebGL context for the
  // whole app lifetime → no flash on route change).
  const calmRoutes = ['/settings', '/files', '/analytics']
  const calm = calmRoutes.some(p => location.pathname.startsWith(p))

  return (
    <AppCtx.Provider value={{ userId, setUserId, ttsEnabled, setTtsEnabled }}>
      {/* Intelligence core — masked PixelBlast + radial bloom + vignette.
          Mounted ONCE; never remounts across routes. */}
      <div
        className="fixed inset-0 z-0 pointer-events-none"
        style={{ opacity: calm ? 0.42 : 0.70, transition: 'opacity 600ms ease' }}
      >
        <IntelligenceCore />
      </div>
      {/* Top scrim — fades core cleanly into the header */}
      <div
        className="fixed inset-x-0 top-0 h-24 z-[1] pointer-events-none"
        style={{ background: 'linear-gradient(to bottom, rgba(4,6,11,0.65), transparent)' }}
      />
      {/* Bottom scrim — protects readability above the input bar */}
      <div
        className="fixed inset-x-0 bottom-0 h-32 z-[1] pointer-events-none"
        style={{ background: 'linear-gradient(to top, rgba(4,6,11,0.72), transparent)' }}
      />
      <div className="relative z-10 h-full">
        <Layout>
          <AnimatedRoutes />
        </Layout>
      </div>
      <CommandPalette />
      <OnboardingModal />
    </AppCtx.Provider>
  )
}

// ── Root ────────────────────────────────────────────────────────
export default function App() {
  const toastCtx = useToastState()
  // Sound model (Item 8): unlock AudioContext on first gesture + wire
  // data-sound click delegation once for the whole app.
  useEffect(() => { initSound(); initSoundDelegation() }, [])
  return (
    <AuthProvider>
      <AmbientProvider>
        <AgentFieldProvider>
        <PrefsProvider>
        <ToastContext.Provider value={toastCtx}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              path="/*"
              element={
                <ProtectedRoute>
                  <AuthenticatedShell />
                </ProtectedRoute>
              }
            />
          </Routes>

          <AnimatePresence>
            {toastCtx.toasts.map(t => (
              <Toast key={t.id} toast={t} onDismiss={toastCtx.removeToast} />
            ))}
          </AnimatePresence>
        </ToastContext.Provider>
        </PrefsProvider>
        </AgentFieldProvider>
      </AmbientProvider>
    </AuthProvider>
  )
}
