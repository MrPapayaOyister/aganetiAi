import { createContext, useContext, useState } from 'react'
import { Routes, Route } from 'react-router-dom'
import { AnimatePresence } from 'framer-motion'
import Layout from './components/Layout'
import { Toast } from './components/Toast'
import AssistantPage from './pages/AssistantPage'
import AnalyticsPage from './pages/AnalyticsPage'
import FilesPage from './pages/FilesPage'
import InboxPage from './pages/InboxPage'
import SettingsPage from './pages/SettingsPage'
import { ToastContext, useToastState } from './hooks/useToast'
import type { UserID } from './api/client'

// ── App-level context ────────────────────────────────
interface AppCtxType {
  userId: UserID
  setUserId: (id: UserID) => void
  ttsEnabled: boolean
  setTtsEnabled: (v: boolean) => void
}

const AppCtx = createContext<AppCtxType>({
  userId: 'user_1',
  setUserId: () => {},
  ttsEnabled: false,
  setTtsEnabled: () => {}
})

export function useAppContext() {
  return useContext(AppCtx)
}

export default function App() {
  const [userId, setUserIdState] = useState<UserID>(() => {
    const stored = localStorage.getItem('aria_user_id')
    return (stored === 'user_1' || stored === 'user_2') ? stored : 'user_1'
  })
  const [ttsEnabled, setTtsEnabledState] = useState<boolean>(() => {
    return localStorage.getItem('aria_tts') === 'true'
  })

  const setUserId = (id: UserID) => {
    setUserIdState(id)
    localStorage.setItem('aria_user_id', id)
  }

  const setTtsEnabled = (v: boolean) => {
    setTtsEnabledState(v)
    localStorage.setItem('aria_tts', String(v))
  }

  const toastCtx = useToastState()

  return (
    <AppCtx.Provider value={{ userId, setUserId, ttsEnabled, setTtsEnabled }}>
      <ToastContext.Provider value={toastCtx}>
        <Layout>
          <Routes>
            <Route path="/" element={<AssistantPage />} />
            <Route path="/analytics" element={<AnalyticsPage />} />
            <Route path="/files" element={<FilesPage />} />
            <Route path="/inbox" element={<InboxPage />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </Layout>
        <AnimatePresence>
          {toastCtx.toasts.map(t => (
            <Toast key={t.id} toast={t} onDismiss={toastCtx.removeToast} />
          ))}
        </AnimatePresence>
      </ToastContext.Provider>
    </AppCtx.Provider>
  )
}
