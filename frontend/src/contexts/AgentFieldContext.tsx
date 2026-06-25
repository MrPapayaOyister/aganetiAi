import {
  createContext, useContext, useMemo, useRef, useState, useCallback, type ReactNode,
} from 'react'

export type AgentFieldMode =
  | 'idle' | 'listening' | 'thinking' | 'speaking'
  | 'acting' | 'success' | 'error'

interface PulseRequest {
  id: number
  nx: number       // normalized 0..1, top-left origin
  ny: number
  intensity: number
}

interface AgentFieldCtx {
  mode: AgentFieldMode
  setMode: (m: AgentFieldMode, amplitude?: number) => void
  /** Set live amplitude (0..1). Does NOT trigger a re-render — wrapper reads via amplitudeRef. */
  setAmplitude: (a: number) => void
  /** Direct mutable handle so the wrapper can sample at 60fps without re-renders. */
  amplitudeRef: React.MutableRefObject<number>
  /** Send a ripple to PixelBlast. Coords are 0..1, top-left origin. */
  pulse: (nx?: number, ny?: number, intensity?: number) => void
  lastPulse: PulseRequest | null
}

const NULL_REF: React.MutableRefObject<number> = { current: 0 }

const Ctx = createContext<AgentFieldCtx>({
  mode: 'idle', setMode: () => {}, setAmplitude: () => {},
  amplitudeRef: NULL_REF, pulse: () => {}, lastPulse: null,
})

export function AgentFieldProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<AgentFieldMode>('idle')
  const [lastPulse, setLastPulse] = useState<PulseRequest | null>(null)
  const pulseIdRef = useRef(0)
  const amplitudeRef = useRef(0)

  const setMode = useCallback((m: AgentFieldMode, amp?: number) => {
    setModeState(m)
    if (amp != null) amplitudeRef.current = amp
  }, [])

  const setAmplitude = useCallback((a: number) => {
    amplitudeRef.current = a
  }, [])

  const pulse = useCallback((nx = 0.5, ny = 0.6, intensity = 1) => {
    pulseIdRef.current += 1
    setLastPulse({ id: pulseIdRef.current, nx, ny, intensity })
  }, [])

  const value = useMemo(
    () => ({ mode, setMode, setAmplitude, amplitudeRef, pulse, lastPulse }),
    [mode, setMode, setAmplitude, pulse, lastPulse]
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export const useAgentField = () => useContext(Ctx)
