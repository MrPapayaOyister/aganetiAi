/**
 * Sub-agent registry — display metadata shared by the delegation cards and the
 * AgentNetworkRail. Mirrors backend.delegation.KNOWN_AGENTS.
 */
import {
  Calendar, Mail, Brain, Clock, CheckSquare, Sparkles,
  type LucideIcon,
} from 'lucide-react'

export interface AgentMeta {
  id: string
  label: string
  role: string
  color: string          // accent hex
  icon: LucideIcon
}

export const ARIA: AgentMeta = {
  id: 'aria', label: 'Aria', role: 'Orchestrator', color: '#00D4FF', icon: Sparkles,
}

export const AGENTS: AgentMeta[] = [
  { id: 'calendar_agent',  label: 'Calendar', role: 'Schedules & events',   color: '#00FFB3', icon: Calendar },
  { id: 'mail_agent',      label: 'Mail',     role: 'Inbox & drafts',       color: '#00D4FF', icon: Mail },
  { id: 'memory_agent',    label: 'Memory',   role: 'Recall & knowledge',   color: '#7B2FFF', icon: Brain },
  { id: 'scheduler_agent', label: 'Scheduler',role: 'Reminders & timing',   color: '#FFB800', icon: Clock },
  { id: 'task_agent',      label: 'Tasks',    role: 'To-dos & tracking',    color: '#FB7185', icon: CheckSquare },
]

// Backend uses email_agent; UI labels it Mail. Accept both ids.
const _byId: Record<string, AgentMeta> = { [ARIA.id]: ARIA }
for (const a of AGENTS) _byId[a.id] = a
_byId['email_agent'] = { ..._byId['mail_agent'], id: 'email_agent' }

export function agentMeta(id: string | undefined | null): AgentMeta {
  if (!id) return ARIA
  return _byId[id] ?? {
    id, label: id.replace(/_agent$/, '').replace(/_/g, ' '),
    role: 'Sub-agent', color: '#9AA7BD', icon: Sparkles,
  }
}
