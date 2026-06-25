import { createContext, useContext, useMemo, useRef, useState, useCallback, type ReactNode } from 'react'

export type AgentFieldMode =
  | 'idle'
  | 'listening'
  | 'thinking'
  | 'speaking'
  | 'acting'
  | 'success'
  | 'error'

interface PulseRequest {
  id: number
  nx: number       // normalized 0..1, top-left origin
  ny: number
  intensity: number
}

interface AgentFieldCtx {
  mode: AgentFieldMode
  amplitude: number                                   // 0..1 from voice analyser
  setMode: (m: AgentFieldMode, amplitude?: number) => void
  setAmplitude: (a: number) => void
  /** Send a ripple to PixelBlast. Coords are 0..1, top-left origin. */
  pulse: (nx?: number, ny?: number, intensity?: number) => void
  /** Last pulse request — AgentPixelField subscribes to this. */
  lastPulse: PulseRequest | null
}

const Ctx = createContext<AgentFieldCtx>({
  mode: 'idle', amplitude: 0,
  setMode: () => {}, setAmplitude: () => {},
  pulse: () => {}, lastPulse: null,
})

export function AgentFieldProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<AgentFieldMode>('idle')
  const [amplitude, setAmplitude] = useState(0)
  const [lastPulse, setLastPulse] = useState<PulseRequest | null>(null)
  const pulseIdRef = useRef(0)

  const setMode = useCallback((m: AgentFieldMode, amp?: number) => {
    setModeState(m)
    if (amp != null) setAmplitude(amp)
  }, [])

  const pulse = useCallback((nx = 0.5, ny = 0.6, intensity = 1) => {
    pulseIdRef.current += 1
    setLastPulse({ id: pulseIdRef.current, nx, ny, intensity })
  }, [])

  const value = useMemo(
    () => ({ mode, amplitude, setMode, setAmplitude, pulse, lastPulse }),
    [mode, amplitude, setMode, pulse, lastPulse]
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useAgentField = () => useContext(Ctx)
