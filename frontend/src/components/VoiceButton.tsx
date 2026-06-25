import { motion, AnimatePresence } from 'framer-motion'
import { Mic, Square } from 'lucide-react'
import { WaveformBar } from './WaveformBar'
import type { VoiceState } from '../hooks/useVoice'

interface VoiceButtonProps {
  voiceState: VoiceState
  analyserNode: AnalyserNode | null
  onStart: () => void
  onStop: () => void
  disabled?: boolean
}

export function VoiceButton({ voiceState, analyserNode, onStart, onStop, disabled }: VoiceButtonProps) {
  const isActive = voiceState === 'listening'
  const isProcessing = voiceState === 'processing'

  const handleClick = () => {
    if (isActive) onStop()
    else if (!isProcessing && !disabled) onStart()
  }

  return (
    <div className="relative flex items-center gap-2">
      <AnimatePresence>
        {isActive && (
          <motion.div
            key="waveform"
            initial={{ opacity: 0, width: 0 }}
            animate={{ opacity: 1, width: 'auto' }}
            exit={{ opacity: 0, width: 0 }}
            className="overflow-hidden"
          >
            <WaveformBar active analyserNode={analyserNode} height={24} />
          </motion.div>
        )}
      </AnimatePresence>

      <motion.button
        whileTap={{ scale: 0.9 }}
        onClick={handleClick}
        disabled={disabled || isProcessing}
        className="relative w-10 h-10 rounded-full flex items-center justify-center
                   transition-all duration-200 disabled:opacity-40"
        style={{
          background: isActive
            ? 'rgba(255,68,102,0.15)'
            : 'rgba(0,212,255,0.08)',
          border: isActive
            ? '1px solid rgba(255,68,102,0.4)'
            : '1px solid rgba(0,212,255,0.2)',
          color: isActive ? '#FF4466' : '#4A6080',
        }}
        title={isActive ? 'Stop recording' : 'Voice input'}
      >
        {isProcessing ? (
          <motion.div
            className="w-3.5 h-3.5 rounded-full border-2 border-[#00D4FF] border-t-transparent"
            animate={{ rotate: 360 }}
            transition={{ duration: 0.8, repeat: Infinity, ease: 'linear' }}
          />
        ) : isActive ? (
          <Square size={14} fill="currentColor" />
        ) : (
          <Mic size={16} />
        )}

        {/* Pulse ring when listening */}
        {isActive && (
          <motion.div
            className="absolute inset-0 rounded-full border border-[#FF4466]/40"
            animate={{ scale: [1, 1.5], opacity: [0.6, 0] }}
            transition={{ duration: 1.2, repeat: Infinity, ease: 'easeOut' }}
          />
        )}
      </motion.button>
    </div>
  )
}
