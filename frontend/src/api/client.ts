import axios from 'axios'
import { authHeader } from '../lib/supabase'

export const http = axios.create({ baseURL: '/api' })

// Attach the Supabase bearer token to every backend call. The backend enforces
// auth on every route (Phase 0), so unauthenticated calls now correctly 401.
http.interceptors.request.use(async (config) => {
  const h = await authHeader()
  if (h.Authorization) {
    const headers = config.headers as any
    if (headers && typeof headers.set === 'function') headers.set('Authorization', h.Authorization)
    else config.headers = { ...(headers ?? {}), Authorization: h.Authorization } as any
  }
  return config
})

// ── Types ──────────────────────────────────────────────
// UserID is the real Supabase auth.users.id (UUID string).
export type UserID = string

export interface Task {
  id: string
  title: string
  status: 'pending' | 'done'
  priority: 'low' | 'medium' | 'high' | 'urgent'
  due_date: string | null
  notes: string
}

export interface ChatRequest {
  message: string
  session_id: string
  stream: boolean
  user_id: UserID
}

export interface Email {
  id: string
  from_name: string
  from_email: string
  subject: string
  body: string
  received_at: string
}

export interface AgendaEvent {
  id: string
  subject: string
  start: { dateTime: string }
  end: { dateTime: string }
  attendees: { emailAddress: { address: string } }[]
}

// ── Chat ──────────────────────────────────────────────
export const sendChat = (payload: ChatRequest) =>
  http.post<{ reply: string }>('/chat', payload)

// ── Tasks ─────────────────────────────────────────────
export const getTasks = (user_id: UserID, status?: string) =>
  http.get<Task[]>('/tasks', { params: { user_id, status } })

export const createTask = (data: Partial<Task> & { user_id: UserID }) =>
  http.post<Task>('/tasks', data)

export const updateTask = (task_id: string, data: Partial<Task>, user_id: UserID) =>
  http.patch<Task>(`/tasks/${task_id}`, data, { params: { user_id } })

export const deleteTask = (task_id: string, user_id: UserID) =>
  http.delete(`/tasks/${task_id}`, { params: { user_id } })

// ── Mail ──────────────────────────────────────────────
export const getInbox = (user_id: UserID) =>
  http.get<{ emails: Email[] }>('/mail/inbox', { params: { user_id } })

export const getInboxCount = (user_id: UserID) =>
  http.get<{ unread: number }>('/mail/inbox/count', { params: { user_id } })

export const sendEmail = (data: { to_email: string; subject: string; body: string; user_id: UserID }) =>
  http.post('/send_email', data)

// ── Calendar ──────────────────────────────────────────
export const getAgenda = (user_id: UserID) =>
  http.get<{ agenda: AgendaEvent[] }>('/calendar/agenda', { params: { user_id } })

// ── Agent Inbox ───────────────────────────────────────
export const getAgentInbox = (user_id: UserID) =>
  http.get('/agent/inbox', { params: { user_id } })

export const getAgentInboxSummary = (user_id: UserID) =>
  http.get('/agent/inbox/summary', { params: { user_id } })

export const resolveMessage = (message_id: string) =>
  http.patch(`/agent/message/${message_id}/resolve`)

export const rejectMessage = (message_id: string, reason?: string) =>
  http.patch(`/agent/message/${message_id}/reject`, null, { params: { reason } })

// ── Reports / Digest ──────────────────────────────────
export const getEmailDigest = (user_id: UserID) =>
  http.get<{ digest: string }>(`/digest/email/${user_id}`)

export const generateReport = (data: { user_id: UserID; sections: string[]; title: string }) =>
  http.post('/report/generate', data)

// ── Schedules ─────────────────────────────────────────
export const getSchedules = (user_id: UserID) =>
  http.get(`/schedule/list/${user_id}`)

export const createSchedule = (user_id: UserID, text: string) =>
  http.post('/schedule/create', { user_id, text })

export const deleteSchedule = (user_id: UserID, schedule_id: string) =>
  http.delete(`/schedule/${user_id}/${schedule_id}`)

// ── Contacts ──────────────────────────────────────────
// MUST pass user_id — without it the backend defaults to "user_1" and
// fails to look up OAuth tokens stored under the real Supabase UUID.
export const getContacts = (user_id: UserID) =>
  http.get('/contacts', { params: { user_id } })

// ── Proactive initiatives (P3) ─────────────────────────
export interface Initiative {
  id: string
  user_id: string
  category: string
  title: string
  body: string
  status: string
  created_at: string
}
export const getInitiatives = (user_id: UserID, limit = 10) =>
  http.get<{ initiatives: Initiative[] }>(`/initiatives/${user_id}`, { params: { limit } })
export const ackInitiative = (id: string, dismissed = false) =>
  http.post(`/initiatives/${id}/ack`, { dismissed })

// ── Delegations (P7) ───────────────────────────────────
export interface Delegation {
  id: string
  user_id: string
  from_agent: string
  to_agent: string
  task: string
  status: 'pending' | 'in_progress' | 'completed' | 'failed'
  result: string | null
  error: string | null
  created_at: string
  updated_at: string
}
export const createDelegation = (user_id: UserID, to_agent: string, task: string) =>
  http.post<Delegation>('/delegations', { user_id, to_agent, task })
export const getDelegations = (user_id: UserID, limit = 20) =>
  http.get<{ delegations: Delegation[] }>(`/delegations/${user_id}`, { params: { limit } })

// ── Operational analytics (P5) ─────────────────────────
export const getAnalyticsSummary = (period = '7d', user_id?: UserID) =>
  http.get('/analytics/summary', { params: { period, user_id } })
export const getAnalyticsTools = (period = '30d', user_id?: UserID) =>
  http.get('/analytics/tools', { params: { period, user_id } })
export const getAnalyticsTasks = (period = '30d', user_id?: UserID) =>
  http.get('/analytics/tasks', { params: { period, user_id } })
export const getAnalyticsActiveHours = (period = '30d', user_id?: UserID) =>
  http.get('/analytics/active_hours', { params: { period, user_id } })

// ── Voice (proxied to DGX) ────────────────────────────
export const tts = async (text: string): Promise<ArrayBuffer> => {
  const r = await fetch('/api/tts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text })
  })
  return r.arrayBuffer()
}

export const stt = async (audioBlob: Blob): Promise<string> => {
  const form = new FormData()
  form.append('audio', audioBlob, 'recording.wav')
  const r = await http.post<{ text: string }>('/stt', form)
  return r.data.text
}

// ── Agent outbox / messaging ──────────────────────────
export const getAgentOutbox = (user_id: UserID) =>
  http.get('/agent/outbox', { params: { user_id } })

export const sendAgentMessage = (data: {
  from_user_id: UserID; to_user_id: UserID; type: string; payload: object
}) => http.post('/agent/message', data)

// ── Ingest files ──────────────────────────────────────
export const ingestUpload = (file: File, user_id: UserID) => {
  const form = new FormData()
  form.append('file', file)
  form.append('user_id', user_id)
  return http.post('/ingest/upload', form)
}

// ── Provider connections ──────────────────────────────
export interface ProviderInfo {
  connected: boolean
  email?: string
  scopes?: string[]
  connected_at?: string
}
export interface ProviderStatus {
  google: ProviderInfo
  microsoft: ProviderInfo
}
export const getProviderStatus = (user_id: string) =>
  http.get<ProviderStatus>('/auth/provider/status', { params: { user_id } })

export const disconnectProvider = (provider: string, user_id: string) =>
  http.delete(`/auth/provider/${provider}`, { params: { user_id } })

// ── Health ────────────────────────────────────────────
export const getHealth = () => http.get('/health')
export const getHealthServices = () => http.get('/health/services')

export interface EmailDraft {
  _index: number
  sender?: string
  to?: string
  subject?: string
  draft_reply?: string
  body?: string
  triage_notes?: string
}
export const getDrafts = (user_id: UserID) =>
  http.get<{ drafts: EmailDraft[] }>(`/drafts/${user_id}`)
export const deleteDraft = (user_id: UserID, index: number) =>
  http.delete(`/drafts/${user_id}/${index}`)