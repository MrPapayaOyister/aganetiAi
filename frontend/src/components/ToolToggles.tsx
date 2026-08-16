import { useEffect, useRef, useState } from 'react'
import { SlidersHorizontal, Check } from 'lucide-react'
import { apiFetch } from '../lib/supabase'

export interface ToolGroup {
  id: string
  label: string
  description?: string
  /** False for groups a user would not meaningfully choose to turn off —
   *  ones that fire implicitly and degrade quietly, or whose label does not
   *  name something recognisable. Presentation only. */
  user_visible?: boolean
}

/**
 * Enable/disable tool groups for this chat.
 *
 * Groups, not individual tools: 19 tools is already past the point where a flat
 * switch list is usable, and "turn off email in this thread" is how people
 * actually think about it.
 *
 * The count on the trigger is not decoration. A disabled tool is absent from the
 * model's payload, so the assistant does not say "email is off" — it simply
 * cannot check mail, and a user who forgot they turned it off concludes the
 * thing is broken. Silent invisibility is the failure mode this whole surface
 * risks introducing, so the state is legible without opening anything.
 */
export default function ToolToggles({ userId, sessionId }: { userId: string; sessionId: string }) {
  const [groups, setGroups] = useState<ToolGroup[]>([])
  const [disabled, setDisabled] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let cancelled = false
    apiFetch(`/api/chat/tools?user_id=${encodeURIComponent(userId)}` +
             `&session_id=${encodeURIComponent(sessionId)}`)
      .then(r => r.json())
      .then(d => {
        if (cancelled) return
        if (Array.isArray(d.groups)) setGroups(d.groups)
        if (Array.isArray(d.disabled)) setDisabled(d.disabled)
      })
      .catch(() => { /* leave everything enabled — the safe direction */ })
    return () => { cancelled = true }
  }, [userId, sessionId])

  // Close on outside click / Escape, so the popover can't strand the composer.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const persist = async (next: string[]) => {
    setDisabled(next)                       // optimistic: the popover stays responsive
    setSaving(true)
    try {
      const r = await apiFetch('/api/chat/tools', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, session_id: sessionId,
                               disabled: next, scope: 'session' }),
      })
      const d = await r.json()
      // Trust the server's normalised list — it drops unknown group ids.
      if (Array.isArray(d.disabled)) setDisabled(d.disabled)
    } catch {
      // Roll back rather than show a toggle the backend never accepted.
      setDisabled(disabled)
    } finally {
      setSaving(false)
    }
  }

  const toggle = (id: string) =>
    persist(disabled.includes(id) ? disabled.filter(g => g !== id) : [...disabled, id])

  if (groups.length === 0) return null

  // Render rule: VISIBLE, or currently DISABLED.
  //
  // The second half is what makes a trapped state impossible. A hidden group is
  // only ever absent from this panel while it is ON; the moment it is off —
  // however that happened, including a value set through the API — it appears
  // here, can be switched back on, and then disappears again. Without this,
  // hiding a group that someone had already disabled would strand it off with no
  // route back.
  const shown = groups.filter(g => g.user_visible !== false || disabled.includes(g.id))
  // The badge counts EVERY disabled group, hidden ones included. Not counting
  // them would rebuild the silent-invisibility bug one level up: the assistant
  // would quietly lack a capability with nothing on screen to explain why.
  const offCount = disabled.length

  return (
    <div ref={rootRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        aria-label={offCount ? `Tools — ${offCount} group${offCount > 1 ? 's' : ''} off`
                             : 'Tools — all enabled'}
        data-tools-off={offCount}
        className={`press relative flex h-9 w-9 items-center justify-center rounded-xl border
                    transition-colors ${offCount
                      ? 'border-[#F5A524]/40 bg-[#F5A524]/[0.10] text-[#F5A524]'
                      : 'border-white/[0.06] bg-white/[0.04] text-[#9AA7BD] hover:text-[#E6EBF5]'}`}
      >
        <SlidersHorizontal size={15} />
        {offCount > 0 && (
          // The count, not just a dot: "something is off" prompts a click,
          // "3 are off" tells you whether that matches what you intended.
          <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center
                           rounded-full bg-[#F5A524] px-1 text-[10px] font-semibold leading-none
                           text-black">
            {offCount}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute bottom-11 left-0 z-50 w-72 rounded-2xl border border-white/[0.08]
                        bg-[#0B1220]/95 p-2 shadow-2xl backdrop-blur">
          <div className="px-2 pb-1.5 pt-1 text-[11px] text-[#6B7A91]">
            Tools for this chat{saving ? ' — saving…' : ''}
          </div>
          {shown.map(g => {
            const off = disabled.includes(g.id)
            return (
              <button
                key={g.id}
                type="button"
                onClick={() => toggle(g.id)}
                aria-pressed={!off}
                data-group={g.id}
                className="flex w-full items-start gap-2.5 rounded-xl px-2 py-1.5 text-left
                           transition-colors hover:bg-white/[0.05]"
              >
                <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center
                                  rounded border ${off
                                    ? 'border-white/[0.15]'
                                    : 'border-[#38DBFF]/50 bg-[#38DBFF]/20 text-[#38DBFF]'}`}>
                  {!off && <Check size={11} />}
                </span>
                <span className="min-w-0">
                  <span className={`block text-[13px] ${off ? 'text-[#6B7A91]' : 'text-[#E6EBF5]'}`}>
                    {g.label}
                    {g.user_visible === false && (
                      // Only ever seen while this group is off — flag it so its
                      // presence reads as deliberate rather than a stray row.
                      <span className="ml-1.5 text-[10px] uppercase tracking-wide text-[#F5A524]">
                        off · advanced
                      </span>
                    )}
                  </span>
                  {g.description && (
                    <span className="block truncate text-[11px] text-[#6B7A91]">{g.description}</span>
                  )}
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
