export interface PaiccBridge {
  backendUrl: string
  view: string
  getBackendUrl: () => string
  getView: () => string
  minimize: () => void
  hide: () => void
  hideBall: () => void
  showMain: () => void
  openExternal: (url: string) => void
  openRoute: (route: string) => void
  getPathForFile: (file: any) => string
  ballDragStart: (x: number, y: number) => void
  ballDragMove: (x: number, y: number) => void
  ballDragEnd: () => void
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
