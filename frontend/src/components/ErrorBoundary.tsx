import { Component, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'

interface Props { children: ReactNode }
interface State { error: Error | null }

/**
 * Top-level error boundary. Without this, any uncaught render error produces a
 * blank screen with no clue. This surfaces the message + a reload button.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: unknown) {
    // eslint-disable-next-line no-console
    console.error('[ErrorBoundary]', error, info)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-dvh w-full bg-[#1E2230] flex items-center justify-center p-6">
          <div className="glass-strong rounded-2xl max-w-md w-full p-6 text-center"
               style={{ borderRadius: 18 }}>
            <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-[#FF4466] to-[#7B2FFF]
                            mx-auto mb-4 flex items-center justify-center text-white">
              <AlertTriangle size={22} />
            </div>
            <h1 className="text-lg font-semibold text-[#E2E8F0]">Something went wrong</h1>
            <p className="text-sm text-[#94A3B8] mt-2 break-words">
              {this.state.error.message || 'Unexpected error'}
            </p>
            <button
              onClick={() => window.location.reload()}
              className="mt-5 px-5 py-2.5 rounded-xl bg-[#00D4FF]/15 border border-[#00D4FF]/30
                         text-[#00D4FF] text-sm font-medium hover:bg-[#00D4FF]/25 transition-colors"
            >
              Reload
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
