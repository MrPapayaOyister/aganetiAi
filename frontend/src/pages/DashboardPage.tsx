/* Dashboard — prompt-to-chart. Ask a plain-language question, the agent queries the
   live Dar Al Ber aid-request database and charts appear ONE AT A TIME as each
   save_chart tool completes (SSE chart_saved -> REST reconcile), the same live-reveal
   architecture as Hermes, adapted to agenticAi's SSE stream + its own dark theme and
   hand-rolled interactive charts. Distinct from the fixed "Analytics" page: this is
   ad-hoc, prompt-driven charting. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { LayoutDashboard, Send, Square, Plus, Trash2, X, Sparkles } from 'lucide-react'
import { http } from '../api/client'
import { apiFetch } from '../lib/supabase'
import { KpiTile, BarChartI, DonutChartI, LineChartI } from '../components/charts/DashCharts'

interface Chart { id: string; type: string; title: string; board_id: string | null; data: any[]; error: string | null }
interface Board { board_id: string; chart_count: number; last_created: string }

const BOARD_KEY = 'aganeti-dashboard-board'
const EXAMPLES = [
  'requests by category',
  'approved vs rejected requests',
  'requests by emirate',
  'monthly request trend',
]

export default function DashboardPage() {
  const [boardId, setBoardId] = useState<string>(() => localStorage.getItem(BOARD_KEY) || '')
  const [charts, setCharts] = useState<Chart[]>([])
  const [boards, setBoards] = useState<Board[]>([])
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [status, setStatus] = useState('')
  const [reply, setReply] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const boardRef = useRef(boardId)
  boardRef.current = boardId

  // ── data ──────────────────────────────────────────────────────────────────
  const loadBoards = useCallback(async () => {
    try { setBoards((await http.get('/dashboard/boards')).data.boards || []) } catch { /* ignore */ }
  }, [])

  const reconcile = useCallback(async () => {
    const bid = boardRef.current
    try {
      const res = await http.get('/dashboard/charts', { params: { board_id: bid, include_unclaimed: true } })
      const fetched: Chart[] = res.data.charts || []
      // Claim any just-created untagged chart to this board so it never ghosts elsewhere.
      await Promise.all(fetched.filter(c => !c.board_id && bid).map(c =>
        http.post(`/dashboard/charts/${c.id}/board`, { board_id: bid }).catch(() => {})))
      setCharts(prev => {
        const byId = new Map(fetched.map(c => [c.id, c]))
        const prevIds = new Set(prev.map(c => c.id))
        const kept = prev.filter(c => byId.has(c.id)).map(c => byId.get(c.id)!)
        const appended = fetched.filter(c => !prevIds.has(c.id))
        return [...kept, ...appended]
      })
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { void reconcile(); void loadBoards() }, [reconcile, loadBoards, boardId])

  // ── boards ────────────────────────────────────────────────────────────────
  const newBoard = useCallback(async () => {
    try {
      const { data } = await http.post('/dashboard/board-session', {})
      localStorage.setItem(BOARD_KEY, data.board_id)
      setCharts([]); setReply(''); setStatus('')
      setBoardId(data.board_id)
    } catch { /* ignore */ }
  }, [])

  const selectBoard = useCallback((bid: string) => {
    if (bid === boardRef.current) return
    localStorage.setItem(BOARD_KEY, bid)
    setCharts([]); setReply(''); setStatus('')
    setBoardId(bid)
  }, [])

  const deleteBoard = useCallback(async (bid: string) => {
    if (!confirm('Delete this board and its charts?')) return
    try { await http.delete(`/dashboard/boards/${bid}`) } catch { /* ignore */ }
    if (bid === boardRef.current) { setCharts([]); localStorage.removeItem(BOARD_KEY); setBoardId('') }
    void loadBoards()
  }, [loadBoards])

  const removeChart = useCallback(async (id: string) => {
    setCharts(prev => prev.filter(c => c.id !== id))
    try { await http.delete(`/dashboard/charts/${id}`) } catch { /* ignore */ }
  }, [])

  // ── streaming (dashboard SSE: token / tool_call / chart_saved / done) ───────
  const send = useCallback(async (text: string) => {
    if (!text.trim() || streaming) return
    // Ensure we have a board so charts are scoped from the first prompt.
    let bid = boardRef.current
    if (!bid) {
      try { bid = (await http.post('/dashboard/board-session', {})).data.board_id } catch { bid = '' }
      if (bid) { localStorage.setItem(BOARD_KEY, bid); setBoardId(bid); boardRef.current = bid }
    }
    setStreaming(true); setReply(''); setStatus('Thinking…'); setInput('')
    abortRef.current = new AbortController()
    try {
      const res = await apiFetch('/api/dashboard/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, board_id: bid }),
        signal: abortRef.current.signal,
      })
      if (!res.ok || !res.body) { setStatus('Something went wrong.'); setStreaming(false); return }
      const reader = res.body.getReader()
      const dec = new TextDecoder()
      let buf = ''
      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        const lines = buf.split('\n'); buf = lines.pop() ?? ''
        for (const line of lines) {
          if (!line.startsWith('data:')) continue
          const raw = line.slice(line.indexOf(':') + 1).trim()
          if (raw === '[DONE]') continue
          let ev: any
          try { ev = JSON.parse(raw) } catch { continue }
          if (ev.type === 'tool_call') {
            const n = ev.name as string
            setStatus(n === 'save_chart' ? 'Creating a chart…'
              : n === 'query_data' ? 'Reading the data…'
              : n === 'get_database_schema' ? 'Looking at the data…'
              : n === 'delete_chart' ? 'Removing a chart…' : 'Working…')
          } else if (ev.type === 'chart_saved') {
            void reconcile()
          } else if (ev.type === 'token') {
            setReply(ev.content || '')
          } else if (ev.type === 'done') {
            if (ev.final) setReply(ev.final)
            setStatus('')
          } else if (ev.type === 'error') {
            setStatus(ev.message || 'Something went wrong.')
          }
        }
      }
      await reconcile()
    } catch (e: any) {
      if (e?.name !== 'AbortError') setStatus('Connection lost.')
    } finally {
      setStreaming(false); setStatus(''); void loadBoards()
    }
  }, [streaming, reconcile, loadBoards])

  const stop = useCallback(() => { abortRef.current?.abort() }, [])

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void send(input) }
  }

  const hasCharts = charts.length > 0

  // ── render ──────────────────────────────────────────────────────────────────
  return (
    <div className="relative z-10 h-full flex flex-col px-6 pt-6 pb-4 max-w-[1400px] mx-auto w-full">
      {/* header */}
      <div className="flex items-center gap-3 mb-1">
        <div className="w-9 h-9 rounded-xl grid place-items-center"
          style={{ background: 'rgba(0,212,255,0.12)', border: '1px solid rgba(0,212,255,0.28)' }}>
          <LayoutDashboard size={18} style={{ color: '#00D4FF' }} />
        </div>
        <div>
          <h1 className="t-title">Dashboard</h1>
          <p className="t-caption">Ask a question in plain language — charts build live from the aid-request data.</p>
        </div>
      </div>

      {/* prompt bar */}
      <div className="composer-dock flex items-end gap-2 mt-4 mb-2 p-2">
        <textarea
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={onKey}
          rows={1}
          placeholder="e.g. show requests by category and approved vs rejected"
          className="flex-1 bg-transparent resize-none outline-none t-body px-2 py-1.5 max-h-32"
          disabled={streaming}
        />
        {streaming ? (
          <button onClick={stop} className="neu-pill px-3 py-2 flex items-center gap-1.5" style={{ color: '#FF4466' }}>
            <Square size={15} /> Stop
          </button>
        ) : (
          <button onClick={() => void send(input)} disabled={!input.trim()}
            className="neu-pill px-3 py-2 flex items-center gap-1.5 disabled:opacity-40" style={{ color: '#00D4FF' }}>
            <Send size={15} /> Ask
          </button>
        )}
      </div>

      {/* status / reply line */}
      <div className="h-6 mb-2 flex items-center gap-2 t-caption">
        {status && <span className="flex items-center gap-1.5" style={{ color: '#00D4FF' }}>
          <Sparkles size={12} className="animate-pulse" />{status}</span>}
        {!status && reply && <span style={{ color: 'var(--text-secondary,#9AA7BD)' }}>{reply}</span>}
      </div>

      {/* body: grid + boards rail */}
      <div className="flex-1 min-h-0 flex gap-4">
        <div className="flex-1 min-h-0 overflow-y-auto pr-1">
          {!hasCharts ? (
            <div className="h-full grid place-items-center">
              <div className="text-center max-w-md">
                <div className="w-14 h-14 rounded-2xl grid place-items-center mx-auto mb-4 neu">
                  <LayoutDashboard size={24} style={{ color: '#00D4FF' }} />
                </div>
                <h3 className="t-heading mb-1">No charts yet</h3>
                <p className="t-caption mb-4">Ask for a chart and it appears here live. Try one:</p>
                <div className="flex flex-wrap gap-2 justify-center">
                  {EXAMPLES.map(ex => (
                    <button key={ex} disabled={streaming} onClick={() => void send(ex)}
                      className="neu-pill px-3 py-1.5 t-caption disabled:opacity-40" style={{ color: '#00D4FF' }}>
                      Try: “{ex}”
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))' }}>
              <AnimatePresence mode="popLayout">
                {charts.map((c, i) => (
                  <motion.div
                    key={c.id} layout
                    initial={{ opacity: 0, y: 16, scale: 0.97 }}
                    animate={{ opacity: 1, y: 0, scale: 1 }}
                    exit={{ opacity: 0, scale: 0.95 }}
                    transition={{ delay: Math.min(i * 0.06, 0.4), duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
                    className="neu rounded-2xl p-4 group"
                  >
                    <div className="flex items-start justify-between mb-3">
                      <h3 className="t-heading pr-2">{c.title}</h3>
                      <button onClick={() => void removeChart(c.id)}
                        className="opacity-0 group-hover:opacity-100 transition-opacity"
                        style={{ color: 'var(--text-muted,#5C6B85)' }} title="Remove chart">
                        <Trash2 size={14} />
                      </button>
                    </div>
                    {c.error ? (
                      <div className="t-caption h-[180px] grid place-items-center" style={{ color: '#FF4466' }}>
                        Couldn’t load this chart.
                      </div>
                    ) : c.type === 'kpi' ? (
                      <KpiTile value={Number(c.data?.[0]?.value ?? 0)} />
                    ) : c.type === 'pie' ? (
                      <DonutChartI data={c.data || []} />
                    ) : c.type === 'line' ? (
                      <LineChartI data={c.data || []} />
                    ) : (
                      <BarChartI data={c.data || []} />
                    )}
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          )}
        </div>

        {/* boards rail */}
        <aside className="w-52 shrink-0 hidden lg:flex flex-col gap-2">
          <button onClick={() => void newBoard()} className="neu-pill px-3 py-2 flex items-center justify-center gap-1.5 t-label" style={{ color: '#00D4FF' }}>
            <Plus size={15} /> New board
          </button>
          <div className="t-caption px-1 mt-1">History</div>
          <div className="flex-1 overflow-y-auto flex flex-col gap-1.5">
            {boards.length === 0 && <div className="t-caption px-1 opacity-60">No boards yet</div>}
            {boards.map(b => (
              <div key={b.board_id}
                className={`group flex items-center gap-2 px-2.5 py-2 rounded-xl cursor-pointer transition-colors
                  ${b.board_id === boardId ? 'neu-inset' : 'hover:bg-white/[0.04]'}`}
                onClick={() => selectBoard(b.board_id)}>
                <LayoutDashboard size={13} style={{ color: b.board_id === boardId ? '#00D4FF' : '#5C6B85' }} />
                <span className="t-caption truncate flex-1">{b.chart_count} chart{b.chart_count === 1 ? '' : 's'}</span>
                <button onClick={e => { e.stopPropagation(); void deleteBoard(b.board_id) }}
                  className="opacity-0 group-hover:opacity-100 transition-opacity" style={{ color: '#FF4466' }}>
                  <X size={13} />
                </button>
              </div>
            ))}
          </div>
        </aside>
      </div>
    </div>
  )
}
