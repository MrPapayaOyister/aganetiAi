/**
 * OAuth callback landing page.
 * Supabase redirects here after Google/Microsoft OAuth.
 * The Supabase client detects the hash/code and exchanges it for a session.
 * We just show a spinner — AuthContext picks up the new session automatically.
 */
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { supabase } from '../lib/supabase'

export default function AuthCallbackPage() {
  const navigate = useNavigate()

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      // Session is now active (Supabase exchanged the code on init)
      if (data.session) {
        navigate('/', { replace: true })
      } else {
        navigate('/login', { replace: true })
      }
    })
  }, [navigate])

  return (
    <div className="min-h-screen bg-[#070B14] flex flex-col items-center justify-center gap-4">
      <div className="w-10 h-10 rounded-full border-2 border-[#00D4FF] border-t-transparent animate-spin" />
      <p className="text-[#4A6080] text-sm">Completing sign-in…</p>
    </div>
  )
}
