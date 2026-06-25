import { createContext, useContext, useState, useCallback, type ReactNode } from 'react'
import type { ParticleMode } from '../components/ParticleCanvas'

interface AmbientCtx {
  mode: ParticleMode
  amplitude: number
  setAmbient: (mode: ParticleMode, amplitude?: number) => void
}

const Ctx = createContext<AmbientCtx>({ mode: 'idle', amplitude: 0, setAmbient: () => {} })

export function AmbientProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ParticleMode>('idle')
  const [amplitude, setAmplitude] = useState(0)

  const setAmbient = useCallback((m: ParticleMode, amp = 0) => {
    setMode(m)
    setAmplitude(amp)
  }, [])

  return <Ctx.Provider value={{ mode, amplitude, setAmbient }}>{children}</Ctx.Provider>
}

export const useAmbient = () => useContext(Ctx)
