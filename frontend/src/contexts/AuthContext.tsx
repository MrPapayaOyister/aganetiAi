import {
  createContext, useContext, useEffect, useState, type ReactNode,
} from 'react'
import type { Session, User } from '@supabase/supabase-js'
import { supabase, REDIRECT_URL } from '../lib/supabase'
import type { UserID } from '../api/client'

/**
 * Demo-grade auth.
 * Real Supabase session for login, but the backend still speaks user_1 / user_2,
 * so we map the authenticated email → a legacy UserID and persist it.
 */

function deriveUserId(email: string | undefined): UserID {
  if (!email) return 'user_1'
  // Map by convention: anything with "user1"/the primary owner → user_1, else user_2.
  return /user1|owner|admin/i.test(email) ? 'user_1' : 'user_2'
}

interface AuthCtx {
  session: Session | null
  user: User | null
  userId: UserID
  loading: boolean
  signInWithGoogle: () => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthCtx>(null!)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [user, setUser]       = useState<User | null>(null)
  const [userId, setUserId]   = useState<UserID>(
    () => (localStorage.getItem('aria_user_id') as UserID) || 'user_1'
  )
  const [loading, setLoading] = useState(true)

  function applySession(s: Session | null) {
    setSession(s)
    setUser(s?.user ?? null)
    if (s?.user) {
      const id = deriveUserId(s.user.email)
      setUserId(id)
      localStorage.setItem('aria_user_id', id)
    }
  }

  useEffect(() => {
    // Is this page load an OAuth return? (implicit flow puts the token in the hash)
    const hash = window.location.hash || ''
    const search = window.location.search || ''
    const oauthReturn = /access_token=|error_description=|error=/.test(hash) || /[?&]code=/.test(search)

    let settled = false
    const finish = (s: Session | null) => {
      applySession(s)
      if (!settled) { settled = true; setLoading(false) }
    }

    // Fires AFTER Supabase parses the URL hash → carries the real session.
    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      (_event, s) => finish(s)
    )

    // For a normal load, resolve immediately. During an OAuth return, do NOT
    // clear loading here — that would let ProtectedRoute navigate to /login and
    // strip the hash before Supabase reads it. Wait for onAuthStateChange.
    supabase.auth.getSession().then(({ data }) => {
      if (!oauthReturn) finish(data.session)
      else if (data.session) finish(data.session)
    })

    // Safety net: never hang on the spinner forever.
    const t = window.setTimeout(() => { if (!settled) { settled = true; setLoading(false) } }, 5000)

    return () => { subscription.unsubscribe(); window.clearTimeout(t) }
  }, [])

  const signInWithGoogle = async () => {
    await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: {
        redirectTo: REDIRECT_URL,
        queryParams: { access_type: 'offline', prompt: 'select_account' },
      },
    })
  }

  const signOut = async () => {
    await supabase.auth.signOut()
    localStorage.removeItem('aria_user_id')
    setSession(null)
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ session, user, userId, loading, signInWithGoogle, signOut }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)
