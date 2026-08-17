export interface BallState {
  docked: boolean
  edge: 'left' | 'right' | 'top' | 'bottom' | null
  ball: { x: number; y: number }
  workArea: { x: number; y: number; width: number; height: number }
}

export interface PaiccBridge {
  backendUrl: string
  view: string
  getBackendUrl: () => string
  getView: () => string
  minimize: () => void
  hide: () => void
  hideBall: () => void
  showMain: () => void
  quitApp: () => void
  openExternal: (url: string) => void
  openRoute: (route: string) => void
  getPathForFile: (file: any) => string
  ballDragStart: (x: number, y: number) => void
  ballDragMove: (x: number, y: number) => void
  ballDragEnd: () => void
  setBallMouseIgnore: (ignore: boolean) => void
  getBallState: () => Promise<BallState>
  onBallState: (cb: (s: BallState) => void) => void
  getAutoLaunch: () => Promise<boolean>
  setAutoLaunch: (v: boolean) => Promise<boolean>
  onNavigate: (cb: (route: string) => void) => void
  onBackendStatus: (cb: (status: { ready: boolean; url: string }) => void) => void
  onToggleInput: (cb: () => void) => void
}

declare global {
  interface Window {
    paicc?: PaiccBridge
  }
}

export {}
