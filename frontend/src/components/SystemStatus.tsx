import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import axios from 'axios'
import { Activity, Server, Cpu, Database, Mic, Volume2 } from 'lucide-react'

type Status = 'ok' | 'degraded' | 'down' | 'checking'

interface ServiceStatus {
  name: string
  status: Status
  latency?: number
  icon: React.ElementType
}

const statusColor = {
  ok:       '#00FF88',
  degraded: '#FFB800',
  down:     '#FF4466',
  checking: '#4A6080',
}

const statusLabel = {
  ok: 'Operational',
  degraded: 'Degraded',
  down: 'Down',
  checking: 'Checking…',
}

async function ping(path: string): Promise<{ status: Status; latency: number }> {
  const t0 = performance.now()
  try {
    await axios.get(path, { timeout: 5000 })
    return { status: 'ok', latency: Math.round(performance.now() - t0) }
  } catch (e: any) {
    if (e.response) return { status: 'degraded', latency: Math.round(performance.now() - t0) }
    return { status: 'down', latency: 0 }
  }
}

export function SystemStatus() {
  const [services, setServices] = useState<ServiceStatus[]>([
    { name: 'FastAPI', status: 'checking', icon: Server },
    { name: 'LLM Smart (14B)', status: 'checking', icon: Cpu },
    { name: 'LLM Fast (1.5B)', status: 'checking', icon: Cpu },
    { name: 'Vector DB', status: 'checking', icon: Database },
    { name: 'TTS', status: 'checking', icon: Volume2 },
    { name: 'STT', status: 'checking', icon: Mic },
  ])

  const checkAll = async () => {
    const checks = await Promise.allSettled([
      ping('/api/health'),
      ping('/api/health/services').then(r => r).catch(() => ({ status: 'down' as Status, latency: 0 })),
      ping('/api/health/services').then(r => r).catch(() => ({ status: 'down' as Status, latency: 0 })),
      ping('/api/health/services').then(r => r).catch(() => ({ status: 'down' as Status, latency: 0 })),
      ping('/api/health').then(r => r).catch(() => ({ status: 'down' as Status, latency: 0 })),
      ping('/api/health').then(r => r).catch(() => ({ status: 'down' as Status, latency: 0 })),
    ])

    // FastAPI: ping /health directly
    const apiResult = await ping('/api/health')

    setServices(prev => prev.map((s, i) => {
      if (i === 0) return { ...s, ...apiResult }
      const r = checks[i]
      if (r.status === 'fulfilled') return { ...s, ...r.value }
      return { ...s, status: 'down', latency: 0 }
    }))
  }

  useEffect(() => {
    checkAll()
    const t = setInterval(checkAll, 10_000)
    return () => clearInterval(t)
  }, [])

  const allOk = services.every(s => s.status === 'ok')
  const anyDown = services.some(s => s.status === 'down')

  return (
    <div className="glass rounded-2xl p-4 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Activity size={15} className="text-[#00D4FF]" />
          <span className="text-sm font-semibold text-[#E2E8F0]">System Status</span>
        </div>
        <div className="flex items-center gap-1.5">
          <motion.div
            className="w-2 h-2 rounded-full"
            style={{ background: anyDown ? '#FF4466' : allOk ? '#00FF88' : '#FFB800' }}
            animate={{ opacity: [1, 0.4, 1] }}
            transition={{ duration: 2, repeat: Infinity }}
          />
          <span className="text-xs text-[#4A6080]">
            {anyDown ? 'Degraded' : allOk ? 'All systems operational' : 'Partial outage'}
          </span>
        </div>
      </div>

      {/* Service list */}
      <div className="space-y-2">
        {services.map((s, i) => {
          const Icon = s.icon
          const color = statusColor[s.status]
          return (
            <motion.div
              key={s.name}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: i * 0.05 }}
              className="flex items-center justify-between py-1.5 border-b border-[#1E3A5F]/20 last:border-0"
            >
              <div className="flex items-center gap-2">
                <Icon size={13} className="text-[#4A6080]" />
                <span className="text-sm text-[#94A3B8]">{s.name}</span>
              </div>
              <div className="flex items-center gap-2">
                {s.latency !== undefined && s.status !== 'checking' && s.latency > 0 && (
                  <span className="text-xs text-[#4A6080]">{s.latency}ms</span>
                )}
                <div className="flex items-center gap-1.5">
                  <motion.div
                    className="w-1.5 h-1.5 rounded-full"
                    style={{ background: color }}
                    animate={s.status === 'checking' ? { opacity: [0.3, 1, 0.3] } : {}}
                    transition={{ duration: 1, repeat: Infinity }}
                  />
                  <span className="text-xs" style={{ color }}>
                    {statusLabel[s.status]}
                  </span>
                </div>
              </div>
            </motion.div>
          )
        })}
      </div>

      <button
        onClick={checkAll}
        className="text-xs text-[#4A6080] hover:text-[#00D4FF] transition-colors"
      >
        Refresh status
      </button>
    </div>
  )
}
