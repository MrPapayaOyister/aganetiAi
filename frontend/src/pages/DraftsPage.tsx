import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Send, Trash2, Mail } from 'lucide-react'
import { getDrafts, deleteDraft, sendEmail, type EmailDraft } from '../api/client'
import { PressButton } from '../components/ui/PressButton'
import { useAppContext } from '../App'
import { useToast } from '../hooks/useToast'
import { SkeletonCard } from '../components/SkeletonCard'

export default function DraftsPage() {
  const { userId } = useAppContext()
  const { addToast } = useToast()
  const qc = useQueryClient()
  const [edited, setEdited] = useState<Record<number, string>>({})
  const [busy, setBusy] = useState<number | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['drafts', userId],
    queryFn: () => getDrafts(userId).then(r => r.data.drafts),
    refetchInterval: 30_000,
  })
  const drafts: EmailDraft[] = data ?? []

  const removeMut = useMutation({
    mutationFn: (index: number) => deleteDraft(userId, index),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['drafts', userId] }),
  })

  const bodyOf = (d: EmailDraft) => edited[d._index] ?? d.draft_reply ?? d.body ?? ''
  const toOf = (d: EmailDraft) => d.to || (d.sender || '').match(/<(.+?)>/)?.[1] || d.sender || ''

  const send = async (d: EmailDraft) => {
    const to = toOf(d)
    if (!to) { addToast('No recipient on this draft', 'error'); return }
    setBusy(d._index)
    try {
      await sendEmail({ to_email: to, subject: d.subject || '(no subject)', body: bodyOf(d), user_id: userId })
      await deleteDraft(userId, d._index)
      addToast('Email sent', 'success')
      qc.invalidateQueries({ queryKey: ['drafts', userId] })
    } catch {
      addToast('Failed to send', 'error')
    } finally { setBusy(null) }
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-3xl mx-auto px-4 py-6 space-y-4 pb-24 md:pb-6">
        <motion.div initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }}>
          <h1 className="t-title">Email Drafts</h1>
          <p className="t-caption mt-0.5">Review, edit, and send the replies Aria prepared</p>
        </motion.div>

        {isLoading && <SkeletonCard lines={4} />}

        {!isLoading && drafts.length === 0 && (
          <div className="neu rounded-2xl p-10 text-center">
            <Mail size={28} className="mx-auto text-[#5C6B85] mb-3" />
            <p className="t-body text-[var(--text-secondary)]">No drafts awaiting approval</p>
          </div>
        )}

        <AnimatePresence>
          {drafts.map(d => (
            <motion.div
              key={d._index}
              layout
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.97 }}
              className="neu rounded-2xl p-4 space-y-3"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="t-heading truncate">{d.subject || '(no subject)'}</div>
                  <div className="t-caption truncate">To: {toOf(d) || '—'}</div>
                </div>
              </div>
              {d.triage_notes && (
                <div className="t-caption neu-inset rounded-lg px-3 py-2">{d.triage_notes}</div>
              )}
              <textarea
                value={bodyOf(d)}
                onChange={e => setEdited(prev => ({ ...prev, [d._index]: e.target.value }))}
                rows={5}
                className="w-full neu-inset rounded-xl px-3 py-2.5 t-body bg-transparent resize-y
                           outline-none focus:ring-1 focus:ring-[#38DBFF]/40"
              />
              <div className="flex items-center justify-end gap-2">
                <PressButton variant="danger" size="sm" onClick={() => removeMut.mutate(d._index)}>
                  <Trash2 size={14} /> Discard
                </PressButton>
                <PressButton variant="primary" size="sm"
                  loading={busy === d._index}
                  onClick={() => send(d)}>
                  <Send size={14} /> Send
                </PressButton>
              </div>
            </motion.div>
          ))}
        </AnimatePresence>
      </div>
    </div>
  )
}
