import { memo, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import {
  AlertTriangle, Check, ChevronDown, CircleDashed, FileText, Loader2,
  MinusCircle, Network, ShieldCheck, ShieldX, X,
} from 'lucide-react'
import {
  isSubstantiated, type AgentProcessData, type AgentStage, type Verdict,
} from '../lib/agentProcess'

/**
 * "Agent process" — what the agent planned, found and concluded.
 *
 * Collapsed by default. The answer is the product; this is the working, and a
 * panel that opens itself on every turn would bury the thing the user asked for.
 *
 * It renders ONLY the public artifact contract (see lib/agentProcess): five
 * stages, a plan, cited evidence and a verdict. No scores, no provider names, no
 * tenant, no store names — those never leave the server, and the reducer drops
 * them a second time even if they did.
 *
 * NOT chain-of-thought. Everything here is structured execution metadata the
 * backend already decided to publish; the model's reasoning is neither streamed
 * nor rendered.
 */

const STATUS_ICON: Record<AgentStage['status'], React.ReactNode> = {
  pending: <CircleDashed size={13} className="text-[#4A6080]" />,
  running: <Loader2 size={13} className="animate-spin text-[#00D4FF]" />,
  done: <Check size={13} className="text-[#00D4FF]" />,
  skipped: <MinusCircle size={13} className="text-[#4A6080]" />,
  error: <X size={13} className="text-[#FF6B6B]" />,
}

/** Verdict → how it LOOKS. The three states must be visually distinct: the
 *  failure this guards against is a refusal wearing a success badge. */
const VERDICT_STYLE: Record<Verdict, { ring: string; text: string; icon: React.ReactNode }> = {
  SUPPORTED: {
    ring: 'border-[#00D4FF]/40 bg-[#00D4FF]/10', text: 'text-[#00D4FF]',
    icon: <ShieldCheck size={13} />,
  },
  PARTIALLY_SUPPORTED: {
    ring: 'border-[#F5A623]/40 bg-[#F5A623]/10', text: 'text-[#F5A623]',
    icon: <AlertTriangle size={13} />,
  },
  UNSUPPORTED: {
    ring: 'border-[#FF6B6B]/40 bg-[#FF6B6B]/10', text: 'text-[#FF6B6B]',
    icon: <ShieldX size={13} />,
  },
  CONFLICTING: {
    ring: 'border-[#F5A623]/40 bg-[#F5A623]/10', text: 'text-[#F5A623]',
    icon: <AlertTriangle size={13} />,
  },
}

function Section({ title, count, children, testId }: {
  title: string; count?: number; children: React.ReactNode; testId: string
}) {
  const [open, setOpen] = useState(false)
  return (
    <div className="rounded-lg border border-[#1E3A5F]/40 bg-[#1E2230]/40" data-testid={testId}>
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        data-testid={`${testId}-toggle`}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-[12px]
                   font-medium text-[#9AA7BD] hover:text-[#C9D4E3]"
      >
        <ChevronDown size={13}
          className={`shrink-0 transition-transform ${open ? '' : '-rotate-90'}`} />
        <span>{title}</span>
        {count !== undefined && (
          <span className="ml-auto text-[11px] text-[#4A6080]">{count}</span>
        )}
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.16 }}
            className="overflow-hidden"
          >
            <div className="space-y-2 px-3 pb-3 text-[12px] text-[#9AA7BD]">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function AgentProcessBase({ data }: { data: AgentProcessData }) {
  const [open, setOpen] = useState(false)
  const v = data.verification
  const substantiated = isSubstantiated(data)
  const style = v ? VERDICT_STYLE[v.verdict] : null

  return (
    <div className="mt-3 max-w-2xl" data-testid="agent-process">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        data-testid="agent-process-toggle"
        className="flex items-center gap-2 rounded-lg border border-[#1E3A5F]/40
                   bg-[#232838]/60 px-3 py-1.5 text-[11px] font-medium
                   text-[#9AA7BD] hover:text-[#C9D4E3]"
      >
        <Network size={13} className="text-[#00D4FF]" />
        <span>Agent process</span>
        {v && style && (
          // The verdict is visible WITHOUT expanding. Hiding it behind a click
          // is how an unsupported answer comes to look like a normal one.
          <span
            data-testid="agent-verdict-badge"
            data-verdict={v.verdict}
            data-substantiated={substantiated ? 'true' : 'false'}
            className={`ml-1 inline-flex items-center gap-1 rounded-md border px-1.5
                        py-0.5 text-[10px] ${style.ring} ${style.text}`}
          >
            {style.icon}{v.label}
          </span>
        )}
        <ChevronDown size={13}
          className={`transition-transform ${open ? '' : '-rotate-90'}`} />
      </button>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.18 }}
            className="overflow-hidden"
          >
            <div className="mt-2 space-y-2">
              {/* stages */}
              <div className="flex flex-wrap gap-x-4 gap-y-1 rounded-lg border
                              border-[#1E3A5F]/40 bg-[#1E2230]/40 px-3 py-2"
                   data-testid="agent-stages">
                {data.stages.map(s => (
                  <span key={s.key} data-testid={`agent-stage-${s.key}`}
                        data-status={s.status}
                        className={`inline-flex items-center gap-1.5 text-[11px] ${
                          s.status === 'pending' ? 'text-[#4A6080]' : 'text-[#9AA7BD]'}`}>
                    {STATUS_ICON[s.status]}{s.label}
                  </span>
                ))}
              </div>

              {data.plan && (
                <Section title="Plan" testId="agent-plan">
                  {data.plan.goal && (
                    <p className="text-[#C9D4E3]">{data.plan.goal}</p>
                  )}
                  <ol className="space-y-1">
                    {data.plan.steps.map((s, i) => (
                      <li key={s.id || i} className="flex gap-2">
                        <span className="text-[#4A6080]">{s.id || i + 1}.</span>
                        <span>
                          <span className="text-[#00D4FF]">{s.agent}</span>
                          {s.task ? ` — ${s.task}` : ''}
                        </span>
                      </li>
                    ))}
                  </ol>
                </Section>
              )}

              {data.evidence && (
                <Section title="Evidence" testId="agent-evidence"
                         count={data.evidence.counts.total ?? data.evidence.citations.length}>
                  {data.evidence.citations.length === 0 && (
                    <p className="italic text-[#4A6080]">No supporting evidence was found.</p>
                  )}
                  {data.evidence.citations.map((c, i) => (
                    <div key={i} className="rounded-md bg-[#1E2230]/60 p-2"
                         data-testid="agent-citation">
                      <div className="mb-0.5 flex items-center gap-1.5 text-[11px] text-[#4A6080]">
                        {c.where === 'knowledge graph'
                          ? <Network size={11} /> : <FileText size={11} />}
                        <span>{c.source || c.where}</span>
                      </div>
                      <p className="text-[#C9D4E3]">{c.text}</p>
                    </div>
                  ))}
                </Section>
              )}

              {v && (
                <Section title="Verification" testId="agent-verification">
                  <div className={`inline-flex items-center gap-1.5 rounded-md border
                                   px-2 py-1 ${style!.ring} ${style!.text}`}>
                    {style!.icon}<span className="font-medium">{v.label}</span>
                  </div>
                  {v.explanation && <p className="text-[#C9D4E3]">{v.explanation}</p>}
                  {v.missing.length > 0 && (
                    <div data-testid="agent-verification-missing">
                      <p className="text-[#4A6080]">Not established by the evidence:</p>
                      <ul className="list-disc pl-4">
                        {v.missing.map((m, i) => <li key={i}>{m}</li>)}
                      </ul>
                    </div>
                  )}
                  {v.conflicts.length > 0 && (
                    <div data-testid="agent-verification-conflicts">
                      <p className="text-[#4A6080]">Conflicting evidence:</p>
                      <ul className="list-disc pl-4">
                        {v.conflicts.map((c, i) => <li key={i}>{c}</li>)}
                      </ul>
                    </div>
                  )}
                </Section>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export const AgentProcess = memo(AgentProcessBase)
export default AgentProcess
