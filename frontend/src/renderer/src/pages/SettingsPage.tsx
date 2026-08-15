import { Button, Form, Input, Select, Space, Spin, Table, Tag, notification } from 'antd'
import type { TableColumnsType } from 'antd'
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '@/services/api'
import { useSettingsStore } from '@/store/useSettingsStore'
import type { OperationLog } from '@/types'

const PROVIDER_OPTIONS = [
  { value: 'deepseek', label: 'deepseek' },
  { value: 'openai', label: 'openai' },
  { value: 'claude', label: 'claude' },
  { value: 'ollama', label: 'ollama' },
  { value: 'custom', label: 'custom' },
]

const formatDate = (ts: number): string => {
  if (!ts) return '-'
  const ms = ts < 1e12 ? ts * 1000 : ts
  return new Date(ms).toLocaleString('zh-CN')
}

function SectionTitle({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div
      style={{
        fontSize: 15,
        fontWeight: 600,
        margin: '24px 0 12px',
        paddingBottom: 8,
        borderBottom: '1px solid #262b36',
        color: '#e6e9ef',
      }}
    >
      {children}
    </div>
  )
}

function FieldGrid({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
        gap: '0 20px',
      }}
    >
      {children}
    </div>
  )
}

export default function SettingsPage(): JSX.Element {
  const { settings, loading, load, save } = useSettingsStore()
  const [form] = Form.useForm()
  const [logs, setLogs] = useState<OperationLog[]>([])
  const [logsLoading, setLogsLoading] = useState(false)

  useEffect(() => {
    void load()
    setLogsLoading(true)
    api
      .get('/logs/operations', { params: { limit: 200 } })
      .then(({ data }) => setLogs(data as OperationLog[]))
      .catch(() => notification.error({ message: '操作日志加载失败' }))
      .finally(() => setLogsLoading(false))
  }, [load])

  useEffect(() => {
    if (Object.keys(settings).length > 0) {
      form.setFieldsValue(settings)
    }
  }, [settings, form])

  const handleSave = async (): Promise<void> => {
    let values: Record<string, unknown>
    try {
      values = await form.validateFields()
    } catch {
      return
    }
    try {
      await save(values)
      notification.success({ message: '设置已保存' })
    } catch (err) {
      notification.error({ message: '保存失败', description: String(err) })
    }
  }

  const columns: TableColumnsType<OperationLog> = [
    {
      title: '时间',
      dataIndex: 'timestamp',
      key: 'timestamp',
      width: 180,
      render: (v: number) => formatDate(v),
    },
    { title: '用户', dataIndex: 'user', key: 'user', width: 140 },
    { title: '操作', dataIndex: 'action', key: 'action', width: 240 },
    {
      title: '参数',
      dataIndex: 'params',
      key: 'params',
      ellipsis: true,
      render: (v: unknown) => (
        <span className="mono" style={{ fontSize: 12 }}>
          {typeof v === 'string' ? v : JSON.stringify(v)}
        </span>
      ),
    },
  ]

  return (
    <div>
      <Spin spinning={loading}>
        <Form form={form} layout="vertical">
          <SectionTitle>LLM</SectionTitle>
          <FieldGrid>
            <Form.Item name="provider" label="Provider">
              <Select options={PROVIDER_OPTIONS} />
            </Form.Item>
            <Form.Item name="model" label="模型">
              <Input placeholder="例如 deepseek-chat" />
            </Form.Item>
            <Form.Item name="base_url" label="Base URL">
              <Input placeholder="https://api.deepseek.com" />
            </Form.Item>
            <Form.Item name="api_key" label="API Key">
              <Input.Password placeholder="sk-..." />
            </Form.Item>
          </FieldGrid>

          <SectionTitle>量化</SectionTitle>
          <FieldGrid>
            <Form.Item name="quant_root" label="量化根目录">
              <Input placeholder="量化项目根路径" />
            </Form.Item>
            <Form.Item name="quant_config_file" label="量化配置文件">
              <Input placeholder="配置文件名或路径" />
            </Form.Item>
            <Form.Item name="quant_log_dir" label="量化日志目录">
              <Input placeholder="日志目录路径" />
            </Form.Item>
          </FieldGrid>

          <SectionTitle>文件</SectionTitle>
          <FieldGrid>
            <Form.Item name="everything_path" label="Everything 路径">
              <Input placeholder="Everything.exe 路径" />
            </Form.Item>
          </FieldGrid>

          <SectionTitle>邮件</SectionTitle>
          <FieldGrid>
            <Form.Item name="smtp_host" label="SMTP 主机">
              <Input placeholder="smtp.example.com" />
            </Form.Item>
            <Form.Item name="smtp_port" label="SMTP 端口">
              <Input placeholder="465" />
            </Form.Item>
            <Form.Item name="smtp_user" label="SMTP 用户">
              <Input placeholder="user@example.com" />
            </Form.Item>
            <Form.Item name="smtp_password" label="SMTP 密码">
              <Input.Password placeholder="密码" />
            </Form.Item>
            <Form.Item name="smtp_from" label="发件人">
              <Input placeholder="PAICC <user@example.com>" />
            </Form.Item>
            <Form.Item name="smtp_to" label="收件人">
              <Input placeholder="receiver@example.com" />
            </Form.Item>
          </FieldGrid>

          <div style={{ marginTop: 24 }}>
            <Button type="primary" onClick={handleSave}>
              保存
            </Button>
          </div>
        </Form>
      </Spin>

      <SectionTitle>操作日志</SectionTitle>
      <Table<OperationLog>
        rowKey="id"
        size="small"
        columns={columns}
        dataSource={logs}
        loading={logsLoading}
        pagination={{ pageSize: 20, showSizeChanger: false }}
        expandable={{
          expandedRowRender: (record) => (
            <pre
              className="mono"
              style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: 0, fontSize: 12 }}
            >
              {JSON.stringify(record.result, null, 2)}
            </pre>
          ),
        }}
      />
    </div>
  )
}
