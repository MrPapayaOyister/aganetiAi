/**
 * Sandboxed rendering of tool-produced HTML (the `embeds` SSE event).
 *
 * Ported from Open WebUI's src/lib/components/common/FullHeightIframe.svelte and
 * src/lib/utils/csp.ts, via reference/yt-tool-service/embed-widget.js. Kept as a
 * framework-agnostic function rather than a component so the security-critical
 * parts sit in one auditable place; ToolEmbeds.tsx is the thin React wrapper.
 *
 * Four properties here are load-bearing. Do not relax them without understanding
 * what each one stops:
 *
 *  1. sandbox="allow-scripts" ONLY. allow-same-origin must stay off: srcdoc is
 *     same-origin with this app, so combining the two would hand tool HTML our
 *     DOM, cookies and localStorage (including the Supabase session). Inline
 *     third-party playback would need the embed served from a separate origin
 *     via src=, not this flag. allow-forms, allow-downloads and allow-popups
 *     stay off too — anything a tool wants to link to is rendered by React
 *     outside the frame instead (see ToolEmbeds.tsx).
 *  2. Height messages are validated by `event.source === iframe.contentWindow`,
 *     NOT by event.origin. A srcdoc iframe's origin is the string "null", so an
 *     origin check here is either a no-op or wrong; window identity is the only
 *     thing that actually distinguishes our frame from any other sender.
 *  3. The reported height is clamped. A buggy or hostile embed reporting 10^9px
 *     would otherwise destroy the page layout.
 *  4. The CSP <meta> is injected as the FIRST child of <head>. Per spec the
 *     first CSP meta tag wins, so this overrides any policy the tool's own HTML
 *     tried to set for itself.
 */

/**
 * Content-Security-Policy applied inside every embed.
 *
 * `'self'` is deliberately absent: with allow-same-origin off the frame's origin
 * is opaque, so 'self' matches nothing and reads as a false reassurance.
 *
 * frame-src is required — without it the nested youtube.com player falls back to
 * default-src and is blocked outright. That was latent in the reference, whose
 * default settings render a thumbnail card with no nested iframe at all.
 */
export const DEFAULT_EMBED_CSP = [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline'",
  'img-src https: data:',
  'media-src https: blob:',
  'frame-src https://www.youtube.com https://www.youtube-nocookie.com',
].join('; ')

/**
 * Named CSP profiles. A tool declares a profile NAME in its embed payload; the
 * policy itself is defined HERE and never travels with the tool.
 *
 * That direction matters. If a tool could author its own CSP, a careless or
 * compromised one would simply grant itself whatever it wanted, and the sandbox
 * would be advisory. An unknown or absent name falls back to `default`, so a
 * typo fails closed.
 *
 * Profiles exist so a news card does not inherit a video player's network
 * permissions. Widen the profile a tool needs — never the shared default.
 */
export const CSP_PROFILES: Record<string, (origin: string) => string> = {
  // Everything that is not a video player: no script origins, no network.
  default: () => DEFAULT_EMBED_CSP,

  // Live TV only. hls.js is served from OUR origin (see public/vendor/README.md)
  // rather than a CDN, so script-src stays first-party. connect-src is what
  // hls.js uses to fetch the manifest and segments; media-src blob: is
  // MediaSource. Both are unavoidable for HLS and both are why this is a
  // separate profile.
  video: (origin: string) => [
    "default-src 'none'",
    `script-src 'unsafe-inline' ${origin}`,
    "style-src 'unsafe-inline'",
    'img-src https: data:',
    'media-src blob: https:',
    'connect-src https:',
  ].join('; '),
}

export function cspFor(profile: string | null | undefined): string {
  const build = (profile && CSP_PROFILES[profile]) || CSP_PROFILES.default
  // The frame is opaque-origin, so 'self' means nothing inside it — the app's
  // real origin has to be named explicitly.
  return build(typeof window !== 'undefined' ? window.location.origin : '')
}

/**
 * Prepend a CSP <meta> tag to an HTML document. Ported verbatim from
 * Open WebUI's csp.ts — first CSP meta tag wins per spec.
 */
export function injectCsp(html: string, csp: string): string {
  if (!csp) return html
  const escaped = csp.replace(/"/g, '&quot;')
  const tag = `<meta http-equiv="Content-Security-Policy" content="${escaped}">`
  const idx = html.indexOf('<head>')
  return idx !== -1 ? html.slice(0, idx + 6) + tag + html.slice(idx + 6) : tag + html
}

/**
 * One tool-produced embed.
 *
 * `link` and `video` are rendered by React OUTSIDE the sandboxed iframe.
 * Neither can live inside it:
 *   - a popup opened from a sandboxed frame inherits the sandbox, so the new tab
 *     gets an opaque origin and youtube.com refuses it (ERR_BLOCKED_BY_RESPONSE);
 *   - a nested player frame inherits it too and fails with "writeEmbed is not
 *     defined", rendering black.
 * Both verified in-browser.
 */
export interface SearchResult {
  id: string
  title: string
  channel?: string
  thumb?: string
  duration?: number
}

export interface DirectoryChannel {
  id: string
  name: string
  url: string
  country?: string
  category?: string
  quality?: string
}

export interface NewsArticle {
  title: string
  url: string
  category?: string
}

export interface EmbedItem {
  html: string
  link?: { url: string; label: string } | null
  video?: { id: string; start?: number } | null
  /** Search hits for the picker. Selecting one renders the same player `video` would. */
  results?: SearchResult[] | null
  query?: string | null
  /**
   * Name of the CSP profile this embed needs (see CSP_PROFILES). A NAME only —
   * the policy is defined in this module. Unknown or absent falls back to the
   * strict default.
   */
  csp?: string | null
  /** Directory hits for the multi-select channel picker. Every one shown has
   *  already been checked live server-side. */
  channels?: DirectoryChannel[] | null
  /** Which library the picker commits to: "tv" (default) or "radio". The picker
   *  is shared, so without this a saved radio station would be added as a TV
   *  channel and fail HLS validation. */
  channel_kind?: string | null
  /** News stories, rendered as real links outside the frame. */
  articles?: NewsArticle[] | null
  /**
   * When this widget was rendered. Present ONLY on embeds rehydrated from
   * history — live ones arrive over SSE without it — so it doubles as the flag
   * for "this is a snapshot, not a fresh lookup". Drives the "as of" chip.
   */
  renderedAt?: string | null
  /** HTML exceeded the storage cap, so only the structured half was kept. */
  oversized?: boolean
}

/**
 * YouTube ids are exactly 11 characters. Validating the length is what makes it
 * safe to build a player URL from a value that may still be arriving: a partial
 * id is shorter, fails here, and the fallback renders instead of a frame briefly
 * loading the wrong video. (Borrowed from Hermes, which parses ids out of
 * streaming text; our ids arrive whole inside one SSE payload, so here this is
 * defence-in-depth plus rejection of anything malformed reaching an iframe src.)
 */
const YOUTUBE_ID = /^[A-Za-z0-9_-]{11}$/

export function isValidVideoId(id: unknown): id is string {
  return typeof id === 'string' && YOUTUBE_ID.test(id)
}

/**
 * Privacy-enhanced embed URL. youtube-nocookie.com avoids setting tracking
 * cookies for a viewer who never presses play, which matters because this
 * player renders as soon as the message does rather than on a click.
 */
export function youtubeEmbedUrl(videoId: string, start = 0): string {
  const qs = new URLSearchParams({ rel: '0', modestbranding: '1' })
  if (start > 0) qs.set('start', String(Math.floor(start)))
  return `https://www.youtube-nocookie.com/embed/${videoId}?${qs.toString()}`
}

export interface EmbedOptions {
  /** CSP injected into the document. Defaults to DEFAULT_EMBED_CSP. */
  csp?: string
  /** Grant allow-forms. Off by default. */
  allowForms?: boolean
  /**
   * Grant allow-same-origin. Off by default and should stay that way — see the
   * module docstring. Only for tool HTML you fully trust AND that needs it.
   */
  allowSameOrigin?: boolean
  /**
   * Grant allow-popups. OFF, and it should stay off. A popup opened from a
   * sandboxed frame inherits that sandbox unless allow-popups-to-escape-sandbox
   * is also set, so the opened tab gets an opaque origin and third-party sites
   * refuse it (verified: youtube.com returns ERR_BLOCKED_BY_RESPONSE). The
   * escape flag would fix that by making every embed able to open fully
   * unsandboxed tabs — not a trade worth making. Links belong outside the
   * iframe, in the React layer, where they are ordinary app links.
   */
  allowPopups?: boolean
  /** Grant allow-downloads. Off by default. */
  allowDownloads?: boolean
  /** Upper bound on a self-reported height, in px. */
  maxHeight?: number
  /** Lower bound, so a mis-measured embed can't collapse to nothing. */
  minHeight?: number
  title?: string
  className?: string
}

/**
 * Render one embed HTML string into `container` as a sandboxed iframe.
 * Returns a cleanup function — call it on unmount to drop the message listener.
 */
export function renderEmbed(
  container: HTMLElement,
  html: string,
  options: EmbedOptions = {},
): () => void {
  const {
    csp = DEFAULT_EMBED_CSP,
    allowForms = false,
    allowSameOrigin = false,
    allowPopups = false,
    allowDownloads = false,
    maxHeight = 2000,
    minHeight = 40,
    title = 'Embedded content',
    className = '',
  } = options

  const sandbox = [
    'allow-scripts',
    allowForms && 'allow-forms',
    allowSameOrigin && 'allow-same-origin', // never combine casually with allow-scripts
    allowPopups && 'allow-popups',
    allowDownloads && 'allow-downloads',
  ].filter(Boolean).join(' ')

  const iframe = document.createElement('iframe')
  iframe.setAttribute('sandbox', sandbox)
  iframe.setAttribute('title', title)
  iframe.setAttribute('width', '100%')
  iframe.setAttribute('frameborder', '0')
  // `allow` supersedes the legacy allowfullscreen attribute; setting both makes
  // the browser log a precedence warning, so only the modern one is set.
  iframe.setAttribute('allow', 'fullscreen')
  iframe.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin')
  if (className) iframe.className = className
  iframe.style.cssText = 'width:100%;border:0;display:block;border-radius:12px;'
  iframe.style.height = `${minHeight}px`
  iframe.srcdoc = injectCsp(html, csp)

  function onMessage(e: MessageEvent) {
    // srcdoc iframes report origin "null" — validate by window identity instead.
    if (e.source !== iframe.contentWindow) return
    const data = (e.data ?? {}) as { type?: string; height?: unknown }
    if (data.type === 'iframe:height' && typeof data.height === 'number' &&
        Number.isFinite(data.height) && data.height > 0) {
      // `> 0` is load-bearing, not tidiness. Cards commonly fire once on a
      // short timer before layout has happened, when body.scrollHeight is
      // still 0; clamping that to minHeight collapses the frame, and if the
      // real measurement is then delayed or dropped it stays collapsed. A
      // report of zero is never meaningful, so ignore it and let the current
      // height stand. (Observed: a card posting [0, 332] in that order.)
      iframe.style.height = `${Math.min(Math.max(data.height, minHeight), maxHeight)}px`
    }
  }
  window.addEventListener('message', onMessage)

  container.appendChild(iframe)

  return function cleanup() {
    window.removeEventListener('message', onMessage)
    iframe.remove()
  }
}

/**
 * Render every embed from one tool result into `container`, stacked.
 * Returns a single cleanup for all of them.
 */
export function renderEmbeds(
  container: HTMLElement,
  embeds: string[],
  options: EmbedOptions = {},
): () => void {
  const cleanups = (embeds ?? []).map((html) => renderEmbed(container, html, options))
  return function cleanupAll() {
    cleanups.forEach((fn) => fn())
  }
}
