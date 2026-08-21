import { app, BrowserWindow, globalShortcut, ipcMain, Menu, nativeImage, screen, shell, Tray } from 'electron'
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { deflateSync } from 'node:zlib'

const BACKEND_PORT = Number(process.env.PAICC_BACKEND_PORT || 8000)
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}`

let mainWindow: BrowserWindow | null = null
let ballWindow: BrowserWindow | null = null
let tray: Tray | null = null
let backendProc: ChildProcess | null = null
let backendReady = false
let isQuitting = false
let ballDragCursor: { x: number; y: number } | null = null
let ballDragPos: number[] | null = null

// Floating-ball geometry — MUST match the renderer constants in FloatingBall.tsx.
const BALL_W = 360
const BALL_H = 512
const BALL_CX = 180 // ball centre x within the window
const BALL_CY = 256 // ball centre y within the window (vertically centred)
const BALL_STRIP = 14 // visible strip thickness when docked against an edge
const BALL_SAFE = 40 // min distance the ball centre keeps from every edge when free
const BALL_SNAP = 50 // drag within this of an edge → dock to a strip
const BALL_TWEEN_MS = 200 // smooth slide duration for dock/undock
let ballDocked = false
let ballDockEdge: 'left' | 'right' | 'top' | 'bottom' | null = null
let ballTween: ReturnType<typeof setInterval> | null = null

const rendererUrl = process.env['ELECTRON_RENDERER_URL']

// ---------------------------------------------------------------------------
// Tiny PNG encoder (no external deps) — used for the tray icon.
// ---------------------------------------------------------------------------
const PNG_SIG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
const CRC_TABLE = (() => {
  const t = new Int32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c
  }
  return t
})()

function crc32(buf: Buffer): number {
  let c = 0xffffffff
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function pngChunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length, 0)
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body), 0)
  return Buffer.concat([len, body, crc])
}

function makeCirclePng(size = 16, color: [number, number, number] = [74, 144, 226]): Buffer {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(size, 0)
  ihdr.writeUInt32BE(size, 4)
  ihdr[8] = 8 // bit depth
  ihdr[9] = 6 // color type RGBA
  const stride = size * 4 + 1
  const raw = Buffer.alloc(stride * size)
  const cx = (size - 1) / 2
  const r = size / 2 - 1
  for (let y = 0; y < size; y++) {
    raw[y * stride] = 0 // filter byte
    for (let x = 0; x < size; x++) {
      const dx = x - cx
      const dy = y - cx
      const inside = dx * dx + dy * dy <= r * r
      const o = y * stride + 1 + x * 4
      if (inside) {
        raw[o] = color[0]
        raw[o + 1] = color[1]
        raw[o + 2] = color[2]
        raw[o + 3] = 255
      } else {
        raw[o] = 0
        raw[o + 1] = 0
        raw[o + 2] = 0
        raw[o + 3] = 0
      }
    }
  }
  return Buffer.concat([PNG_SIG, pngChunk('IHDR', ihdr), pngChunk('IDAT', deflateSync(raw)), pngChunk('IEND', Buffer.alloc(0))])
}

// ---------------------------------------------------------------------------
// Python backend process management
// ---------------------------------------------------------------------------
function findBackendDir(): string {
  const candidates = [
    process.env.PAICC_BACKEND_DIR,
    resolve(app.getAppPath(), '..', 'backend'),
    resolve(process.resourcesPath || '', 'backend'),
    // Packaged builds don't sit next to a backend checkout, so fall back to the
    // user's PAICC project directory (where the backend + venv live).
    resolve(app.getPath('home'), 'Desktop', 'PAICC', 'backend'),
  ].filter((p): p is string => Boolean(p))
  return candidates.find((p) => existsSync(join(p, 'run.py'))) || candidates[0]
}

function findPython(): string {
  if (process.env.PAICC_PYTHON) return process.env.PAICC_PYTHON
  // Prefer the project's bundled virtualenv so all backend dependencies
  // (apscheduler, etc.) resolve regardless of what the system Python has.
  const backendDir = findBackendDir()
  const venvPython =
    process.platform === 'win32'
      ? join(backendDir, '.venv', 'Scripts', 'python.exe')
      : join(backendDir, '.venv', 'bin', 'python')
  if (existsSync(venvPython)) return venvPython
  return process.platform === 'win32' ? 'python' : 'python3'
}

function startBackend(): void {
  if (process.env.PAICC_MANAGE_BACKEND === '0') return
  const cwd = findBackendDir()
  const python = findPython()
  if (!existsSync(join(cwd, 'run.py'))) {
    console.warn('[paicc] backend run.py not found at', cwd, '— assuming external backend')
    return
  }
  try {
    backendProc = spawn(python, ['run.py'], {
      cwd,
      windowsHide: true,
      env: { ...process.env, PAICC_BACKEND_PORT: String(BACKEND_PORT) },
      stdio: 'ignore',
    })
    backendProc.on('exit', (code) => {
      backendReady = false
      if (!isQuitting) console.warn(`[paicc] backend exited (code ${code})`)
    })
  } catch (err) {
    console.error('[paicc] failed to start backend', err)
  }
  pollBackendHealth()
}

async function pollBackendHealth(): Promise<void> {
  const check = async (): Promise<boolean> => {
    try {
      const res = await fetch(`${BACKEND_URL}/api/health`)
      return res.ok
    } catch {
      return false
    }
  }
  const interval = setInterval(async () => {
    const ok = await check()
    if (ok && !backendReady) {
      backendReady = true
      broadcast('backend-status', { ready: true, url: BACKEND_URL })
    } else if (!ok && backendReady) {
      backendReady = false
      broadcast('backend-status', { ready: false, url: BACKEND_URL })
    }
  }, 2000)
  interval.unref()
}

function broadcast(channel: string, payload: unknown): void {
  for (const w of [mainWindow, ballWindow]) {
    if (w && !w.isDestroyed()) w.webContents.send(channel, payload)
  }
}

// ---------------------------------------------------------------------------
// Floating-ball edge docking (collapse to a strip, expand on hover)
// ---------------------------------------------------------------------------
function ballScreenPos(): { x: number; y: number } {
  if (!ballWindow) return { x: BALL_CX, y: BALL_CY }
  const [x, y] = ballWindow.getPosition()
  return { x: x + BALL_CX, y: y + BALL_CY }
}

function setBallScreenPos(x: number, y: number): void {
  if (!ballWindow) return
  ballWindow.setPosition(Math.round(x - BALL_CX), Math.round(y - BALL_CY))
}

// Smoothly slide the ball window to a target screen position (ease-in-out quad).
function tweenBallToScreen(cx: number, cy: number, onDone?: () => void): void {
  tweenBallTo(cx - BALL_CX, cy - BALL_CY, onDone)
}

function tweenBallTo(x: number, y: number, onDone?: () => void): void {
  if (!ballWindow) {
    onDone?.()
    return
  }
  if (ballTween) clearInterval(ballTween)
  const from = ballWindow.getPosition()
  const start = Date.now()
  const ease = (t: number): number => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2)
  ballTween = setInterval(() => {
    const t = Math.min(1, (Date.now() - start) / BALL_TWEEN_MS)
    const nx = Math.round(from[0] + (x - from[0]) * ease(t))
    const ny = Math.round(from[1] + (y - from[1]) * ease(t))
    if (ballWindow && !ballWindow.isDestroyed()) ballWindow.setPosition(nx, ny)
    if (t >= 1) {
      if (ballTween) clearInterval(ballTween)
      ballTween = null
      onDone?.()
    }
  }, 16)
}

function workAreaAt(x: number, y: number): Electron.Rectangle {
  return screen.getDisplayMatching({ x: Math.round(x), y: Math.round(y), width: 1, height: 1 })
    .workArea
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(Math.max(v, lo), hi)
}

// Safe bounds for a free ball: keeps the ball itself fully on-screen.
function safeBounds(wa: Electron.Rectangle): { minX: number; maxX: number; minY: number; maxY: number } {
  const cx = wa.x + wa.width / 2
  const cy = wa.y + wa.height / 2
  return {
    minX: Math.min(wa.x + BALL_SAFE, cx),
    maxX: Math.max(wa.x + wa.width - BALL_SAFE, cx),
    minY: Math.min(wa.y + BALL_SAFE, cy),
    maxY: Math.max(wa.y + wa.height - BALL_SAFE, cy),
  }
}

function currentBallState(): {
  docked: boolean
  edge: 'left' | 'right' | 'top' | 'bottom' | null
  ball: { x: number; y: number }
  workArea: { x: number; y: number; width: number; height: number }
} {
  const ball = ballScreenPos()
  const wa = workAreaAt(ball.x, ball.y)
  return {
    docked: ballDocked,
    edge: ballDockEdge,
    ball,
    workArea: { x: wa.x, y: wa.y, width: wa.width, height: wa.height },
  }
}

function broadcastBallState(): void {
  if (ballWindow && !ballWindow.isDestroyed()) {
    ballWindow.webContents.send('ball-state', currentBallState())
  }
}

// Dock the ball so only a thin strip peeks out at the given screen edge. The strip
// *is* the collapsed ball — it stays at the edge; the satellite menu orbits it.
function dockBall(edge: 'left' | 'right' | 'top' | 'bottom'): void {
  if (!ballWindow) return
  const ball = ballScreenPos()
  const wa = workAreaAt(ball.x, ball.y)
  const { minX, maxX, minY, maxY } = safeBounds(wa)
  let cx = ball.x
  let cy = ball.y
  if (edge === 'left') {
    cx = wa.x + BALL_STRIP / 2
    cy = clamp(ball.y, minY, maxY)
  } else if (edge === 'right') {
    cx = wa.x + wa.width - BALL_STRIP / 2
    cy = clamp(ball.y, minY, maxY)
  } else if (edge === 'top') {
    cy = wa.y + BALL_STRIP / 2
    cx = clamp(ball.x, minX, maxX)
  } else {
    cy = wa.y + wa.height - BALL_STRIP / 2
    cx = clamp(ball.x, minX, maxX)
  }
  ballDocked = true
  ballDockEdge = edge
  broadcastBallState()
  tweenBallToScreen(cx, cy, () => broadcastBallState())
}

// Restore the ball from a docked strip back to a free position inside the screen.
function undockBall(): void {
  const ball = ballScreenPos()
  const wa = workAreaAt(ball.x, ball.y)
  const { minX, maxX, minY, maxY } = safeBounds(wa)
  const cx = clamp(ball.x, minX, maxX)
  const cy = clamp(ball.y, minY, maxY)
  ballDocked = false
  ballDockEdge = null
  broadcastBallState()
  tweenBallToScreen(cx, cy, () => broadcastBallState())
}

// After a drag: dock to the nearest edge if close enough, otherwise keep it free
// (clamped on-screen). Dragging a docked strip inward past the snap threshold
// restores the ball.
function snapBallAfterDrag(): void {
  const ball = ballScreenPos()
  const wa = workAreaAt(ball.x, ball.y)
  const dl = ball.x - wa.x
  const dr = wa.x + wa.width - ball.x
  const dt = ball.y - wa.y
  const db = wa.y + wa.height - ball.y
  const min = Math.min(dl, dr, dt, db)
  if (min < BALL_SNAP) {
    const edge: 'left' | 'right' | 'top' | 'bottom' =
      dl === min ? 'left' : dr === min ? 'right' : dt === min ? 'top' : 'bottom'
    dockBall(edge)
  } else if (ballDocked) {
    undockBall()
  } else {
    const { minX, maxX, minY, maxY } = safeBounds(wa)
    ballDocked = false
    ballDockEdge = null
    broadcastBallState()
    tweenBallToScreen(clamp(ball.x, minX, maxX), clamp(ball.y, minY, maxY))
  }
}

// ---------------------------------------------------------------------------
// Windows
// ---------------------------------------------------------------------------
function createMainWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 820,
    minWidth: 980,
    minHeight: 640,
    title: 'Personal AI Command Center',
    show: false,
    autoHideMenuBar: true,
    backgroundColor: '#0f1115',
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true,
    },
  })
  loadRenderer(mainWindow, 'main')
  mainWindow.once('ready-to-show', () => mainWindow?.show())
  mainWindow.on('close', (e) => {
    // Hide to tray instead of quitting — unless we are actually quitting.
    if (!isQuitting) {
      e.preventDefault()
      mainWindow?.hide()
    }
  })
}

function createBallWindow(): void {
  ballWindow = new BrowserWindow({
    width: BALL_W,
    height: BALL_H,
    transparent: true,
    frame: false,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    webPreferences: {
      preload: join(__dirname, '../preload/index.js'),
      sandbox: false,
      contextIsolation: true,
    },
  })
  ballWindow.setAlwaysOnTop(true, 'screen-saver')
  loadRenderer(ballWindow, 'ball')
  // Bottom-right corner by default, kept inside the safe region.
  const wa = screen.getPrimaryDisplay().workArea
  setBallScreenPos(wa.x + wa.width - 200, wa.y + wa.height - 150)
  broadcastBallState()
}

function loadRenderer(win: BrowserWindow, view: 'main' | 'ball'): void {
  // Prevent OS file-drops (or any cross-document navigation) from navigating the
  // window away from the app UI.
  win.webContents.on('will-navigate', (e) => e.preventDefault())
  const query = `view=${view}`
  if (rendererUrl) {
    win.loadURL(`${rendererUrl}?${query}`)
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'), { query: { view } })
  }
}

function createTray(): void {
  try {
    const icon = nativeImage.createFromBuffer(makeCirclePng(16))
    tray = new Tray(icon)
    tray.setToolTip('Personal AI Command Center')
    tray.setContextMenu(
      Menu.buildFromTemplate([
        { label: '显示主界面', click: () => mainWindow?.show() },
        { label: '显示悬浮球', click: () => ballWindow?.show() },
        { type: 'separator' },
        {
          label: '后端状态',
          enabled: false,
        },
        { type: 'separator' },
        { label: '退出', click: () => app.quit() },
      ]),
    )
    tray.on('double-click', () => mainWindow?.show())
  } catch (err) {
    console.warn('[paicc] tray unavailable', err)
  }
}

// ---------------------------------------------------------------------------
// IPC
// ---------------------------------------------------------------------------
function registerIpc(): void {
  // The preload reads these two synchronously via `sendSync`, so they must be
  // `ipcMain.on` (which sets `event.returnValue`) — `ipcMain.handle` only serves `invoke`.
  ipcMain.on('paicc:get-backend-url', (e) => {
    e.returnValue = BACKEND_URL
  })
  ipcMain.on('paicc:get-view', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender)
    e.returnValue = w === ballWindow ? 'ball' : 'main'
  })
  // Manual frameless-window dragging for the ball (it is a drop target, so it
  // cannot use `-webkit-app-region: drag`).
  ipcMain.on('paicc:ball-drag-start', (_e, x: number, y: number) => {
    if (!ballWindow) return
    if (ballTween) {
      clearInterval(ballTween)
      ballTween = null
    }
    ballDragCursor = { x, y }
    ballDragPos = ballWindow.getPosition()
  })
  ipcMain.on('paicc:ball-drag-move', (_e, x: number, y: number) => {
    if (!ballWindow || !ballDragCursor || !ballDragPos) return
    const dx = x - ballDragCursor.x
    const dy = y - ballDragCursor.y
    ballWindow.setPosition(ballDragPos[0] + dx, ballDragPos[1] + dy)
  })
  ipcMain.on('paicc:ball-drag-end', () => {
    ballDragCursor = null
    ballDragPos = null
    snapBallAfterDrag()
  })
  ipcMain.on('paicc:set-ball-mouse-ignore', (_e, ignore: boolean) => {
    ballWindow?.setIgnoreMouseEvents(Boolean(ignore), { forward: true })
  })
  ipcMain.handle('paicc:get-ball-state', () => currentBallState())
  ipcMain.on('paicc:window-minimize', (e) => BrowserWindow.fromWebContents(e.sender)?.minimize())
  ipcMain.on('paicc:window-hide', (e) => BrowserWindow.fromWebContents(e.sender)?.hide())
  ipcMain.on('paicc:ball-hide', () => ballWindow?.hide())
  ipcMain.on('paicc:show-main', () => mainWindow?.show())
  ipcMain.on('paicc:app-quit', () => app.quit())
  ipcMain.on('paicc:open-external', (_e, url: string) => {
    if (typeof url === 'string' && /^https?:\/\//.test(url)) shell.openExternal(url)
  })
  ipcMain.on('paicc:open-route', (_e, route: string) => {
    mainWindow?.show()
    if (typeof route === 'string') mainWindow?.webContents.send('paicc:navigate', route)
  })
  // In dev the login item is the bare Electron binary, which carries no app path:
  // on boot Windows would launch Electron's default "run a local app" screen. So we
  // point it at the project root — package.json's "main" resolves to out/main/index.js
  // and out/renderer/index.html serves the UI (no dev server needed). Packaged builds
  // need no such fix.
  const devLoginItemOpts = (): { path?: string; args?: string[] } =>
    app.isPackaged ? {} : { path: process.execPath, args: [app.getAppPath()] }

  ipcMain.handle('paicc:get-auto-launch', () =>
    app.getLoginItemSettings(devLoginItemOpts()).openAtLogin,
  )
  ipcMain.handle('paicc:set-auto-launch', (_e, openAtLogin: boolean) => {
    app.setLoginItemSettings({ openAtLogin: Boolean(openAtLogin), ...devLoginItemOpts() })
    return app.getLoginItemSettings(devLoginItemOpts()).openAtLogin
  })
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
app.whenReady().then(() => {
  registerIpc()
  createMainWindow()
  createBallWindow()
  createTray()
  startBackend()

  globalShortcut.register('Alt+Space', () => {
    const w = mainWindow?.isVisible() ? mainWindow : ballWindow
    w?.webContents.send('toggle-input')
    if (w && !w.isVisible()) w.show()
  })

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createMainWindow()
    mainWindow?.show()
  })
})

app.on('will-quit', () => {
  globalShortcut.unregisterAll()
})

app.on('before-quit', () => {
  isQuitting = true
  if (backendProc) {
    try {
      backendProc.kill()
    } catch {
      /* noop */
    }
  }
})

app.on('window-all-closed', () => {
  // Keep running in the tray; only quit explicitly.
})
