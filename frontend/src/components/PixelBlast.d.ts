import * as React from 'react'

export interface PixelBlastHandle {
  triggerRipple: (nx?: number, ny?: number) => void
  setColor: (hex: string) => void
  setSpeed: (v: number) => void
}

export interface PixelBlastProps {
  variant?: 'square' | 'circle' | 'triangle' | 'diamond'
  pixelSize?: number
  color?: string
  className?: string
  style?: React.CSSProperties
  antialias?: boolean
  patternScale?: number
  patternDensity?: number
  liquid?: boolean
  liquidStrength?: number
  liquidRadius?: number
  pixelSizeJitter?: number
  enableRipples?: boolean
  rippleIntensityScale?: number
  rippleThickness?: number
  rippleSpeed?: number
  liquidWobbleSpeed?: number
  autoPauseOffscreen?: boolean
  speed?: number
  transparent?: boolean
  edgeFade?: number
  noiseAmount?: number
  interactive?: boolean
}

declare const PixelBlast: React.ForwardRefExoticComponent<
  PixelBlastProps & React.RefAttributes<PixelBlastHandle>
>

export default PixelBlast
