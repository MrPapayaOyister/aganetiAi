import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Inbox, Send, Check, X, Plus, ChevronDown, ChevronUp } from 'lucide-react'
import { resolveMessage, rejectMessage } from '../api/client'
import { useAppContext } from '../App'
import { useToast } from '../hooks/useToast'
import { SkeletonCard } from '../components/SkeletonCard'
import axios from 'axios'

const getAgentOutbox = (userId: string) => axios.get('/api/agent/outbox', { params: { user_id: userId } })
const sendAgentMessage = (data: object) => axios.post('/api/agent/message', data)

type Tab = 'incoming' | 'sent'
type MsgType = 'task_delegation' | 'message' | 'report' | string

const typeColors: Record<string, string> = {
  task_delegation: '#00D4FF',
  message: '#7B2FFF',
  report: '#00FF88',
}

interface ComposeModalProps {
  userId: string
  onClose: () => void
  onSent: () => void
}

function ComposeModal({ userId, onClose, onSent }: ComposeModalProps) {
  const [toUser, setToUser] = useState<'user_1' | 'user_2'>(userId === 'user_1' ? 'user_2' : 'user_1')
  const [type, setType] = useState<MsgType>('message')
  const [payload, setPayload] = useState('')
  const { addToast } = useToast()

  const handleSend = async () => {
    if (!payload.trim()) return
    try {
      await sendAgentMessage({
        from_user_id: userId,
        to_user_id: toUser,
        type,
        payload: { content: payload },
      })
      addToast('Message sent', 'success')
      onSent()
    } catch {
      addToast('Failed to send', 'error')
    }
  }

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      className="fixed inset-0 z-50 bg-black/60 backdrop-blur-sm flex items-end md:items-center justify-center p-4"
      onClick={onClose}
    >
      <motion.div
        initial={{ y: 60 }}
        animate={{ y: 0 }}
        exit={{ y: 60 }}
        onClick={e => e.stopPropagation()}
        className="glass rounded-2xl w-full max-w-md p-5 space-y-4"
      >
        <div className="flex items-center justify-between">
          <h3 className="font-semibold text-[#E2E8F0]">New Message</h3>
          <button onClick={onClose} className="text-[#4A6080] hover:text-[#E2E8F0] transition-colors">
            <X size={16} />
          </button>
        </div>

        <div className="space-y-3">
          <div>
            <label className="text-xs text-[#4A6080] mb-1 block">To Agent</label>
            <select
              value={toUser}
              onChange={e => setToUser(e.target.value as any)}
              className="w-full glass-sm rounded-xl px-3 py-2 text-sm text-[#E2E8F0] bg-transparent outline-none"
            >
              <option value="user_1">Agent 1 (user_1)</option>
              <option value="user_2">Agent 2 (user_2)</option>
            </select>
          </div>

          <div>
            <label className="text-xs text-[#4A6080] mb-1 block">Type</label>
            <select
              value={type}
              onChange={e => setType(e.target.value)}
              className="w-full glass-sm rounded-xl px-3 py-2 text-sm text-[#E2E8F0] bg-transparent outline-none"
            >
              <option value="message">Message</option>
              <option value="task_delegation">Task Delegation</option>
              <option value="report">Report</option>
            </select>
          </div>

          <div>
            <label className="text-xs text-[#4A6080] mb-1 block">Payload</label>
            <textarea
              value={payload}
              onChange={e => setPayload(e.target.value)}
              rows={4}
              placeholder="Message content…"
              className="w-full glass-sm rounded-xl px-3 py-2 text-sm text-[#E2E8F0]
                         bg-transparent outline-none resize-none placeholder:text-[#4A6080]"
            />
          </div>
        </div>

        <div className="flex gap-2 justify-end">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm text-[#4A6080] hover:text-[#E2E8F0] transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleSend}
            disabled={!payload.trim()}
            className="px-5 py-2 rounded-xl text-sm font-medium bg-[#00D4FF]/15
                       border border-[#00D4FF]/30 text-[#00D4FF] hover:bg-[#00D4FF]/25
                       disabled:opacity-30 transition-colors"
          >
            Send
          </button>
        </div>
      </motion.div>
    </motion.div>
  )
}

export default function InboxPage() {
  const { userId } = useAppContext()
  const { addToast } = useToast()
  const qc = useQueryClient()
  const [tab, setTab] = useState<Tab>('incoming')
  const [composing, setComposing] = useState(false)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const { data: inboxData, isLoading: inboxLoading } = useQuery({
    queryKey: ['agent-inbox', userId],
    queryFn: () => axios.get('/api/agent/inbox', { params: { user_id: userId } }).then(r => r.data),
    refetchInterval: 30_000,
  })

  const { data: outboxData, isLoading: outboxLoading } = useQuery({
    queryKey: ['agent-outbox', userId],
    queryFn: () => getAgentOutbox(userId).then(r => r.data),
    refetchInterval: 30_000,
  })

  const resolveMut = useMutation({
    mutationFn: (id: string) => resolveMessage(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['agent-inbox', userId] }); addToast('Message accepted', 'success') },
    onError: () => addToast('Failed to accept', 'error'),
  })

  const rejectMut = useMutation({
    mutationFn: (id: string) => rejectMessage(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['agent-inbox', userId] }); addToast('Message rejected', 'info') },
    onError: () => addToast('Failed to reject', 'error'),
  })

  const messages = inboxData?.messages ?? []
  const sent = outboxData?.messages ?? []
  const unread = messages.filter((m: any) => m.status === 'pending').length

  const statusColor = (s: string) => ({
    pending: '#FFB800',
    accepted: '#00FF88',
    rejected: '#FF4466',
  }[s] ?? '#4A6080')

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-2xl mx-auto px-4 py-6 space-y-5 pb-24 md:pb-6">
        {/* Header */}
        <motion.div
          initial={{ opacity: 0, y: -8 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex items-center justify-between"
        >
          <div>
            <div className="flex items-center gap-2">
              <Inbox size={18} className="text-[#00D4FF]" />
              <h1 className="text-lg font-semibold text-[#E2E8F0]">Agent Inbox</h1>
              {unread > 0 && (
                <span className="px-1.5 py-0.5 rounded-full text-[10px] font-bold
                                 bg-[#00D4FF]/20 text-[#00D4FF] border border-[#00D4FF]/30">
                  {unread}
                </span>
              )}
            </div>
            <p className="text-xs text-[#4A6080] mt-0.5">Inter-agent messaging mesh</p>
          </div>
          <motion.button
            whileTap={{ scale: 0.92 }}
            onClick={() => setComposing(true)}
            className="flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-medium
                       bg-[#00D4FF]/12 border border-[#00D4FF]/25 text-[#00D4FF]
                       hover:bg-[#00D4FF]/20 transition-colors"
          >
            <Plus size={14} />
            Compose
          </motion.button>
        </motion.div>

        {/* Tabs */}
        <div className="flex gap-1 glass-sm rounded-xl p-1">
          {(['incoming', 'sent'] as Tab[]).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`flex-1 py-2 rounded-lg text-sm font-medium transition-all
                          ${tab === t
                            ? 'bg-[#00D4FF]/15 text-[#00D4FF] border border-[#00D4FF]/25'
                            : 'text-[#4A6080] hover:text-[#94A3B8]'}`}
            >
              {t === 'incoming' ? 'Incoming' : 'Sent'}
            </button>
          ))}
        </div>

        {/* Messages */}
        <AnimatePresence mode="wait">
          {tab === 'incoming' ? (
            <motion.div key="incoming" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="space-y-3">
              {inboxLoading && [1,2,3].map(i => <SkeletonCard key={i} lines={3} />)}

              {!inboxLoading && messages.length === 0 && (
                <div className="glass rounded-2xl p-8 text-center">
                  <Inbox size={28} className="mx-auto text-[#1E3A5F] mb-2" />
                  <p className="text-[#4A6080] text-sm">No incoming messages</p>
                </div>
              )}

              {messages.map((msg: any) => {
                const isExpanded = expandedId === msg.id
                const typeColor = typeColors[msg.type] ?? '#4A6080'
                const isPending = msg.status === 'pending'

                return (
                  <motion.div
                    key={msg.id}
                    layout
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.96 }}
                    className={`glass rounded-2xl overflow-hidden transition-all
                                ${isPending ? '' : 'opacity-60'}`}
                  >
                    <div className="p-4">
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 mb-1">
                            <span
                              className="text-[10px] font-bold px-1.5 py-0.5 rounded"
                              style={{ color: typeColor, background: `${typeColor}15` }}
                            >
                              {msg.type?.replace('_', ' ').toUpperCase()}
                            </span>
                            <span className="text-xs text-[#4A6080]">from {msg.from_user_id ?? 'unknown'}</span>
                          </div>
                          <p className="text-sm text-[#E2E8F0] leading-snug truncate">
                            {typeof msg.payload === 'string'
                              ? msg.payload
                              : msg.payload?.content ?? JSON.stringify(msg.payload).substring(0, 80)}
                          </p>
                        </div>

                        <button
                          onClick={() => setExpandedId(isExpanded ? null : msg.id)}
                          className="shrink-0 text-[#4A6080]"
                        >
                          {isExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
                        </button>
                      </div>

                      {/* Expanded payload */}
                      <AnimatePresence>
                        {isExpanded && (
                          <motion.div
                            initial={{ height: 0, opacity: 0 }}
                            animate={{ height: 'auto', opacity: 1 }}
                            exit={{ height: 0, opacity: 0 }}
                            className="mt-3 pt-3 border-t border-[#1E3A5F]/30 overflow-hidden"
                          >
                            <pre className="text-xs text-[#94A3B8] whitespace-pre-wrap break-words">
                              {typeof msg.payload === 'object'
                                ? JSON.stringify(msg.payload, null, 2)
                                : msg.payload}
                            </pre>
                          </motion.div>
                        )}
                      </AnimatePresence>

                      {/* Actions */}
                      {isPending && (
                        <div className="flex gap-2 mt-3">
                          <motion.button
                            whileTap={{ scale: 0.92 }}
                            onClick={() => resolveMut.mutate(msg.id)}
                            disabled={resolveMut.isPending}
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium
                                       bg-[#00FF88]/12 border border-[#00FF88]/25 text-[#00FF88]
                                       hover:bg-[#00FF88]/20 transition-colors disabled:opacity-40"
                          >
                            <Check size={12} />
                            Accept
                          </motion.button>
                          <motion.button
                            whileTap={{ scale: 0.92 }}
                            onClick={() => rejectMut.mutate(msg.id)}
                            disabled={rejectMut.isPending}
                            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium
                                       bg-[#FF4466]/12 border border-[#FF4466]/25 text-[#FF4466]
                                       hover:bg-[#FF4466]/20 transition-colors disabled:opacity-40"
                          >
                            <X size={12} />
                            Reject
                          </motion.button>
                        </div>
                      )}

                      {!isPending && (
                        <div className="mt-2">
                          <span
                            className="text-xs font-medium"
                            style={{ color: statusColor(msg.status) }}
                          >
                            {msg.status?.charAt(0).toUpperCase() + msg.status?.slice(1)}
                          </span>
                        </div>
                      )}
                    </div>
                  </motion.div>
                )
              })}
            </motion.div>
          ) : (
            <motion.div key="sent" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="space-y-3">
              {outboxLoading && [1,2].map(i => <SkeletonCard key={i} lines={2} />)}

              {!outboxLoading && sent.length === 0 && (
                <div className="glass rounded-2xl p-8 text-center">
                  <Send size={28} className="mx-auto text-[#1E3A5F] mb-2" />
                  <p className="text-[#4A6080] text-sm">No sent messages</p>
                </div>
              )}

              {sent.map((msg: any) => (
                <motion.div
                  key={msg.id}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="glass-sm rounded-xl p-4"
                >
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <span className="text-xs text-[#4A6080]">To: {msg.to_user_id}</span>
                    <span
                      className="text-[10px] font-bold"
                      style={{ color: statusColor(msg.status) }}
                    >
                      {msg.status?.toUpperCase()}
                    </span>
                  </div>
                  <p className="text-sm text-[#94A3B8] truncate">
                    {typeof msg.payload === 'string'
                      ? msg.payload
                      : msg.payload?.content ?? '–'}
                  </p>
                </motion.div>
              ))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <AnimatePresence>
        {composing && (
          <ComposeModal
            userId={userId}
            onClose={() => setComposing(false)}
            onSent={() => {
              setComposing(false)
              qc.invalidateQueries({ queryKey: ['agent-outbox', userId] })
            }}
          />
        )}
      </AnimatePresence>
    </div>
  )
}
