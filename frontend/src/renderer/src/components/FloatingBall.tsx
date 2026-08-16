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
const CENTER_X = 170
const CENTER_Y = 256
const RADIUS = 118

const MENU = [
  { key: '/', icon: <DashboardOutlined />, label: '系统' },
  { key: '/files', icon: <FileSearchOutlined />, label: '文件' },
  { key: '/apps', icon: <AppstoreOutlined />, label: '应用' },
  { key: '/storage', icon: <PieChartOutlined />, label: '存储' },
  { key: '/quant', icon: <LineChartOutlined />, label: '量化' },
  { key: '/research', icon: <BookOutlined />, label: '研究' },
  { key: '/settings', icon: <SettingOutlined />, label: '设置' },
]

function anglePoint(deg: number): { left: number; top: number } {
  const rad = (deg * Math.PI) / 180
  return {
    left: CENTER_X + RADIUS * Math.cos(rad) - 22,
    top: CENTER_Y - RADIUS * Math.sin(rad) - 22,
  }
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
  const hoverTimer = useRef<number | null>(null)
  const dragResetTimer = useRef<number | null>(null)
  const noticeTimer = useRef<number | null>(null)
  const didDrag = useRef(false)

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

  // Alt+Space global shortcut toggles the quick-input panel.
  useEffect(() => {
    window.paicc?.onToggleInput(() => {
      setMenuOpen(false)
      setInputOpen((v) => !v)
    })
  }, [])

  const onBallEnter = (): void => {
    hoverTimer.current = window.setTimeout(() => setMenuOpen(true), 500)
  }
  const onBallLeave = (): void => {
    if (hoverTimer.current) window.clearTimeout(hoverTimer.current)
  }
  const onWinLeave = (): void => {
    setMenuOpen(false)
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
    // Auto-hide if the drag is cancelled (Esc) or stalls without further events.
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
      window.paicc?.ballDragEnd()
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  return (
    <div
      className={`ball-root${dragging ? ' dragging' : ''}`}
      onMouseLeave={onWinLeave}
      onDragOver={onDragOver}
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      onDragEnd={onDragEnd}
    >
      {dragging && <div className="ball-drop-hint">松开以添加到快捷启动</div>}
      {notice && <div className="ball-notice">{notice}</div>}
      <div className="ball-quit" title="退出 PAICC" onClick={() => window.paicc?.quitApp()}>
        <PoweroffOutlined />
      </div>
      {menuOpen && !inputOpen && (
        <>
          {MENU.map((item, i) => {
            const p = anglePoint(150 - i * 20)
            return (
              <div
                key={item.key}
                className="radial-item"
                title={item.label}
                style={{ left: p.left, top: p.top }}
                onClick={() => window.paicc?.openRoute(item.key)}
              >
                {item.icon}
              </div>
            )
          })}
          {/* Inner ring: favorite apps quick-launch */}
          {favorites.slice(0, 6).map((app, i) => {
            const p = anglePoint(150 - i * 20)
            const innerR = 0.55
            const left = CENTER_X + (p.left + 22 - CENTER_X) * innerR - 16
            const top = CENTER_Y + (p.top + 22 - CENTER_Y) * innerR - 16
            return (
              <div
                key={app.id}
                className="radial-item"
                title={`${app.name}（右键移除）`}
                style={{ left, top, width: 32, height: 32, fontSize: 12 }}
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

      {inputOpen && (
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

      <div
        className="ball"
        style={{ left: CENTER_X - BALL_SIZE / 2, top: CENTER_Y - BALL_SIZE / 2 }}
        onMouseEnter={onBallEnter}
        onMouseLeave={onBallLeave}
        onMouseDown={onBallMouseDown}
        onClick={() => {
          if (didDrag.current) {
            didDrag.current = false
            return
          }
          setMenuOpen(false)
          setInputOpen((v) => !v)
        }}
      >
        <div className="ball-inner">
          {loading ? <span style={{ fontSize: 12 }}>…</span> : <CommentOutlined />}
        </div>
      </div>
    </div>
  )
}
