import {
  AppstoreOutlined,
  BookOutlined,
  CloseOutlined,
  CommentOutlined,
  DashboardOutlined,
  FileSearchOutlined,
  LineChartOutlined,
  PieChartOutlined,
  SendOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { useEffect, useRef, useState } from 'react'
import { api, BACKEND_URL } from '@/services/api'
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
  const [answer, setAnswer] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [favorites, setFavorites] = useState<AppEntry[]>([])
  const hoverTimer = useRef<number | null>(null)

  useEffect(() => {
    api
      .get('/apps/list', { params: { favorites_only: 1 } })
      .then((r) => setFavorites(r.data))
      .catch(() => setFavorites([]))
  }, [])

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
    setAnswer('')
    try {
      const { data } = await api.post('/ai/chat', {
        messages: [{ role: 'user', content: t }],
        use_tools: true,
      })
      setAnswer(data.needs_confirmation ? `⚠️ 需要确认：${data.confirmation?.title}` : data.content)
    } catch {
      setAnswer('请求失败，请检查后端连接。')
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

  return (
    <div className="ball-root" onMouseLeave={onWinLeave}>
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
                title={app.name}
                style={{ left, top, width: 32, height: 32, fontSize: 12 }}
                onClick={() => launch(app.id)}
              >
                <AppIcon app={app} size={22} />
              </div>
            )
          })}
        </>
      )}

      {inputOpen && (
        <>
          <div className="ball-input" style={{ left: 20, top: 128, width: 300 }}>
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
                setAnswer(null)
              }}
            />
          </div>
          {answer != null && (
            <div className="ball-answer" style={{ left: 20, top: 12 }}>
              {loading ? '思考中…' : answer}
            </div>
          )}
        </>
      )}

      <div
        className="ball"
        style={{ left: CENTER_X - BALL_SIZE / 2, top: CENTER_Y - BALL_SIZE / 2 }}
        onMouseEnter={onBallEnter}
        onMouseLeave={onBallLeave}
        onClick={() => {
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
