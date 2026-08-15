import { Alert, Button, Descriptions, Modal, Space } from 'antd'
import { useChatStore } from '@/store/useChatStore'

export default function ConfirmDialog(): JSX.Element | null {
  const { pendingConfirmation, approveConfirmation, denyConfirmation } = useChatStore()

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
        description="以下操作需要你的明确批准，30 秒未响应将自动拒绝。"
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
