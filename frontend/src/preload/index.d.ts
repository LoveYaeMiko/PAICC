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
