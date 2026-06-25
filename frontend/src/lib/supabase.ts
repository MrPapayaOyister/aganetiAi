import { createClient } from '@supabase/supabase-js'

const SUPABASE_URL  = import.meta.env.VITE_SUPABASE_URL  as string
const SUPABASE_ANON = import.meta.env.VITE_SUPABASE_ANON_KEY as string

/** Where OAuth redirects back to. Falls back to the current origin in dev. */
export const REDIRECT_URL =
  (import.meta.env.VITE_REDIRECT_URL as string | undefined) ||
  (typeof window !== 'undefined' ? window.location.origin : '')

if (!SUPABASE_URL || !SUPABASE_ANON) {
  // eslint-disable-next-line no-console
  console.error('Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY')
}

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON, {
  auth: {
    persistSession:     true,
    autoRefreshToken:   true,
    detectSessionInUrl: true,   // handles the OAuth redirect hash automatically
    storageKey:         'aria-auth',
    flowType:           'pkce',
  },
})

export type SupabaseSession =
  Awaited<ReturnType<typeof supabase.auth.getSession>>['data']['session']
