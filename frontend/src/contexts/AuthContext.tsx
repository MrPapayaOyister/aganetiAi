import {
  createContext, useContext, useEffect, useState,
  useCallback, type ReactNode
} from 'react'
import type { Session } from '@supabase/supabase-js'
import { supabase } from '../lib/supabase'
import { http } from '../api/client'

// ── Types ─────────────────────────────────────────────────────

export interface ProviderInfo {
  provider:       'google' | 'microsoft'
  email:          string
  scopes:         string[]
}

export interface AriaUser {
  userId:         string
  email:          string
  displayName:    string
  avatarUrl?:     string
  plan:           'free' | 'pro' | 'team' | 'enterprise'
  features:       string[]
  providers:      ProviderInfo[]
  availableTools: string[]
}

interface AuthCtx {
  session:          Session | null
  user:             AriaUser | null
  loading:          boolean
  signInWithGoogle:    () => Promise<void>
  signInWithMicrosoft: () => Promise<void>
  signOut:          () => Promise<void>
  connectProvider:  (provider: 'google' | 'microsoft') => Promise<void>
  disconnectProvider: (provider: 'google' | 'microsoft') => Promise<void>
  refetchUser:      () => Promise<void>
}

const AuthContext = createContext<AuthCtx>(null!)

// ── Provider ──────────────────────────────────────────────────

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession]   = useState<Session | null>(null)
  const [user, setUser]         = useState<AriaUser | null>(null)
  const [loading, setLoading]   = useState(true)

  // Fetch /api/auth/me using the current Supabase JWT
  const fetchAriaUser = useCallback(async (jwt: string) => {
    try {
      const { data } = await http.get('/auth/me', {
        headers: { Authorization: `Bearer ${jwt}` },
      })
      setUser(data as AriaUser)
    } catch {
      setUser(null)
    }
  }, [])

  // On session change, sync to Aria backend
  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session)
      if (data.session?.access_token) {
        fetchAriaUser(data.session.access_token).finally(() => setLoading(false))
      } else {
        setLoading(false)
      }
    })

    const { data: { subscription } } = supabase.auth.onAuthStateChange(
      async (_event, newSession) => {
        setSession(newSession)
        if (newSession?.access_token) {
          await fetchAriaUser(newSession.access_token)
        } else {
          setUser(null)
        }
      }
    )

    return () => subscription.unsubscribe()
  }, [fetchAriaUser])

  // After OAuth redirect, extract provider token and POST to backend
  useEffect(() => {
    supabase.auth.getSession().then(async ({ data }) => {
      const s = data.session
      if (!s) return

      // Supabase gives us provider_token + provider_refresh_token in the session
      // after an OAuth signin. POST these to our backend to store them.
      const providerToken  = (s as any).provider_token as string | undefined
      const providerRefresh = (s as any).provider_refresh_token as string | undefined
      const identity = s.user.identities?.[0]

      if (providerToken && identity?.provider) {
        const provider = identity.provider as 'google' | 'microsoft'
        try {
          await http.post('/auth/provider/connect', {
            provider,
            provider_email:  identity.identity_data?.email ?? s.user.email,
            access_token:    providerToken,
            refresh_token:   providerRefresh ?? null,
            expires_in:      3600,
            scopes:          (identity.identity_data?.scope ?? '').split(' ').filter(Boolean),
          }, {
            headers: { Authorization: `Bearer ${s.access_token}` },
          })
        } catch (e) {
          console.warn('Failed to store provider token:', e)
        }
      }
    })
  }, [session?.user?.id])

  const signInWithGoogle = async () => {
    await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: {
        redirectTo: `${window.location.origin}/auth/callback`,
        scopes: [
          'https://www.googleapis.com/auth/gmail.readonly',
          'https://www.googleapis.com/auth/gmail.send',
          'https://www.googleapis.com/auth/calendar.readonly',
          'https://www.googleapis.com/auth/calendar.events',
        ].join(' '),
        queryParams: { access_type: 'offline', prompt: 'consent' },
      },
    })
  }

  const signInWithMicrosoft = async () => {
    await supabase.auth.signInWithOAuth({
      provider: 'azure',
      options: {
        redirectTo: `${window.location.origin}/auth/callback`,
        scopes: 'offline_access Mail.Read Mail.Send Calendars.ReadWrite Contacts.Read',
      },
    })
  }

  const signOut = async () => {
    await supabase.auth.signOut()
    setUser(null)
    setSession(null)
  }

  const connectProvider = async (provider: 'google' | 'microsoft') => {
    if (provider === 'google') await signInWithGoogle()
    else await signInWithMicrosoft()
  }

  const disconnectProvider = async (provider: 'google' | 'microsoft') => {
    if (!session) return
    await http.delete(`/auth/provider/${provider}`, {
      headers: { Authorization: `Bearer ${session.access_token}` },
    })
    await fetchAriaUser(session.access_token)
  }

  const refetchUser = async () => {
    if (session?.access_token) await fetchAriaUser(session.access_token)
  }

  return (
    <AuthContext.Provider value={{
      session, user, loading,
      signInWithGoogle, signInWithMicrosoft,
      signOut, connectProvider, disconnectProvider, refetchUser,
    }}>
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)
