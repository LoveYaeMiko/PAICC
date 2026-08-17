import { contextBridge, ipcRenderer, webUtils } from 'electron'

export interface BallState {
  docked: boolean
  edge: 'left' | 'right' | 'top' | 'bottom' | null
  ball: { x: number; y: number }
  workArea: { x: number; y: number; width: number; height: number }
}

const backendUrl = (ipcRenderer.sendSync('paicc:get-backend-url') as string) || 'http://127.0.0.1:8000'
const view = (ipcRenderer.sendSync('paicc:get-view') as string) || 'main'

const api = {
  backendUrl,
  view,
  getBackendUrl: (): string => backendUrl,
  getView: (): string => view,
  // window controls
  minimize: (): void => ipcRenderer.send('paicc:window-minimize'),
  hide: (): void => ipcRenderer.send('paicc:window-hide'),
  hideBall: (): void => ipcRenderer.send('paicc:ball-hide'),
  showMain: (): void => ipcRenderer.send('paicc:show-main'),
  quitApp: (): void => ipcRenderer.send('paicc:app-quit'),
  openExternal: (url: string): void => ipcRenderer.send('paicc:open-external', url),
  openRoute: (route: string): void => ipcRenderer.send('paicc:open-route', route),
  // Resolve an OS-dropped File to its absolute path (File.path is deprecated in Electron 32+).
  getPathForFile: (file: any): string => webUtils.getPathForFile(file),
  // Manual frameless-window dragging (the ball is a drop target, so it can't use app-region: drag).
  ballDragStart: (x: number, y: number): void => ipcRenderer.send('paicc:ball-drag-start', x, y),
  ballDragMove: (x: number, y: number): void => ipcRenderer.send('paicc:ball-drag-move', x, y),
  ballDragEnd: (): void => ipcRenderer.send('paicc:ball-drag-end'),
  setBallMouseIgnore: (ignore: boolean): void => ipcRenderer.send('paicc:set-ball-mouse-ignore', ignore),
  getBallState: (): Promise<BallState> => ipcRenderer.invoke('paicc:get-ball-state'),
  onBallState: (cb: (s: BallState) => void): void => {
    ipcRenderer.on('ball-state', (_e, s) => cb(s))
  },
  getAutoLaunch: (): Promise<boolean> => ipcRenderer.invoke('paicc:get-auto-launch'),
  setAutoLaunch: (v: boolean): Promise<boolean> => ipcRenderer.invoke('paicc:set-auto-launch', v),
  onNavigate: (cb: (route: string) => void): void => {
    ipcRenderer.on('paicc:navigate', (_e, route) => cb(route))
  },
  // events from main -> renderer
  onBackendStatus: (cb: (status: { ready: boolean; url: string }) => void): void => {
    ipcRenderer.on('backend-status', (_e, status) => cb(status))
  },
  onToggleInput: (cb: () => void): void => {
    ipcRenderer.on('toggle-input', () => cb())
  },
}

contextBridge.exposeInMainWorld('paicc', api)

export type PaiccBridge = typeof api
