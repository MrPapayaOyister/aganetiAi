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
    let settled = false
    const finish = (s: Session | null) => {
      applySession(s)
      if (!settled) { settled = true; setLoading(false) }
    }

    // Keep listening for later changes (sign-out, refresh).
    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      (event, s) => {
        if (event === 'SIGNED_OUT') { finish(null); return }
        if (s) finish(s)
      }
    )

    // ── Explicit OAuth-return handling (robust on plain HTTP) ──
    // We do NOT rely on detectSessionInUrl: on the HTTP origin it silently
    // no-ops. Parse the implicit-flow token out of the URL hash ourselves and
    // set the session directly. setSession() needs no Web Crypto.
    async function init() {
      const hash = window.location.hash.startsWith('#')
        ? window.location.hash.slice(1)
        : window.location.hash
      const hp = new URLSearchParams(hash)
      const sp = new URLSearchParams(window.location.search)

      const access_token  = hp.get('access_token')
      const refresh_token = hp.get('refresh_token')
      const errDesc = hp.get('error_description') || sp.get('error_description')
      const code = sp.get('code')

      // eslint-disable-next-line no-console
      console.info('[auth] init', {
        hasHashToken: !!access_token, hasRefresh: !!refresh_token,
        hasCode: !!code, err: errDesc || null,
      })

      if (errDesc) {
        // eslint-disable-next-line no-console
        console.error('[auth] provider returned error:', errDesc)
      }

      if (access_token && refresh_token) {
        const { data, error } = await supabase.auth.setSession({ access_token, refresh_token })
        if (error) {
          // eslint-disable-next-line no-console
          console.error('[auth] setSession failed:', error.message)
        } else {
          // eslint-disable-next-line no-console
          console.info('[auth] session established via hash for', data.session?.user?.email)
        }
        // Clean the token out of the URL bar.
        window.history.replaceState({}, document.title, window.location.pathname)
        finish(data?.session ?? null)
        return
      }

      if (code) {
        // PKCE/code return — needs a stored verifier (Web Crypto), unavailable on HTTP.
        try {
          const { data, error } = await supabase.auth.exchangeCodeForSession(code)
          if (error) throw error
          window.history.replaceState({}, document.title, window.location.pathname)
          finish(data.session)
          return
        } catch (e) {
          // eslint-disable-next-line no-console
          console.error('[auth] code exchange failed (HTTP/PKCE limitation):', e)
        }
      }

      // Normal load — existing persisted session, if any.
      const { data } = await supabase.auth.getSession()
      finish(data.session)
    }
    init()

    // Safety net: never hang on the spinner forever.
    const t = window.setTimeout(() => { if (!settled) { settled = true; setLoading(false) } }, 6000)

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
