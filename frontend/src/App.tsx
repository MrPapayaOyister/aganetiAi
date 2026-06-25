import { createContext, useContext, useState } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { AnimatePresence } from 'framer-motion'
import Layout from './components/Layout'
import { Toast } from './components/Toast'
import AssistantPage from './pages/AssistantPage'
import AnalyticsPage from './pages/AnalyticsPage'
import FilesPage from './pages/FilesPage'
import InboxPage from './pages/InboxPage'
import SettingsPage from './pages/SettingsPage'
import LoginPage from './pages/LoginPage'
import AuthCallbackPage from './pages/AuthCallbackPage'
import ProtectedRoute from './components/ProtectedRoute'
import { ToastContext, useToastState } from './hooks/useToast'
import { AuthProvider, useAuth } from './contexts/AuthContext'
import type { UserID } from './api/client'

// ── Legacy app context (TTS preference, kept for compat) ──────
// userId is now derived from Supabase session — see useAuth()
interface AppCtxType {
  userId: UserID                  // will be replaced by auth.user.userId post-migration
  setUserId: (id: UserID) => void // kept temporarily for Sidebar switcher
  ttsEnabled: boolean
  setTtsEnabled: (v: boolean) => void
}

const AppCtx = createContext<AppCtxType>({
  userId:       'user_1',
  setUserId:    () => {},
  ttsEnabled:   false,
  setTtsEnabled: () => {},
})

export function useAppContext() { return useContext(AppCtx) }

// ── Authenticated shell ────────────────────────────────────────
// Reads the real user ID from Supabase session and injects it
// into the legacy AppCtx so existing components keep working
// during the migration period.
function AuthenticatedShell() {
  const { user } = useAuth()
  const [ttsEnabled, setTtsEnabledState] = useState<boolean>(() =>
    localStorage.getItem('aria_tts') === 'true'
  )

  // During migration: map real user UUID to legacy user_1/user_2 if needed,
  // or just pass the UUID — routes that already accept user_id as a string will work.
  const userId = (user?.userId ?? localStorage.getItem('aria_user_id') ?? 'user_1') as UserID
  const setUserId = (id: UserID) => localStorage.setItem('aria_user_id', id)
  const setTtsEnabled = (v: boolean) => {
    setTtsEnabledState(v)
    localStorage.setItem('aria_tts', String(v))
  }

  return (
    <AppCtx.Provider value={{ userId, setUserId, ttsEnabled, setTtsEnabled }}>
      <Layout>
        <Routes>
          <Route path="/" element={<AssistantPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
          <Route path="/files" element={<FilesPage />} />
          <Route path="/inbox" element={<InboxPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Layout>
    </AppCtx.Provider>
  )
}

// ── Root ──────────────────────────────────────────────────────

export default function App() {
  const toastCtx = useToastState()

  return (
    <AuthProvider>
      <ToastContext.Provider value={toastCtx}>
        <Routes>
          {/* Public routes */}
          <Route path="/login"         element={<LoginPage />} />
          <Route path="/auth/callback" element={<AuthCallbackPage />} />

          {/* Protected shell — all app routes */}
          <Route
            path="/*"
            element={
              <ProtectedRoute>
                <AuthenticatedShell />
              </ProtectedRoute>
            }
          />
        </Routes>

        {/* Toasts rendered outside layout */}
        <AnimatePresence>
          {toastCtx.toasts.map(t => (
            <Toast key={t.id} toast={t} onDismiss={toastCtx.removeToast} />
          ))}
        </AnimatePresence>
      </ToastContext.Provider>
    </AuthProvider>
  )
}
