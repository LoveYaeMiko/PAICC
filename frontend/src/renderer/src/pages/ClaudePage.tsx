import { PlayCircleOutlined, PoweroffOutlined, SendOutlined } from '@ant-design/icons'
import { Badge, Button, Input, Space, Spin, notification } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import ClaudeTerminal from '@/components/ClaudeTerminal'
import { api } from '@/services/api'
import { useWsEvent } from '@/services/ws'

interface ClaudeStatus {
  available: boolean
  running: boolean
  [key: string]: unknown
}

interface FileChange {
  file: string
  diff: string
  timestamp: number
}

export default function ClaudePage(): JSX.Element {
  const [status, setStatus] = useState<ClaudeStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)
  const [starting, setStarting] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [fileChanges, setFileChanges] = useState<FileChange[]>([])
  const [changesOpen, setChangesOpen] = useState(false)

  const loadStatus = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<ClaudeStatus>('/claude/status')
      setStatus(data)
    } catch {
      notification.error({ message: '获取 Claude 状态失败', description: '请检查后端连接。' })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadStatus()
  }, [loadStatus])

  const onFileEvent = useCallback((raw: unknown): void => {
    const data = (raw ?? {}) as { type?: string; file?: unknown; diff?: unknown; timestamp?: unknown }
    if (data.type !== 'file_changed') return
    const file = typeof data.file === 'string' ? data.file : ''
    const diff = typeof data.diff === 'string' ? data.diff : ''
    if (!file && !diff) return
    const timestamp = typeof data.timestamp === 'number' ? data.timestamp : Date.now()
    setFileChanges((prev) => [{ file, diff, timestamp }, ...prev].slice(0, 50))
  }, [])

  useWsEvent('claude_event', onFileEvent)

  const handleStart = async (): Promise<void> => {
    setStarting(true)
    try {
      await api.post('/claude/start')
      notification.success({ message: '会话已启动' })
      await loadStatus()
    } catch {
      notification.error({ message: '启动会话失败' })
    } finally {
      setStarting(false)
    }
  }

  const handleStop = async (): Promise<void> => {
    setStopping(true)
    try {
      await api.post('/claude/stop')
      notification.success({ message: '会话已停止' })
      await loadStatus()
    } catch {
      notification.error({ message: '停止会话失败' })
    } finally {
      setStopping(false)
    }
  }

  const handleSend = async (): Promise<void> => {
    const text = message.trim()
    if (!text || sending) return
    setSending(true)
    try {
      await api.post('/claude/send', { message: text })
      setMessage('')
    } catch {
      notification.error({ message: '发送消息失败' })
    } finally {
      setSending(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          padding: '12px 16px',
          background: '#171a21',
          border: '1px solid #262b36',
          borderRadius: 8,
        }}
      >
        <Space size="large">
          {loading ? (
            <Spin size="small" />
          ) : (
            <>
              <span>
                <Badge
                  status={status?.available ? 'success' : 'error'}
                  text={status?.available ? 'Claude 可用' : 'Claude 不可用'}
                />
              </span>
              <span>
                <Badge
                  status={status?.running ? 'processing' : 'default'}
                  text={status?.running ? '运行中' : '未运行'}
                />
              </span>
            </>
          )}
        </Space>
        <Space>
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={starting}
            disabled={status?.running}
            onClick={handleStart}
          >
            启动会话
          </Button>
          <Button
            danger
            icon={<PoweroffOutlined />}
            loading={stopping}
            disabled={!status?.running}
            onClick={handleStop}
          >
            停止
          </Button>
        </Space>
      </div>

      <ClaudeTerminal />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div
          onClick={() => setChangesOpen((open) => !open)}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            cursor: 'pointer',
            padding: '10px 12px',
            background: '#171a21',
            border: '1px solid #262b36',
            borderRadius: 8,
            userSelect: 'none',
          }}
        >
          <span style={{ color: '#e6e9ef' }}>{changesOpen ? '▾' : '▸'} 文件变更</span>
          <span
            style={{
              color: '#57c7ff',
              fontSize: 12,
              padding: '0 8px',
              borderRadius: 10,
              background: 'rgba(87, 199, 255, 0.12)',
            }}
          >
            {fileChanges.length}
          </span>
        </div>
        {changesOpen && (
          <div
            style={{
              display: 'flex',
              flexDirection: 'column',
              gap: 8,
              maxHeight: 420,
              overflowY: 'auto',
            }}
          >
            {fileChanges.length === 0 ? (
              <div style={{ color: '#8a93a6', padding: '4px 0' }}>暂无文件变更</div>
            ) : (
              fileChanges.map((fc, i) => (
                <div
                  key={`${fc.timestamp}-${i}`}
                  style={{
                    border: '1px solid #262b36',
                    borderRadius: 6,
                    background: '#0f1115',
                    padding: 8,
                  }}
                >
                  <div
                    style={{
                      color: '#57c7ff',
                      fontFamily: "'Cascadia Code', Consolas, monospace",
                      fontSize: 12,
                      marginBottom: 4,
                      wordBreak: 'break-all',
                    }}
                  >
                    {fc.file}
                  </div>
                  <pre
                    style={{
                      margin: 0,
                      fontFamily: "'Cascadia Code', 'Fira Code', Consolas, 'Courier New', monospace",
                      fontSize: 12,
                      lineHeight: 1.5,
                      whiteSpace: 'pre-wrap',
                      wordBreak: 'break-all',
                      color: '#e6e9ef',
                    }}
                  >
                    {fc.diff}
                  </pre>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      <div style={{ display: 'flex', gap: 8 }}>
        <Input
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onPressEnter={handleSend}
          placeholder="输入消息，回车或点击发送"
          disabled={sending}
        />
        <Button
          type="primary"
          icon={<SendOutlined />}
          loading={sending}
          disabled={!message.trim()}
          onClick={handleSend}
        >
          发送
        </Button>
      </div>
    </div>
  )
}
