import { ClearOutlined, SendOutlined } from '@ant-design/icons'
import { Button, Empty, Input, Space, Spin, Tag } from 'antd'
import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import Markdown from '@/components/Markdown'
import { useChatStore } from '@/store/useChatStore'
import type { ChatMessage } from '@/types'

const { TextArea } = Input

export default function ChatPage(): JSX.Element {
  const { messages, sending, send, clear } = useChatStore()
  const [text, setText] = useState('')
  const location = useLocation()
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Seed the input from navigation state (e.g. "让 AI 分析" red-line shortcut).
  useEffect(() => {
    const prefill = (location.state as { prefill?: string } | null)?.prefill
    if (typeof prefill === 'string' && prefill.trim()) {
      setText(prefill)
    }
  }, [location.state])

  const handleSend = async (): Promise<void> => {
    const value = text.trim()
    if (!value || sending) return
    setText('')
    await send(value)
  }

  const renderBubble = (msg: ChatMessage, index: number): JSX.Element => {
    const isUser = msg.role === 'user'
    return (
      <div
        key={index}
        style={{
          display: 'flex',
          justifyContent: isUser ? 'flex-end' : 'flex-start',
          marginBottom: 12,
        }}
      >
        <div style={{ maxWidth: '72%' }}>
          <div
            style={{
              padding: '10px 14px',
              borderRadius: 12,
              background: isUser ? '#4a90e2' : '#171a21',
              color: isUser ? '#ffffff' : '#e6e9ef',
              border: isUser ? 'none' : '1px solid #262b36',
              whiteSpace: isUser ? 'pre-wrap' : undefined,
              wordBreak: 'break-word',
              fontSize: 14,
              lineHeight: 1.6,
            }}
          >
            {isUser ? msg.content : <Markdown text={msg.content} />}
          </div>
          {msg.tool_calls && msg.tool_calls.length > 0 && (
            <div
              style={{
                marginTop: 6,
                display: 'flex',
                flexWrap: 'wrap',
                gap: 6,
                justifyContent: isUser ? 'flex-end' : 'flex-start',
              }}
            >
              {msg.tool_calls.map((tc, i) => (
                <Tag key={i} color="geekblue" style={{ marginRight: 0 }}>
                  {tc.name}
                </Tag>
              ))}
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0 }}>
      <div style={{ flex: 1, overflowY: 'auto', minHeight: 0, paddingRight: 4 }}>
        {messages.length === 0 ? (
          <Empty description="开始和 AI 助手对话吧" style={{ marginTop: 80 }} />
        ) : (
          messages.map(renderBubble)
        )}
        <div ref={bottomRef} />
      </div>
      <div style={{ borderTop: '1px solid #262b36', paddingTop: 12 }}>
        <TextArea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="输入消息，Enter 发送，Ctrl+Enter 换行"
          autoSize={{ minRows: 2, maxRows: 6 }}
          disabled={sending}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) {
              e.preventDefault()
              void handleSend()
            }
          }}
        />
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            marginTop: 10,
          }}
        >
          <Button icon={<ClearOutlined />} onClick={clear} disabled={sending}>
            清空
          </Button>
          <Space>
            {sending && <Spin size="small" />}
            <Button type="primary" icon={<SendOutlined />} onClick={handleSend} disabled={sending}>
              发送
            </Button>
          </Space>
        </div>
      </div>
    </div>
  )
}
