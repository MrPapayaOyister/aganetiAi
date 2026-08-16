/**
 * Purge browser-held state that belongs to a specific user.
 *
 * Conversations are namespaced per user (`aria_convos_${userId}`), so a second
 * person signing in on the same browser never saw the first person's thread list.
 * Several other keys are NOT namespaced, though, and survived both sign-out and
 * user switch:
 *
 *   aria_prefs                — assistant preferences, inherited by the next user
 *   aria_msgs_<sessionId>     — cached message bodies, keyed only by session id
 *   aganeti-dashboard-board   — the last dashboard opened
 *
 * On a shared or handed-over machine that is one person's data rendered under
 * another person's login. The server is authoritative and scopes every response
 * to the caller, so this is a client-side cache problem — but a cache that paints
 * before the first fetch returns is exactly what the user sees.
 *
 * DEVICE settings are deliberately kept: sound and text-to-speech are properties
 * of the machine and speakers, not of the account, and clearing them would
 * silently un-mute a shared room.
 */

/** Keys that are device-level and must survive a user switch. */
const DEVICE_KEYS = new Set(['aria_sound', 'aria_sound_muted', 'aria_tts'])

/** Exact keys that hold user data. */
const USER_KEYS = ['aria_prefs', 'aria_user_id', 'aganeti-dashboard-board']

/** Prefixes whose every key holds user data. */
const USER_PREFIXES = ['aria_convos_', 'aria_active_', 'aria_msgs_']

export function clearUserScopedState(): void {
  try {
    const doomed: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (!key || DEVICE_KEYS.has(key)) continue
      if (USER_KEYS.includes(key) || USER_PREFIXES.some(p => key.startsWith(p))) {
        doomed.push(key)
      }
    }
    // Collected first, removed after: removing during the scan shifts the indices
    // and silently skips every other key.
    doomed.forEach(k => localStorage.removeItem(k))
  } catch {
    /* private mode / storage disabled — nothing cached, nothing to leak */
  }
}

/**
 * Clear only when the signed-in user actually changed.
 *
 * Called on every session restore, including the ordinary page refresh that
 * hands back the SAME user — wiping there would drop that user's own cache on
 * every reload and make the app repaint from empty.
 */
export function resetIfUserChanged(nextUserId: string): boolean {
  let previous: string | null = null
  try {
    previous = localStorage.getItem('aria_user_id')
  } catch {
    return false
  }
  if (!nextUserId || previous === nextUserId) return false
  if (previous) clearUserScopedState()
  return Boolean(previous)
}
