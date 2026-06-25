import { createContext, useContext, useState, type ReactNode } from 'react'

export type BgIntensity = 'off' | 'calm' | 'standard' | 'cinematic'

export interface Prefs {
  agentName: string
  displayName: string
  onboardingDone: boolean
  bgIntensity: BgIntensity
}

const DEFAULT_PREFS: Prefs = {
  agentName: 'Aria',
  displayName: '',
  onboardingDone: false,
  bgIntensity: 'standard',
}

function loadPrefs(): Prefs {
  try {
    const stored = localStorage.getItem('aria_prefs')
    if (stored) return { ...DEFAULT_PREFS, ...JSON.parse(stored) }
    if (localStorage.getItem('aria_user_id')) {
      return { ...DEFAULT_PREFS, onboardingDone: true }
    }
  } catch { /* ignore */ }
  return DEFAULT_PREFS
}

interface PrefsCtx {
  prefs: Prefs
  setPrefs: (p: Partial<Prefs>) => void
}

const PrefsContext = createContext<PrefsCtx>({ prefs: DEFAULT_PREFS, setPrefs: () => {} })

export function PrefsProvider({ children }: { children: ReactNode }) {
  const [prefs, setPrefsState] = useState<Prefs>(loadPrefs)

  const setPrefs = (p: Partial<Prefs>) => {
    const next = { ...prefs, ...p }
    setPrefsState(next)
    try { localStorage.setItem('aria_prefs', JSON.stringify(next)) } catch { /* ignore */ }
  }

  return <PrefsContext.Provider value={{ prefs, setPrefs }}>{children}</PrefsContext.Provider>
}

export const usePrefs = () => useContext(PrefsContext)
