import { createClient } from '@supabase/supabase-js'

const SUPABASE_URL  = import.meta.env.VITE_SUPABASE_URL  as string
const SUPABASE_ANON = import.meta.env.VITE_SUPABASE_ANON_KEY as string

if (!SUPABASE_URL || !SUPABASE_ANON) {
  console.error('Missing VITE_SUPABASE_URL or VITE_SUPABASE_ANON_KEY in .env')
}

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON, {
  auth: {
    persistSession:        true,
    autoRefreshToken:      true,
    detectSessionInUrl:    true,      // handles OAuth redirect
    storageKey:            'aria-auth',
  },
})

export type SupabaseSession = Awaited<ReturnType<typeof supabase.auth.getSession>>['data']['session']
