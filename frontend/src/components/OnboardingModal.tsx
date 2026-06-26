import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { Sparkles } from 'lucide-react'
import { usePrefs } from '../contexts/PrefsContext'

export function OnboardingModal() {
  const { prefs, setPrefs } = usePrefs()
  const [agentName, setAgentName] = useState('Aria')
  const [displayName, setDisplayName] = useState('')
  const [visible, setVisible] = useState(!prefs.onboardingDone)

  const dismiss = () => { setPrefs({ onboardingDone: true }); setVisible(false) }

  const submit = () => {
    setPrefs({
      agentName: agentName.trim() || 'Aria',
      displayName: displayName.trim(),
      onboardingDone: true,
    })
    setVisible(false)
  }

  return (
    <AnimatePresence>
      {visible && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          className="fixed inset-0 z-[200] flex items-center justify-center px-6"
          style={{ background: 'rgba(20,23,34,0.72)', backdropFilter: 'blur(4px)' }}
        >
          <motion.div
            initial={{ opacity: 0, y: 24, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 12, scale: 0.97 }}
            transition={{ type: 'spring', stiffness: 320, damping: 26 }}
            className="glass-strong rounded-2xl p-6 max-w-sm w-full"
          >
            <div className="flex items-center gap-3 mb-5">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-[#00D4FF] to-[#7B2FFF]
                              flex items-center justify-center shrink-0">
                <Sparkles size={18} className="text-white" />
              </div>
              <div>
                <h2 className="text-base font-semibold text-[#E2E8F0]">Set up your assistant</h2>
                <p className="text-xs text-[#4A6080]">Quick personalisation — takes 10 seconds</p>
              </div>
            </div>

            <div className="space-y-3">
              <div>
                <label className="text-xs font-medium text-[#9AA7BD] block mb-1.5">
                  What should your assistant be called?
                </label>
                <input
                  value={agentName}
                  onChange={e => setAgentName(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && submit()}
                  placeholder="Aria"
                  maxLength={30}
                  className="w-full neu-inset rounded-xl px-4 py-2.5 text-sm text-[#E2E8F0]
                             placeholder:text-[#4A6080] outline-none"
                />
              </div>
              <div>
                <label className="text-xs font-medium text-[#9AA7BD] block mb-1.5">
                  Your name <span className="text-[#4A6080] font-normal">(optional)</span>
                </label>
                <input
                  value={displayName}
                  onChange={e => setDisplayName(e.target.value)}
                  onKeyDown={e => e.key === 'Enter' && submit()}
                  placeholder="Your first name…"
                  maxLength={50}
                  className="w-full neu-inset rounded-xl px-4 py-2.5 text-sm text-[#E2E8F0]
                             placeholder:text-[#4A6080] outline-none"
                />
              </div>
            </div>

            <div className="flex items-center gap-3 mt-5">
              <button
                onClick={submit}
                disabled={!agentName.trim()}
                className="press flex-1 h-10 rounded-xl text-sm font-medium
                           bg-[#00D4FF]/15 border border-[#00D4FF]/30 text-[#00D4FF]
                           hover:bg-[#00D4FF]/25
                           disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Let's go
              </button>
              <button
                onClick={dismiss}
                className="px-4 h-10 rounded-xl text-sm text-[#5C6B85]
                           hover:text-[#9AA7BD] transition-colors"
              >
                Skip
              </button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
