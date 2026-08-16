import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Descriptions, Modal, Space } from 'antd'
import { useChatStore } from '@/store/useChatStore'

export default function ConfirmDialog(): JSX.Element | null {
  const { pendingConfirmation, approveConfirmation, denyConfirmation } = useChatStore()
  const [remaining, setRemaining] = useState(() =>
    pendingConfirmation
      ? Math.max(0, Math.ceil(pendingConfirmation.expires_at - Date.now() / 1000))
      : 0,
  )
  const autoDeniedRef = useRef(false)

  useEffect(() => {
    autoDeniedRef.current = false
    if (!pendingConfirmation) return

    const expiresAt = pendingConfirmation.expires_at
    const tick = (): void => {
      const secsLeft = expiresAt - Date.now() / 1000
      setRemaining(Math.max(0, Math.ceil(secsLeft)))
      // Fire the auto-deny slightly *before* expiry so the deny request reaches the
      // backend while the confirmation is still pending (denying after expiry 404s).
      if (secsLeft <= 0.5 && !autoDeniedRef.current) {
        autoDeniedRef.current = true
        denyConfirmation()
      }
    }

    tick()
    const id = setInterval(tick, 250)
    return () => clearInterval(id)
  }, [pendingConfirmation, denyConfirmation])

  if (!pendingConfirmation) return null

  const details = pendingConfirmation.details ?? {}

  return (
    <Modal
      open
      title="⚠️ 危险操作确认"
      closable={false}
      onCancel={denyConfirmation}
      footer={
        <Space>
          <Button onClick={denyConfirmation}>拒绝</Button>
          <Button type="primary" danger onClick={approveConfirmation}>
            批准执行
          </Button>
        </Space>
      }
    >
      <Alert
        type="warning"
        showIcon
        message={pendingConfirmation.title}
        description={`以下操作需要你的明确批准，${remaining} 秒后自动拒绝。`}
        style={{ marginBottom: 16 }}
      />
      <Descriptions column={1} size="small" bordered>
        <Descriptions.Item label="操作">{pendingConfirmation.action}</Descriptions.Item>
        {Object.entries(details).map(([k, v]) => (
          <Descriptions.Item key={k} label={k}>
            <span className="mono" style={{ wordBreak: 'break-all' }}>
              {JSON.stringify(v)}
            </span>
          </Descriptions.Item>
        ))}
      </Descriptions>
    </Modal>
  )
}
