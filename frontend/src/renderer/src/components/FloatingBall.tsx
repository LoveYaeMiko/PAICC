import {
  AppstoreOutlined,
  BookOutlined,
  CloseOutlined,
  CommentOutlined,
  DashboardOutlined,
  FileSearchOutlined,
  LineChartOutlined,
  PieChartOutlined,
  PoweroffOutlined,
  SendOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { useCallback, useEffect, useRef, useState, type DragEvent, type MouseEvent as ReactMouseEvent } from 'react'
import { api, BACKEND_URL } from '@/services/api'
import Markdown from '@/components/Markdown'
import type { AppEntry } from '@/types'

function AppIcon({ app, size }: { app: AppEntry; size: number }): JSX.Element {
  const [failed, setFailed] = useState(false)
  if (failed) {
    return (
      <span style={{ fontSize: 12, color: 'var(--paicc-text)' }}>{app.name.slice(0, 1)}</span>
    )
  }
  return (
    <img
      src={`${BACKEND_URL}/api/apps/icon/${app.id}`}
      alt=""
      width={size}
      height={size}
      style={{ width: size, height: size, borderRadius: 4, objectFit: 'contain' }}
      onError={() => setFailed(true)}
    />
  )
}

const BALL_SIZE = 64
const ORBIT_X = 180 // orbit centre x within the window (must match main-process BALL_CX)
const ORBIT_Y = 256 // orbit centre y within the window (must match main-process BALL_CY)
const RADIUS = 118 // outer ring radius (item centre distance)
const INNER_R = 0.55 * RADIUS
const ITEM_HALF = 22 // half of a 44px menu item, used for the on-screen margin
const INTERACT_RADIUS = 40 // interactive radius around the ball/strip when idle
const PANEL_RADIUS = RADIUS + ITEM_HALF + 20 // radius within which the open panel stays alive

const MENU = [
  { key: '/', icon: <DashboardOutlined />, label: '系统' },
  { key: '/files', icon: <FileSearchOutlined />, label: '文件' },
  { key: '/apps', icon: <AppstoreOutlined />, label: '应用' },
  { key: '/storage', icon: <PieChartOutlined />, label: '存储' },
  { key: '/quant', icon: <LineChartOutlined />, label: '量化' },
  { key: '/research', icon: <BookOutlined />, label: '研究' },
  { key: '/settings', icon: <SettingOutlined />, label: '设置' },
]

// Place an item at radius r and angle deg (0 = right, counter-clockwise) around the
// orbit centre, keeping the item upright so its icon isn't rotated.
function orbitTransform(deg: number, r: number): string {
  return `rotate(${deg}deg) translate(${r}px) rotate(${-deg}deg)`
}

interface BallState {
  docked: boolean
  edge: 'left' | 'right' | 'top' | 'bottom' | null
  ball: { x: number; y: number }
  workArea: { x: number; y: number; width: number; height: number }
}

// Largest contiguous arc (in degrees) around the orbit centre where a menu item of
// radius r stays fully inside the work area. full=true means the whole circle fits.
function largestOnScreenArc(
  ball: BallState['ball'],
  wa: BallState['workArea'],
  r: number,
): { center: number; half: number; full: boolean } {
  const ok: boolean[] = new Array(360).fill(false)
  let allOk = true
  for (let d = 0; d < 360; d++) {
    const rad = (d * Math.PI) / 180
    const px = ball.x + r * Math.cos(rad)
    const py = ball.y - r * Math.sin(rad)
    const good =
      px >= wa.x + ITEM_HALF &&
      px <= wa.x + wa.width - ITEM_HALF &&
      py >= wa.y + ITEM_HALF &&
      py <= wa.y + wa.height - ITEM_HALF
    ok[d] = good
    if (!good) allOk = false
  }
  if (allOk) return { center: 90, half: 180, full: true }
  // Longest contiguous run (with wrap-around) is the on-screen arc.
  let bestLen = 0
  let bestStart = 0
  let len = 0
  let start = 0
  for (let i = 0; i < 720; i++) {
    if (ok[i % 360]) {
      if (len === 0) start = i
      len++
      if (len > bestLen) {
        bestLen = len
        bestStart = start % 360
      }
    } else {
      len = 0
    }
  }
  return { center: (bestStart + bestLen / 2) % 360, half: bestLen / 2, full: false }
}

// Spread n items evenly across the safe arc (or around the whole circle if full).
function itemAngles(n: number, arc: { center: number; half: number; full: boolean }): number[] {
  if (n <= 0) return []
  if (arc.full) return Array.from({ length: n }, (_, i) => (i * 360) / n)
  if (n === 1) return [arc.center]
  const span = arc.half * 2
  return Array.from({ length: n }, (_, i) => arc.center - arc.half + (span * i) / (n - 1))
}

export default function FloatingBall(): JSX.Element {
  const [menuOpen, setMenuOpen] = useState(false)
  const [inputOpen, setInputOpen] = useState(false)
  const [text, setText] = useState('')
  const [history, setHistory] = useState<Array<{ role: 'user' | 'assistant'; content: string }>>([])
  const [loading, setLoading] = useState(false)
  const [favorites, setFavorites] = useState<AppEntry[]>([])
  const [dragging, setDragging] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [ballState, setBallState] = useState<BallState | null>(null)
  const hoverTimer = useRef<number | null>(null)
  const dragResetTimer = useRef<number | null>(null)
  const noticeTimer = useRef<number | null>(null)
  const didDrag = useRef(false)
  const interactiveRef = useRef(false)
  const ballDraggingRef = useRef(false)

  const docked = ballState?.docked ?? false
  const edge = ballState?.edge ?? null
  const arc = ballState
    ? largestOnScreenArc(ballState.ball, ballState.workArea, RADIUS)
    : { center: 90, half: 180, full: true }
  const menuAngles = itemAngles(MENU.length, arc)
  const favAngles = itemAngles(Math.min(favorites.length, 6), arc)

  const showNotice = useCallback((msg: string): void => {
    setNotice(msg)
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current)
    noticeTimer.current = window.setTimeout(() => setNotice(null), 2500)
  }, [])

  const loadFavorites = useCallback((): void => {
    api
      .get('/apps/list', { params: { favorites_only: 1 } })
      .then((r) => setFavorites(r.data))
      .catch(() => setFavorites([]))
  }, [])

  useEffect(() => {
    loadFavorites()
  }, [loadFavorites])

  // Pull the initial dock state, then track changes pushed from the main process.
  useEffect(() => {
    window.paicc
      ?.getBallState()
      .then((s) => setBallState(s))
      .catch(() => {})
    window.paicc?.onBallState((s) => setBallState(s))
  }, [])

  // Docking hides the ball and any open panel, and re-enables click-through so the
  // transparent on-screen part of the window doesn't block clicks behind it.
  useEffect(() => {
    if (docked) {
      setMenuOpen(false)
      setInputOpen(false)
      interactiveRef.current = false
      window.paicc?.setBallMouseIgnore(true)
    }
  }, [docked])

  // Start click-through; updatePointer re-enables interactivity over content.
  useEffect(() => {
    interactiveRef.current = false
    window.paicc?.setBallMouseIgnore(true)
  }, [])

  // Alt+Space global shortcut toggles the quick-input panel (free state only).
  useEffect(() => {
    window.paicc?.onToggleInput(() => {
      setMenuOpen(false)
      setInputOpen((v) => !v)
    })
  }, [])

  // Track the cursor distance from the orbit centre (a fixed point in the window):
  // keep the window interactive only over real content, and close the panel once the
  // cursor strays past its radius. This also stops the transparent window from
  // swallowing clicks over empty space.
  const updatePointer = (x: number, y: number): void => {
    if (ballDraggingRef.current) return
    const dx = x - ORBIT_X
    const dy = y - ORBIT_Y
    const dist = Math.sqrt(dx * dx + dy * dy)
    let wantInteractive: boolean
    if (inputOpen) {
      wantInteractive = true
    } else if (menuOpen) {
      wantInteractive = dist <= PANEL_RADIUS
      if (dist > PANEL_RADIUS) setMenuOpen(false)
    } else {
      wantInteractive = dist <= INTERACT_RADIUS
    }
    if (wantInteractive !== interactiveRef.current) {
      interactiveRef.current = wantInteractive
      window.paicc?.setBallMouseIgnore(!wantInteractive)
    }
  }

  const onAnchorEnter = (): void => {
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current)
    hoverTimer.current = window.setTimeout(() => setMenuOpen(true), 300)
  }

  const onAnchorLeave = (): void => {
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current)
  }

  const onWinLeave = (): void => {
    setMenuOpen(false)
    interactiveRef.current = false
    window.paicc?.setBallMouseIgnore(true)
  }

  const send = async (): Promise<void> => {
    const t = text.trim()
    if (!t || loading) return
    setText('')
    setLoading(true)
    const next = [...history, { role: 'user' as const, content: t }]
    setHistory(next)
    try {
      const { data } = await api.post(
        '/ai/chat',
        {
          messages: next.map((m) => ({ role: m.role, content: m.content })),
          use_tools: true,
        },
        { timeout: 120000 },
      )
      const content = data.needs_confirmation
        ? `⚠️ 需要确认：${data.confirmation?.title ?? ''}`
        : data.content
      setHistory([...next, { role: 'assistant', content }])
    } catch {
      setHistory([...next, { role: 'assistant', content: '请求失败，请检查后端连接。' }])
    } finally {
      setLoading(false)
    }
  }

  const launch = async (id: number): Promise<void> => {
    try {
      await api.post('/apps/start', { id })
    } catch {
      /* noop */
    }
  }

  const unfavorite = async (id: number, name: string): Promise<void> => {
    try {
      await api.post('/apps/favorite', { id, is_favorite: false })
      showNotice(`已从快捷启动移除「${name}」`)
      loadFavorites()
    } catch {
      showNotice('移除失败，请重试')
    }
  }

  const onDragOver = (e: DragEvent<HTMLDivElement>): void => {
    e.preventDefault()
    e.dataTransfer.dropEffect = 'copy'
    if (!dragging) setDragging(true)
    if (dragResetTimer.current) window.clearTimeout(dragResetTimer.current)
    dragResetTimer.current = window.setTimeout(() => setDragging(false), 2500)
  }

  const onDragEnter = (e: DragEvent<HTMLDivElement>): void => {
    e.preventDefault()
    setDragging(true)
  }

  const onDragLeave = (e: DragEvent<HTMLDivElement>): void => {
    e.preventDefault()
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
      setDragging(false)
    }
  }

  const onDragEnd = (): void => {
    setDragging(false)
  }

  const onDrop = async (e: DragEvent<HTMLDivElement>): Promise<void> => {
    e.preventDefault()
    setDragging(false)
    const files = Array.from(e.dataTransfer.files ?? [])
    const paths: string[] = []
    for (const f of files) {
      try {
        const p = window.paicc?.getPathForFile(f)
        if (p) paths.push(p)
      } catch {
        /* file has no resolvable filesystem path — skip */
      }
    }
    if (paths.length === 0) return
    const results = await Promise.allSettled(
      paths.map((p) => api.post('/apps/pin', { path: p })),
    )
    const okCount = results.filter(
      (r) => r.status === 'fulfilled' && r.value?.data?.ok,
    ).length
    const failed = paths.length - okCount
    showNotice(
      failed > 0
        ? failed === paths.length
          ? '添加失败，请检查文件'
          : `已添加 ${okCount} 个，${failed} 个失败`
        : `已添加 ${okCount} 个快捷方式`,
    )
    loadFavorites()
  }

  const onBallMouseDown = (e: ReactMouseEvent<HTMLDivElement>): void => {
    if (e.button !== 0) return
    const startX = e.screenX
    const startY = e.screenY
    didDrag.current = false
    ballDraggingRef.current = true
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current)
    setMenuOpen(false)
    window.paicc?.ballDragStart(startX, startY)
    const move = (ev: MouseEvent): void => {
      if (
        !didDrag.current &&
        Math.abs(ev.screenX - startX) + Math.abs(ev.screenY - startY) > 4
      ) {
        didDrag.current = true
      }
      window.paicc?.ballDragMove(ev.screenX, ev.screenY)
    }
    const up = (): void => {
      ballDraggingRef.current = false
      window.paicc?.ballDragEnd()
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const onBallClick = (): void => {
    if (didDrag.current) {
      didDrag.current = false
      return
    }
    // Docked strip: hover already opens the menu; clicking does nothing extra.
    if (docked) return
    setMenuOpen(false)
    setInputOpen((v) => !v)
  }

  // The ball morphs into a thin strip when docked (same element, animated by CSS).
  const ballStyle: React.CSSProperties = (() => {
    if (!docked || !edge) {
      return {
        left: ORBIT_X - BALL_SIZE / 2,
        top: ORBIT_Y - BALL_SIZE / 2,
        width: BALL_SIZE,
        height: BALL_SIZE,
        borderRadius: '50%',
      }
    }
    if (edge === 'left' || edge === 'right') {
      return { left: ORBIT_X - 7, top: ORBIT_Y - 32, width: 14, height: 64, borderRadius: 7 }
    }
    return { left: ORBIT_X - 32, top: ORBIT_Y - 7, width: 64, height: 14, borderRadius: 7 }
  })()

  return (
    <div
      className={`ball-root${dragging ? ' dragging' : ''}`}
      onMouseLeave={onWinLeave}
      onMouseMove={(e) => updatePointer(e.clientX, e.clientY)}
      onDragOver={onDragOver}
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      onDragEnd={onDragEnd}
    >
      {dragging && <div className="ball-drop-hint">松开以添加到快捷启动</div>}
      {notice && <div className="ball-notice">{notice}</div>}

      {/* Satellite ring: items orbit the ball and auto-avoid off-screen angles. */}
      {menuOpen && !inputOpen && (
        <>
          {MENU.map((item, i) => {
            const deg = menuAngles[i] ?? 0
            return (
              <div
                key={item.key}
                className="radial-item"
                title={item.label}
                style={{ left: ORBIT_X - 22, top: ORBIT_Y - 22, transform: orbitTransform(deg, RADIUS) }}
                onClick={() => window.paicc?.openRoute(item.key)}
              >
                {item.icon}
              </div>
            )
          })}
          {/* Inner ring: favorite apps quick-launch */}
          {favorites.slice(0, 6).map((app, i) => {
            const deg = favAngles[i] ?? 0
            return (
              <div
                key={app.id}
                className="radial-item"
                title={`${app.name}（右键移除）`}
                style={{
                  left: ORBIT_X - 16,
                  top: ORBIT_Y - 16,
                  width: 32,
                  height: 32,
                  fontSize: 12,
                  transform: orbitTransform(deg, INNER_R),
                }}
                onClick={() => launch(app.id)}
                onContextMenu={(e) => {
                  e.preventDefault()
                  void unfavorite(app.id, app.name)
                }}
              >
                <AppIcon app={app} size={22} />
              </div>
            )
          })}
        </>
      )}

      {!docked && inputOpen && (
        <>
          {history.length > 0 && (
            <div className="ball-answer" style={{ left: 20, top: 12 }}>
              {history.map((m, i) => (
                <div key={i} className={m.role === 'user' ? 'ball-msg-user' : 'ball-msg-assistant'}>
                  {m.role === 'assistant' ? <Markdown text={m.content} /> : m.content}
                </div>
              ))}
              {loading && <div className="ball-msg-assistant ball-msg-thinking">思考中…</div>}
            </div>
          )}
          <div className="ball-input" style={{ left: 20, top: 176, width: 300 }}>
            <textarea
              autoFocus
              value={text}
              placeholder="输入指令，Enter 发送 / Ctrl+Enter 换行"
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.ctrlKey) {
                  e.preventDefault()
                  send()
                }
              }}
            />
            <SendOutlined style={{ color: '#4a90e2', cursor: 'pointer' }} onClick={send} />
            <CloseOutlined
              style={{ color: '#8a93a6', cursor: 'pointer' }}
              onClick={() => {
                setInputOpen(false)
                setHistory([])
              }}
            />
          </div>
        </>
      )}

      {!docked && (
        <div className="ball-quit" title="退出 PAICC" onClick={() => window.paicc?.quitApp()}>
          <PoweroffOutlined />
        </div>
      )}

      <div
        className={`ball${docked ? ' docked' : ''}`}
        style={ballStyle}
        onMouseEnter={onAnchorEnter}
        onMouseLeave={onAnchorLeave}
        onMouseDown={onBallMouseDown}
        onClick={onBallClick}
      >
        {!docked && (
          <div className="ball-inner">
            {loading ? <span style={{ fontSize: 12 }}>…</span> : <CommentOutlined />}
          </div>
        )}
      </div>
    </div>
  )
}
