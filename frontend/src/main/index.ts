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

const isDev = !app.isPackaged
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
    resolve(app.getAppPath(), '..', 'backend'),
    resolve(process.resourcesPath || '', 'backend'),
  ]
  return candidates.find((p) => existsSync(p)) || candidates[0]
}

function findPython(): string {
  if (process.env.PAICC_PYTHON) return process.env.PAICC_PYTHON
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
    // Hide to tray instead of quitting.
    e.preventDefault()
    mainWindow?.hide()
  })
}

function createBallWindow(): void {
  ballWindow = new BrowserWindow({
    width: 340,
    height: 300,
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
  // Bottom-right corner by default.
  const { width, height } = screen.getPrimaryDisplay().workArea
  ballWindow.setPosition(width - 360, height - 320)
}

function loadRenderer(win: BrowserWindow, view: 'main' | 'ball'): void {
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
  ipcMain.handle('paicc:get-backend-url', () => BACKEND_URL)
  ipcMain.handle('paicc:get-view', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender)
    return w === ballWindow ? 'ball' : 'main'
  })
  ipcMain.on('paicc:window-minimize', (e) => BrowserWindow.fromWebContents(e.sender)?.minimize())
  ipcMain.on('paicc:window-hide', (e) => BrowserWindow.fromWebContents(e.sender)?.hide())
  ipcMain.on('paicc:ball-hide', () => ballWindow?.hide())
  ipcMain.on('paicc:show-main', () => mainWindow?.show())
  ipcMain.on('paicc:open-external', (_e, url: string) => {
    if (typeof url === 'string' && /^https?:\/\//.test(url)) shell.openExternal(url)
  })
  ipcMain.on('paicc:open-route', (_e, route: string) => {
    mainWindow?.show()
    if (typeof route === 'string') mainWindow?.webContents.send('paicc:navigate', route)
  })
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------
app.whenReady().then(() => {
  app.setLoginItemSettings({ openAtLogin: process.env.PAICC_OPEN_AT_LOGIN === '1' })
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
