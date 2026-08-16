import { useEffect, useRef, useState } from 'react'
import { ExternalLink, Play, History, Tv, Radio as RadioIcon, Check as CheckIcon, Loader2 } from 'lucide-react'
import {
  renderEmbed, isValidVideoId, youtubeEmbedUrl, cspFor,
  type EmbedItem, type SearchResult, type DirectoryChannel, type NewsArticle,
} from '../lib/embedWidget'

/**
 * One inline YouTube player.
 *
 * This iframe carries `allow-same-origin`, which the srcdoc embeds must never
 * have — and the difference is the whole point. Here the frame loads YouTube's
 * OWN page via `src=`, so "same origin" means youtube-nocookie.com, not us. A
 * srcdoc frame inherits the embedder's origin, so the same flag there would hand
 * tool-authored markup our DOM, cookies and Supabase session.
 *
 *   src=   + allow-same-origin  →  the frame is YouTube, isolated from us   ✅
 *   srcdoc + allow-same-origin  →  the frame is US, running tool HTML       ❌
 *
 * If you ever point this component at tool-authored HTML, the flag must go.
 */
function VideoEmbed({ videoId, start }: { videoId: string; start: number }) {
  return (
    <div className="w-full max-w-full overflow-hidden rounded-xl border border-white/[0.06] bg-black/40">
      <iframe
        className="block h-full w-full aspect-video"
        src={youtubeEmbedUrl(videoId, start)}
        title="YouTube video player"
        loading="lazy"
        referrerPolicy="strict-origin-when-cross-origin"
        sandbox="allow-scripts allow-same-origin allow-presentation"
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"
        allowFullScreen
      />
    </div>
  )
}

function hhmmss(total?: number): string {
  if (!total || total <= 0) return ''
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
           : `${m}:${String(s).padStart(2, '0')}`
}

/**
 * Search results the user picks from.
 *
 * Clicking a row mounts the real player above the list — the list stays so a
 * different result is one click away. The reference implementation did this by
 * injecting a nested iframe inside the sandboxed card, which cannot work: the
 * nested frame inherits the sandbox and renders black. Here the rows are plain
 * React buttons and the player is a sibling, outside the sandbox entirely.
 */
function ResultPicker({ results, query }: { results: SearchResult[]; query?: string | null }) {
  const [picked, setPicked] = useState<string | null>(null)
  const usable = results.filter(r => isValidVideoId(r.id))
  if (usable.length === 0) return null

  return (
    <div className="space-y-2">
      {picked && <VideoEmbed videoId={picked} start={0} />}
      {query && (
        <div className="text-[11px] text-[#6B7A91] px-0.5">
          Results for “{query}” — pick one to play
        </div>
      )}
      <div className="space-y-1.5">
        {usable.map((r, i) => {
          const active = picked === r.id
          return (
            <button
              key={`${r.id}-${i}`}
              type="button"
              onClick={() => setPicked(r.id)}
              aria-pressed={active}
              className={`press flex w-full items-center gap-2.5 rounded-xl p-1.5 text-left transition-colors
                          border ${active
                            ? 'bg-[#38DBFF]/[0.10] border-[#38DBFF]/30'
                            : 'bg-white/[0.03] border-white/[0.06] hover:bg-white/[0.06]'}`}
            >
              <span className="relative shrink-0">
                {r.thumb
                  ? <img src={r.thumb} alt="" loading="lazy"
                         className="h-[54px] w-24 rounded-lg object-cover bg-black" />
                  : <span className="block h-[54px] w-24 rounded-lg bg-black" />}
                {r.duration ? (
                  <span className="absolute bottom-0.5 right-0.5 rounded bg-black/80 px-1
                                   text-[10px] leading-tight text-white">
                    {hhmmss(r.duration)}
                  </span>
                ) : null}
              </span>
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="line-clamp-2 text-[13px] font-medium leading-snug text-[#E6EBF5]">
                  {r.title}
                </span>
                {r.channel && (
                  <span className="truncate text-[11px] text-[#9AA7BD]">{r.channel}</span>
                )}
              </span>
              <Play size={13} className={`ml-auto mr-1 shrink-0 ${
                active ? 'text-[#38DBFF]' : 'text-[#4A6080]'}`} />
            </button>
          )
        })}
      </div>
    </div>
  )
}

/**
 * Channel directory results — multi-select, then commit.
 *
 * A sibling of ResultPicker rather than a reuse of it. The visual language is
 * the same, the interaction is not: ResultPicker is click-a-row-and-act (one
 * click, one video), while adding channels is tick-several-then-press-Add.
 * Folding both into one component would mean a mode flag inside something whose
 * whole job is a single click.
 *
 * Selecting here does not add anything by itself — pressing Add asks the
 * assistant, which re-validates every stream before it is stored.
 */
/**
 * A component with a `kind` discriminator: EVERY STATE GETS BROWSER-VERIFIED,
 * not just the one a fix happens to touch.
 *
 * This is written down because it was learned twice on this component in one
 * sitting. It was shared between Live TV and Radio, and the TV assumptions came
 * out one at a time:
 *   1. code review found the header noun ("channels" vs "stations");
 *   2. a browser check then found the hardcoded <Tv> icon and the picked-state
 *      button label — after review had already passed the file twice;
 *   3. a second browser check found the IDLE button label, in a branch that had
 *      been read while fixing the one right next to it.
 *
 * Reading a component for "the places kind matters" reliably misses one, because
 * the states you are not currently looking at do not render in your head.
 * Mounting it and switching kind does not miss them.
 */
function ChannelPicker({ channels, kind = 'tv', onAdd }: {
  channels: DirectoryChannel[]
  /** Which library a tick commits to. The component is shared between Live TV
   *  and Radio; only the wording and the follow-up differ. */
  kind?: string
  onAdd: (names: string[], kind: string) => void
}) {
  const [picked, setPicked] = useState<Set<string>>(new Set())
  const [sent, setSent] = useState(false)
  const CAP = 25

  const toggle = (name: string) => setPicked(prev => {
    const next = new Set(prev)
    if (next.has(name)) next.delete(name)
    else if (next.size < CAP) next.add(name)
    return next
  })

  if (channels.length === 0) return null
  const atCap = picked.size >= CAP

  return (
    <div className="space-y-2">
      <div className="px-0.5 text-[11px] text-[#6B7A91]">
        {channels.length} live {kind === 'radio' ? 'station' : 'channel'}
        {channels.length > 1 ? 's' : ''} found — tick the
        ones to add{atCap ? ` (max ${CAP})` : ''}
      </div>
      <div className="space-y-1.5">
        {channels.map((c, i) => {
          const on = picked.has(c.name)
          return (
            <button
              key={`${c.id}-${i}`}
              type="button"
              onClick={() => toggle(c.name)}
              aria-pressed={on}
              data-channel={c.name}
              disabled={sent}
              className={`press flex w-full items-center gap-2.5 rounded-xl p-2 text-left
                          transition-colors border ${on
                            ? 'bg-[#38DBFF]/[0.10] border-[#38DBFF]/30'
                            : 'bg-white/[0.03] border-white/[0.06] hover:bg-white/[0.06]'}`}
            >
              <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded
                                border ${on ? 'border-[#38DBFF]/50 bg-[#38DBFF]/20 text-[#38DBFF]'
                                             : 'border-white/[0.15]'}`}>
                {on && <CheckIcon size={11} />}
              </span>
              {/* The picker is shared between Live TV and Radio; a TV glyph on a
                  radio station is a small thing that makes the whole row look
                  like it came from the wrong tool. */}
              {kind === 'radio'
                ? <RadioIcon size={13} className="shrink-0 text-[#4A6080]" />
                : <Tv size={13} className="shrink-0 text-[#4A6080]" />}
              <span className="flex min-w-0 flex-col">
                <span className="truncate text-[13px] text-[#E6EBF5]">{c.name}</span>
                {(c.country || c.category) && (
                  <span className="truncate text-[11px] text-[#6B7A91]">
                    {[c.country, c.category, c.quality].filter(Boolean).join(' · ')}
                  </span>
                )}
              </span>
            </button>
          )
        })}
      </div>
      <button
        type="button"
        data-commit="add-channels"
        disabled={picked.size === 0 || sent}
        onClick={() => { setSent(true); onAdd([...picked], kind) }}
        className="press inline-flex items-center gap-1.5 rounded-lg border
                   border-[#38DBFF]/30 bg-[#38DBFF]/[0.10] px-3 py-1.5 text-[12px]
                   text-[#38DBFF] disabled:opacity-40"
      >
        {sent && <Loader2 size={11} className="animate-spin" />}
        {sent ? 'Adding…' : picked.size
          ? `Add ${picked.size} ${kind === 'radio' ? 'station' : 'channel'}${picked.size > 1 ? 's' : ''}`
                                        : `Select ${kind === 'radio' ? 'stations' : 'channels'} to add`}
      </button>
    </div>
  )
}

/**
 * Real, clickable links for the stories in a news card.
 *
 * The card itself cannot carry an anchor: it is sandboxed to `allow-scripts`
 * only, so a target=_blank popup from inside gets an opaque origin and the
 * browser refuses it — the same wall the YouTube card hit. These live out here,
 * in our own DOM, where they are ordinary links.
 *
 * This replaces a fallback that asked the MODEL to cite each story as markdown.
 * Measured, that fallback produced links when the model listed headlines (5/5
 * articles, 4/4 trials) and NONE when it summarised — "what's the top tech
 * story? just one" gave zero links in 2/2 trials, and a 20-article request
 * linked only 13. A fallback that depends on the model remembering is not one.
 *
 * ALL stories get a link rather than only the one selected in the card's detail
 * pane. Selection lives inside the iframe, so driving this from it would mean
 * accepting a new inbound postMessage type from tool-authored HTML — a new
 * trust surface for a worse result: one link at a time, and nothing at all if
 * the card's script fails.
 */
function ArticleLinks({ articles }: { articles: NewsArticle[] }) {
  const usable = articles.filter(a => a?.url && /^https?:\/\//i.test(a.url))
  if (usable.length === 0) return null
  return (
    <div className="space-y-1">
      <div className="px-0.5 text-[11px] text-[#6B7A91]">Open a story</div>
      {usable.map((a, i) => (
        <a
          key={`${a.url}-${i}`}
          href={a.url}
          target="_blank"
          rel="noopener noreferrer"
          data-article={a.url}
          className="press flex w-full items-center gap-2 rounded-lg border
                     border-white/[0.06] bg-white/[0.03] px-2.5 py-1.5 text-[12px]
                     text-[#9AA7BD] transition-colors hover:border-white/[0.12]
                     hover:text-[#E6EBF5]"
        >
          <ExternalLink size={11} className="shrink-0 text-[#38DBFF]" />
          {a.category && (
            <span className="shrink-0 text-[10px] uppercase tracking-wide text-[#4A6080]">
              {a.category}
            </span>
          )}
          <span className="truncate">{a.title}</span>
        </a>
      ))}
    </div>
  )
}

/**
 * "as of …" for a widget replayed from history.
 *
 * Only rehydrated embeds carry `renderedAt`, so this never appears on a live
 * one. It matters because a stored widget is a SNAPSHOT: the tool is not re-run
 * on reload (that would fire side effects, cost latency, and show different data
 * from what the user actually saw), so a search picker or a weather card can be
 * arbitrarily old and says so.
 */
function RenderedAt({ at }: { at: string }) {
  const when = new Date(at)
  if (Number.isNaN(when.getTime())) return null
  return (
    <div className="flex items-center gap-1 px-0.5 text-[10px] text-[#6B7A91]">
      <History size={9} />
      <span>as of {when.toLocaleString(undefined, {
        dateStyle: 'medium', timeStyle: 'short',
      })}</span>
    </div>
  )
}

/** One tool-authored HTML document in its own sandboxed srcdoc iframe. */
function SandboxedCard({ html, csp }: { html: string; csp?: string | null }) {
  const hostRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    // cspFor() resolves a PROFILE NAME to a policy. The tool never supplies a
    // policy, only a name, so an embed cannot widen its own sandbox.
    return renderEmbed(host, html, { csp: cspFor(csp) })
  }, [html, csp])
  return <div ref={hostRef} />
}

/**
 * Renders the `embeds` a tool call produced, inside the assistant's slab.
 *
 * Each embed picks exactly one presentation, most specific first:
 *
 *   video   → a native player at the provider's own origin (VideoEmbed)
 *   results → a React picker; selecting a row mounts that same player
 *   html    → a sandboxed srcdoc iframe, `allow-scripts` ONLY
 *
 * `html` is always sent and is the fallback for a client that understands
 * neither of the structured forms. `link` renders alongside whichever won, as
 * the escape hatch for anyone who can't or won't play inline.
 *
 * `title`/`label` arrive raw and are escaped by React on render. Do not
 * pre-escape them upstream or an & or ' comes out as &amp; / &#x27;.
 */
export default function ToolEmbeds({ embeds, onAddChannels }: {
  embeds: EmbedItem[]
  /** Asks the assistant to add the picked channels — it re-validates each. */
  onAddChannels?: (names: string[], kind: string) => void
}) {
  if (embeds.length === 0) return null

  return (
    <div className="mt-3 space-y-2">
      {embeds.map((e, i) => {
        const results = (e.results ?? []).filter(r => isValidVideoId(r?.id))
        let body
        if (isValidVideoId(e.video?.id)) {
          body = <VideoEmbed videoId={e.video!.id} start={e.video!.start ?? 0} />
        } else if (results.length > 0) {
          body = <ResultPicker results={results} query={e.query} />
        } else if (e.channels && e.channels.length > 0 && onAddChannels) {
          body = <ChannelPicker channels={e.channels}
                                kind={e.channel_kind || 'tv'}
                                onAdd={onAddChannels} />
        } else if (e.html) {
          body = <SandboxedCard html={e.html} csp={e.csp} />
        } else {
          // Oversized on the way in: the HTML was dropped, the structured half
          // was not. Nothing to render here, but the link below still works.
          body = null
        }
        return (
          <div key={i} className="space-y-2">
            {body}
            {e.articles && e.articles.length > 0 && <ArticleLinks articles={e.articles} />}
            {e.renderedAt && <RenderedAt at={e.renderedAt} />}
            {e.link?.url && (
              <a
                href={e.link.url}
                target="_blank"
                rel="noopener noreferrer"
                className="press inline-flex max-w-full items-center gap-1.5 rounded-lg
                           border border-white/[0.06] bg-white/[0.04] px-2.5 py-1.5
                           text-[12px] text-[#9AA7BD] transition-colors
                           hover:border-white/[0.12] hover:text-[#E6EBF5]"
              >
                <ExternalLink size={11} className="shrink-0 text-[#38DBFF]" />
                <span className="truncate">Watch on YouTube — {e.link.label}</span>
              </a>
            )}
          </div>
        )
      })}
    </div>
  )
}
