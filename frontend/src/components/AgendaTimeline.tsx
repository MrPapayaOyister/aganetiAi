import { useEffect, useState } from 'react'
import { motion } from 'framer-motion'
import { useQuery } from '@tanstack/react-query'
import { Calendar, Clock, Users } from 'lucide-react'
import { getAgenda } from '../api/client'
import type { UserID, AgendaEvent } from '../api/client'
import { SkeletonCard } from './SkeletonCard'

function formatTime(dt: string) {
  return new Date(dt).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: true })
}

function formatDuration(start: string, end: string) {
  const mins = (new Date(end).getTime() - new Date(start).getTime()) / 60000
  if (mins < 60) return `${mins}m`
  return `${Math.floor(mins / 60)}h${mins % 60 > 0 ? ` ${mins % 60}m` : ''}`
}

function getCurrentPercent(): number {
  const now = new Date()
  const start = new Date(now); start.setHours(8, 0, 0, 0)
  const end = new Date(now); end.setHours(20, 0, 0, 0)
  const total = end.getTime() - start.getTime()
  const elapsed = now.getTime() - start.getTime()
  return Math.max(0, Math.min(100, (elapsed / total) * 100))
}

interface AgendaTimelineProps {
  userId: UserID
}

export function AgendaTimeline({ userId }: AgendaTimelineProps) {
  const [nowPercent, setNowPercent] = useState(getCurrentPercent)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['agenda', userId],
    queryFn: () => getAgenda(userId).then(r => r.data),
    staleTime: 300_000,
  })

  useEffect(() => {
    const t = setInterval(() => setNowPercent(getCurrentPercent()), 60_000)
    return () => clearInterval(t)
  }, [])

  // Guard: backend historically returned a formatted string here; only ever map an array.
  const events: AgendaEvent[] = Array.isArray(data?.agenda) ? data.agenda : []

  if (isLoading) return <SkeletonCard lines={4} className="mt-2" />

  if (isError || events.length === 0) {
    return (
      <div className="glass rounded-2xl p-6 text-center">
        <Calendar size={28} className="mx-auto text-[#1E3A5F] mb-2" />
        <p className="text-[#4A6080] text-sm">No events today</p>
      </div>
    )
  }

  return (
    <div className="glass rounded-2xl p-4 relative overflow-hidden">
      <div className="flex items-center gap-2 mb-4">
        <Calendar size={16} className="text-[#00D4FF]" />
        <span className="text-sm font-semibold text-[#E2E8F0]">Today's Agenda</span>
      </div>

      <div className="relative">
        {/* Timeline bar */}
        <div className="absolute left-[72px] top-0 bottom-0 w-px timeline-line" />

        {/* Current time indicator */}
        <motion.div
          className="absolute left-[68px] w-2 h-2 rounded-full bg-[#FF4466] z-10 shadow-[0_0_6px_rgba(255,68,102,0.8)]"
          style={{ top: `${nowPercent}%`, transform: 'translateY(-50%) translateX(-50%)' }}
          animate={{ opacity: [1, 0.5, 1] }}
          transition={{ duration: 2, repeat: Infinity }}
        />

        {/* Events */}
        <div className="space-y-3">
          {events.map((event, i) => {
            const isPast = new Date(event.end.dateTime) < new Date()
            const isNow = new Date(event.start.dateTime) <= new Date() &&
                          new Date(event.end.dateTime) >= new Date()
            const attendeeCount = event.attendees?.length ?? 0

            return (
              <motion.div
                key={event.id ?? i}
                initial={{ opacity: 0, x: -12 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.07 }}
                className={`flex items-start gap-3 ${isPast ? 'opacity-40' : ''}`}
              >
                {/* Time */}
                <div className="w-16 shrink-0 text-right">
                  <span className="text-[11px] text-[#4A6080]">
                    {formatTime(event.start.dateTime)}
                  </span>
                </div>

                {/* Dot */}
                <div className="relative z-10 mt-1">
                  <div
                    className={`w-2.5 h-2.5 rounded-full border-2 transition-all
                      ${isNow
                        ? 'border-[#00D4FF] bg-[#00D4FF] shadow-[0_0_8px_rgba(0,212,255,0.6)]'
                        : isPast
                        ? 'border-[#1E3A5F] bg-[#1E3A5F]'
                        : 'border-[#7B2FFF] bg-transparent'
                      }`}
                  />
                </div>

                {/* Event card */}
                <div
                  className={`flex-1 glass-sm rounded-xl p-3 transition-all
                    ${isNow ? 'border-[#00D4FF]/30' : ''}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className={`text-sm font-medium leading-snug
                                     ${isPast ? 'text-[#4A6080]' : 'text-[#E2E8F0]'}`}>
                        {event.subject}
                      </p>
                      <div className="flex items-center gap-3 mt-1">
                        <span className="flex items-center gap-1 text-[11px] text-[#4A6080]">
                          <Clock size={10} />
                          {formatDuration(event.start.dateTime, event.end.dateTime)}
                        </span>
                        {attendeeCount > 0 && (
                          <span className="flex items-center gap-1 text-[11px] text-[#4A6080]">
                            <Users size={10} />
                            {attendeeCount}
                          </span>
                        )}
                      </div>
                    </div>
                    {isNow && (
                      <span className="shrink-0 text-[10px] font-semibold px-2 py-0.5 rounded-full
                                       bg-[#00D4FF]/15 text-[#00D4FF] border border-[#00D4FF]/25">
                        NOW
                      </span>
                    )}
                  </div>
                </div>
              </motion.div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
