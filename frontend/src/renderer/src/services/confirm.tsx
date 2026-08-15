import { Modal } from 'antd'
import { api } from './api'

/**
 * Create + immediately approve a confirmation, then return its id so the caller can
 * invoke the guarded backend endpoint. Returns null if the user cancels.
 */
export async function confirmOperation(
  action: string,
  title: string,
  details: Record<string, unknown>,
): Promise<string | null> {
  return new Promise((resolve) => {
    Modal.confirm({
      title: `⚠️ ${title}`,
      content: (
        <div>
          <p>操作：{action}</p>
          <pre className="mono" style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
            {JSON.stringify(details, null, 2)}
          </pre>
        </div>
      ),
      okText: '批准执行',
      cancelText: '拒绝',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          const { data } = await api.post('/confirmations', { action, title, details })
          await api.post(`/confirmations/${data.confirmation_id}/approve`)
          resolve(data.confirmation_id)
        } catch {
          resolve(null)
        }
      },
      onCancel: () => resolve(null),
    })
  })
}
