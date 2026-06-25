import { createContext, useContext, useState } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import Layout from './components/Layout'
import { Toast } from './components/Toast'
import CommandPalette from './components/CommandPalette'
import ParticleCanvas from './components/ParticleCanvas'
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
import { AmbientProvider, useAmbient } from './contexts/AmbientContext'

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
function Page({ children }: { children: React.ReactNode }) {
  return (
    <motion.div
      initial={{ opacity: 0, x: 20 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -20 }}
      transition={{ duration: 0.25, ease: 'easeOut' }}
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
  const { mode, amplitude } = useAmbient()
  const [ttsEnabled, setTtsEnabledState] = useState<boolean>(
    () => localStorage.getItem('aria_tts') !== 'false'   // default ON
  )

  // userId comes from auth — no switching, no mutation.
  const setUserId = (_: string) => {}
  const setTtsEnabled = (v: boolean) => {
    setTtsEnabledState(v)
    localStorage.setItem('aria_tts', String(v))
  }

  return (
    <AppCtx.Provider value={{ userId, setUserId, ttsEnabled, setTtsEnabled }}>
      {/* Global ambient particle field — reacts to assistant state */}
      <div className="fixed inset-0 z-0 pointer-events-none">
        <ParticleCanvas mode={mode} amplitude={amplitude} />
      </div>
      <div className="relative z-10 h-full">
        <Layout>
          <AnimatedRoutes />
        </Layout>
      </div>
      <CommandPalette />
    </AppCtx.Provider>
  )
}

// ── Root ────────────────────────────────────────────────────────
export default function App() {
  const toastCtx = useToastState()
  return (
    <AuthProvider>
      <AmbientProvider>
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
      </AmbientProvider>
    </AuthProvider>
  )
}
