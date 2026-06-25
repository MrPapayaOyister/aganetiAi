import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ChevronUp, RefreshCw, Mail } from 'lucide-react'
import ReactMarkdown from 'react-markdown'
import { getEmailDigest } from '../api/client'
import type { UserID } from '../api/client'
import { SkeletonCard } from './SkeletonCard'

interface EmailDigestPanelProps {
  userId: UserID
}

export function EmailDigestPanel({ userId }: EmailDigestPanelProps) {
  const [expanded, setExpanded] = useState(true)

  const { data, isLoading, isError, refetch, isFetching } = useQuery({
    queryKey: ['digest', userId],
    queryFn: () => getEmailDigest(userId).then(r => r.data),
    staleTime: 600_000,
    retry: false,
  })

  return (
    <div className="glass rounded-2xl overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setExpanded(e => !e)}
        className="w-full flex items-center justify-between px-4 py-3 hover:bg-white/5 transition-colors"
      >
        <div className="flex items-center gap-2">
          <Mail size={15} className="text-[#00D4FF]" />
          <span className="text-sm font-semibold text-[#E2E8F0]">Email Digest</span>
        </div>
        <div className="flex items-center gap-2">
          <motion.button
            whileTap={{ scale: 0.85 }}
            onClick={e => { e.stopPropagation(); refetch() }}
            className="p-1.5 rounded-lg hover:bg-white/10 text-[#4A6080] hover:text-[#00D4FF] transition-colors"
            title="Refresh digest"
          >
            <RefreshCw size={13} className={isFetching ? 'animate-spin' : ''} />
          </motion.button>
          {expanded ? <ChevronUp size={15} className="text-[#4A6080]" /> : <ChevronDown size={15} className="text-[#4A6080]" />}
        </div>
      </button>

      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="digest-body"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: 'easeInOut' }}
            className="overflow-hidden"
          >
            <div className="px-4 pb-4 border-t border-[#1E3A5F]/30">
              {isLoading && <SkeletonCard lines={5} className="mt-3 !border-0 !bg-transparent" />}

              {isError && (
                <div className="mt-3 text-center py-4">
                  <p className="text-[#4A6080] text-sm">Could not load digest</p>
                  <button
                    onClick={() => refetch()}
                    className="mt-2 text-xs text-[#00D4FF] hover:underline"
                  >
                    Retry
                  </button>
                </div>
              )}

              {data?.digest && (
                <div className="mt-3 prose-chat text-sm leading-relaxed text-[#94A3B8] max-h-64 overflow-y-auto">
                  <ReactMarkdown>{data.digest}</ReactMarkdown>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
