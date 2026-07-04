import { createClient, type SupabaseClient } from '@supabase/supabase-js'

const SUPABASE_URL  = import.meta.env.VITE_SUPABASE_URL  as string | undefined
const SUPABASE_ANON = import.meta.env.VITE_SUPABASE_ANON_KEY as string | undefined

/** Where OAuth redirects back to. Falls back to the current origin in dev. */
export const REDIRECT_URL =
  (import.meta.env.VITE_REDIRECT_URL as string | undefined) ||
  (typeof window !== 'undefined' ? window.location.origin : '')

/** True when real Supabase credentials are present in the build. */
export const supabaseConfigured = Boolean(SUPABASE_URL && SUPABASE_ANON)

/**
 * Safe no-op stand-in used ONLY when env vars are missing at build time.
 * Without this, createClient('', '') throws "supabaseUrl is required" at import,
 * which crashes the whole app to a blank screen. The stub lets the UI mount and
 * surface a clear "auth not configured" state instead.
 */
function makeStub(): SupabaseClient {
  const noSession = { data: { session: null }, error: null }
  const stub = {
    auth: {
      getSession: async () => noSession,
      getUser: async () => ({ data: { user: null }, error: null }),
      onAuthStateChange: (_cb: unknown) => ({
        data: { subscription: { unsubscribe() {} } },
      }),
      signInWithOAuth: async () => {
        // eslint-disable-next-line no-console
        console.error('[supabase] Not configured — set VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY at build time.')
        return { data: { provider: null, url: null }, error: new Error('Supabase not configured') }
      },
      signOut: async () => ({ error: null }),
    },
  }
  return stub as unknown as SupabaseClient
}

if (!supabaseConfigured) {
  // eslint-disable-next-line no-console
  console.error(
    '[supabase] Missing VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY. ' +
    'Auth is disabled. Add them to frontend/.env.production and rebuild.'
  )
}

export const supabase: SupabaseClient = supabaseConfigured
  ? createClient(SUPABASE_URL!, SUPABASE_ANON!, {
      auth: {
        persistSession:     true,
        autoRefreshToken:   true,
        // We parse the OAuth return ourselves in AuthContext (robust on plain
        // HTTP). Leaving this on lets supabase-js consume/strip the hash at
        // import time and race our handler.
        detectSessionInUrl: false,
        storageKey:         'aria-auth',
        // IMPORTANT: implicit (not pkce). The VM is served over plain HTTP, where
        // crypto.subtle is unavailable, so PKCE's code-exchange silently fails and
        // the session is never set. Implicit returns the token in the URL hash.
        flowType:           'implicit',
      },
    })
  : makeStub()

export type SupabaseSession =
  Awaited<ReturnType<typeof supabase.auth.getSession>>['data']['session']

/**
 * Bearer-auth header for the backend. The backend now enforces a valid Supabase
 * JWT on every route (Phase 0 security), so every API call must carry this.
 * Returns {} when logged out (the request will correctly get a 401).
 */
export async function authHeader(): Promise<Record<string, string>> {
  try {
    const { data } = await supabase.auth.getSession()
    const token = data.session?.access_token
    return token ? { Authorization: `Bearer ${token}` } : {}
  } catch {
    return {}
  }
}

/**
 * fetch() wrapper that always attaches the Supabase bearer token. Use this for
 * any raw fetch to the backend (streaming/SSE/binary) instead of window.fetch,
 * so every protected request is authenticated.
 */
export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const auth = await authHeader()
  return fetch(input, {
    ...init,
    headers: { ...(init.headers as Record<string, string> | undefined), ...auth },
  })
}
