import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { Loader2 } from 'lucide-react'
import { useAuth } from '../contexts/AuthContext'
import { useToast } from '../hooks/useToast'
import ParticleCanvas from '../components/ParticleCanvas'
import { OrbAnimation } from '../components/OrbAnimation'

export default function LoginPage() {
  const { signInWithGoogle, session } = useAuth()
  const { addToast } = useToast()
  const navigate = useNavigate()
  const [booting, setBooting] = useState(true)
  const [authLoading, setAuthLoading] = useState(false)

  // "Awakening" — particles converge then settle as the page mounts
  useEffect(() => {
    const t = setTimeout(() => setBooting(false), 1600)
    return () => clearTimeout(t)
  }, [])

  // If already signed in, skip the login page
  useEffect(() => {
    if (session) navigate('/', { replace: true })
  }, [session, navigate])

  return (
    <div className="relative min-h-dvh w-full overflow-hidden bg-[#1E2230] flex items-center justify-center px-4 pt-safe">
      {/* Ambient field — converges (thinking) during boot, settles to idle */}
      <div className="absolute inset-0">
        <ParticleCanvas mode={booting ? 'thinking' : 'idle'} amplitude={0.4} focusY={0.38} />
      </div>

      {/* Vignette */}
      <div className="pointer-events-none absolute inset-0"
           style={{ background: 'radial-gradient(circle at 50% 38%, transparent 30%, rgba(7,11,20,0.65) 80%)' }} />

      <div className="relative z-10 w-full max-w-sm flex flex-col items-center">
        {/* Orb */}
        <motion.div
          initial={{ scale: 0.6, opacity: 0 }}
          animate={{ scale: 1, opacity: 1 }}
          transition={{ delay: 0.2, type: 'spring', stiffness: 120, damping: 16 }}
          className="mb-6"
        >
          <OrbAnimation mode={booting ? 'thinking' : 'idle'} size={150} />
        </motion.div>

        {/* Heading */}
        <motion.h1
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.5 }}
          className="text-3xl font-bold tracking-tight text-center"
        >
          Welcome to <span className="aurora-text">Aria</span>
        </motion.h1>
        <motion.p
          initial={{ opacity: 0, y: 12 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.62 }}
          className="text-[#94A3B8] text-sm mt-2 text-center"
        >
          Your intelligent enterprise assistant
        </motion.p>

        {/* Card */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.75 }}
          className="glass-strong w-full mt-10 p-6 flex flex-col gap-3"
          style={{ borderRadius: 20 }}
        >
          <button
            onClick={async () => {
              if (authLoading) return
              setAuthLoading(true)
              try { await signInWithGoogle() }
              catch { setAuthLoading(false) }
              // success → redirect unmounts the page; leave spinner running.
            }}
            disabled={authLoading}
            className="press w-full flex items-center justify-center gap-3 px-5 h-12 rounded-xl
                       bg-white text-[#1a1a1a] font-medium text-sm
                       hover:bg-white/90 disabled:opacity-70 disabled:cursor-wait"
          >
            {authLoading ? (
              <Loader2 size={18} className="animate-spin text-[#1a1a1a]" />
            ) : (
              <>
                <svg className="w-5 h-5 shrink-0" viewBox="0 0 24 24">
                  <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
                  <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
                  <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/>
                  <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
                </svg>
                Continue with Google
              </>
            )}
          </button>

          <button
            onClick={() => addToast('Microsoft login coming soon', 'info')}
            className="w-full flex items-center justify-center gap-3 px-5 h-12 rounded-xl
                       bg-white/[0.04] border border-white/[0.1] text-[#E2E8F0] font-medium text-sm
                       hover:bg-white/[0.08] active:scale-[0.98] transition-all duration-150"
          >
            <svg className="w-5 h-5 shrink-0" viewBox="0 0 24 24">
              <path fill="#F25022" d="M1 1h10v10H1z"/>
              <path fill="#7FBA00" d="M13 1h10v10H13z"/>
              <path fill="#00A4EF" d="M1 13h10v10H1z"/>
              <path fill="#FFB900" d="M13 13h10v10H13z"/>
            </svg>
            Continue with Microsoft 365
          </button>
        </motion.div>

        <motion.p
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 1 }}
          className="text-[11px] text-[#4A6080] mt-7 text-center leading-relaxed"
        >
          By continuing you agree to Aria's Terms of Service.
          <br />Your data stays on your infrastructure.
        </motion.p>
      </div>
    </div>
  )
}
