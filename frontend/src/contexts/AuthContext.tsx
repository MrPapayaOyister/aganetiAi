import {
  createContext, useContext, useEffect, useState, type ReactNode,
} from 'react'
import type { Session, User } from '@supabase/supabase-js'
import { supabase, REDIRECT_URL } from '../lib/supabase'
import { clearUserScopedState, resetIfUserChanged } from '../lib/sessionReset'

// ── LOCAL DEV ONLY: skip the Supabase login screen ──────────────────────────
// Supabase only redirects OAuth back to origins allow-listed in the project, so
// logging in against http://localhost is awkward while testing provider linking.
// With VITE_DEV_AUTH_BYPASS=true the app mounts straight into the shell using a
// synthetic session. The backend needs the matching DEV_AUTH_BYPASS=true (see
// scripts/dev_backend.sh) or every API call still 401s.
//
// This CANNOT reach production: the flag lives only in .env.development.local,
// which is gitignored and read exclusively by `vite` in dev mode — `npm run
// build` reads .env.production, so the constant folds to false in the bundle.
const DEV_AUTH_BYPASS = import.meta.env.VITE_DEV_AUTH_BYPASS === 'true'
// Must equal the backend's DEV_AUTH_USER, since that is the identity it enforces.
const DEV_USER_ID = (import.meta.env.VITE_DEV_AUTH_USER as string) || 'user_1'

/** Minimal stand-in that satisfies the `session` truthiness gate in ProtectedRoute. */
function devSession(): Session {
  const user = {
    id: DEV_USER_ID,
    email: 'dev@localhost',
    user_metadata: { full_name: 'Dev User' },
    app_metadata: {},
    aud: 'authenticated',
    created_at: new Date().toISOString(),
  } as unknown as User
  return { access_token: '', refresh_token: '', expires_in: 0,
           token_type: 'bearer', user } as unknown as Session
}

interface AuthCtx {
  session: Session | null
  user: User | null
  userId: string   // real Supabase auth.users.id (UUID)
  loading: boolean
  signInWithGoogle: () => Promise<void>
  signInWithMicrosoft: () => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthCtx>(null!)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [user, setUser]       = useState<User | null>(null)
  const [userId, setUserId]   = useState<string>(
    () => localStorage.getItem('aria_user_id') ?? ''
  )
  const [loading, setLoading] = useState(true)

  function applySession(s: Session | null) {
    setSession(s)
    setUser(s?.user ?? null)
    if (s?.user) {
      // Use the real Supabase UUID — never derive from email.
      const id = s.user.id
      // A DIFFERENT user signing in on this browser must not inherit the previous
      // one's cached preferences, conversations or message bodies. Same user on a
      // refresh keeps their cache — see resetIfUserChanged.
      if (resetIfUserChanged(id)) {
        // eslint-disable-next-line no-console
        console.info('[auth] different user signed in — cleared cached state')
      }
      setUserId(id)
      localStorage.setItem('aria_user_id', id)
    }
  }

  useEffect(() => {
    if (DEV_AUTH_BYPASS) {
      // eslint-disable-next-line no-console
      console.warn('[auth] DEV_AUTH_BYPASS is on — running without a real login')
      applySession(devSession())
      setLoading(false)
      return
    }

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

  /**
   * Sign in with a Microsoft work/school account (Supabase provider 'azure').
   *
   * This establishes IDENTITY only — who you are. It does NOT grant Aria access
   * to your mailbox; that is a separate consent under Settings → Connected Apps
   * (/auth/microsoft/connect), exactly as with Google. Keeping them apart means
   * signing in never silently hands over Mail.Read, and a mailbox can be
   * disconnected without logging out.
   */
  const signInWithMicrosoft = async () => {
    await supabase.auth.signInWithOAuth({
      provider: 'azure',
      options: {
        redirectTo: REDIRECT_URL,
        // Identity scopes only — Mail/Calendar scopes belong to the connect flow.
        // Supabase adds `openid` itself, so listing it here just duplicates it.
        scopes: 'email profile',
      },
    })
  }

  const signOut = async () => {
    await supabase.auth.signOut()
    // Everything user-scoped, not just the id: leaving the caches behind is what
    // let the next person to use this browser see the previous one's data before
    // the first server response landed.
    clearUserScopedState()
    setSession(null)
    setUser(null)
    setUserId('')
  }

  return (
    <AuthContext.Provider value={{ session, user, userId, loading, signInWithGoogle, signInWithMicrosoft, signOut }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)
