import { useEffect, useRef } from 'react'

interface WaveformBarProps {
  active: boolean
  analyserNode?: AnalyserNode | null
  barCount?: number
  color?: string
  height?: number
}

export function WaveformBar({
  active,
  analyserNode,
  barCount = 16,
  color = '#00D4FF',
  height = 28,
}: WaveformBarProps) {
  const barsRef = useRef<(HTMLDivElement | null)[]>([])
  const rafRef = useRef<number>(0)
  const dataRef = useRef<number[]>([])

  useEffect(() => {
    if (!active) {
      cancelAnimationFrame(rafRef.current)
      barsRef.current.forEach(b => {
        if (b) b.style.transform = 'scaleY(0.15)'
      })
      return
    }

    if (analyserNode) {
      const buf = new Uint8Array(analyserNode.frequencyBinCount)

      const draw = () => {
        analyserNode.getByteFrequencyData(buf)
        dataRef.current = Array.from(buf)
        const step = Math.floor(dataRef.current!.length / barCount)
        barsRef.current.forEach((b, i) => {
          if (!b) return
          const val = dataRef.current![i * step] / 255
          b.style.transform = `scaleY(${Math.max(0.1, val)})`
        })
        rafRef.current = requestAnimationFrame(draw)
      }
      draw()
    } else {
      // CSS stagger animation when no analyser data
      barsRef.current.forEach((b, i) => {
        if (b) {
          b.style.animationDelay = `${(i / barCount) * 0.6}s`
          b.className = 'wave-bar rounded-full'
        }
      })
    }

    return () => cancelAnimationFrame(rafRef.current)
  }, [active, analyserNode, barCount])

  return (
    <div className="flex items-end gap-[2px]" style={{ height }}>
      {Array.from({ length: barCount }, (_, i) => (
        <div
          key={i}
          ref={el => { barsRef.current[i] = el }}
          className="rounded-full transition-transform"
          style={{
            width: 2,
            height: height,
            background: color,
            opacity: active ? 0.85 : 0.3,
            transform: 'scaleY(0.15)',
            transformOrigin: 'center bottom',
            transition: analyserNode ? 'none' : 'transform 0.1s ease',
            animationDelay: active && !analyserNode ? `${(i / barCount) * 0.6}s` : undefined,
          }}
        />
      ))}
    </div>
  )
}
