import { Button, Form, Input, Select, Space, Spin, Switch, Table, Tag, notification } from 'antd'
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

/** Settings are stored as strings（``"true"``/``"false"``），so a boolean switch is a Select. */
const ENABLED_OPTIONS = [
  { value: 'true', label: '启用' },
  { value: 'false', label: '禁用' },
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
  const [autoLaunch, setAutoLaunch] = useState(false)
  const [autoLaunchLoading, setAutoLaunchLoading] = useState(true)

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
    window.paicc
      ?.getAutoLaunch()
      .then((v) => setAutoLaunch(Boolean(v)))
      .catch(() => {})
      .finally(() => setAutoLaunchLoading(false))
  }, [])

  const handleAutoLaunchChange = async (checked: boolean): Promise<void> => {
    setAutoLaunch(checked)
    try {
      await window.paicc?.setAutoLaunch(checked)
      notification.success({ message: checked ? '已开启开机自启' : '已关闭开机自启' })
    } catch (err) {
      setAutoLaunch(!checked)
      notification.error({ message: '开机自启设置失败', description: String(err) })
    }
  }

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
          <SectionTitle>系统</SectionTitle>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              padding: '12px 16px',
              border: '1px solid #262b36',
              borderRadius: 8,
              marginBottom: 12,
            }}
          >
            <div>
              <div style={{ color: '#e6e9ef' }}>开机自启</div>
              <div style={{ color: '#8b93a1', fontSize: 12 }}>系统启动时自动运行 PAICC</div>
            </div>
            <Switch
              checked={autoLaunch}
              loading={autoLaunchLoading}
              onChange={handleAutoLaunchChange}
            />
          </div>

          <SectionTitle>LLM</SectionTitle>
          <FieldGrid>
            <Form.Item name="llm_provider" label="Provider">
              <Select options={PROVIDER_OPTIONS} />
            </Form.Item>
            <Form.Item name="llm_model" label="模型">
              <Input placeholder="例如 deepseek-chat" />
            </Form.Item>
            <Form.Item name="llm_base_url" label="Base URL">
              <Input placeholder="https://api.deepseek.com" />
            </Form.Item>
            <Form.Item name="llm_api_key" label="API Key">
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
            <Form.Item
              name="quant_forward_enabled"
              label="前向期候选影子盘"
              extra="每交易日 15:20 推进「当前跟踪的候选规则」账本（规则名与账本路径取自 FQA configs/forward_policy.yaml，隔离账本、配对比较，仅记录不自动切换）"
            >
              <Select options={ENABLED_OPTIONS} />
            </Form.Item>
            <Form.Item
              name="quant_forward_health_enabled"
              label="前向期风险闸门"
              extra="每交易日 15:40 评估风险闸门（≥5 个可测日才有意义）；关闭后候选仍照常推进"
            >
              <Select options={ENABLED_OPTIONS} />
            </Form.Item>
          </FieldGrid>

          <SectionTitle>文件</SectionTitle>
          <FieldGrid>
            <Form.Item name="everything_path" label="Everything (es.exe) 路径">
              <Input placeholder="例如 C:\Program Files\Everything\es.exe" />
            </Form.Item>
          </FieldGrid>

          <SectionTitle>Claude CLI</SectionTitle>
          <FieldGrid>
            <Form.Item name="claude_path" label="Claude CLI 路径">
              <Input placeholder="留空则从 PATH 查找，例如 ...\AppData\Roaming\npm\claude.cmd" />
            </Form.Item>
            <Form.Item
              name="claude_permission_mode"
              label="权限模式"
              extra="留空则沿用 Claude Code 自身配置（如 acceptEdits）"
            >
              <Select
                allowClear
                placeholder="留空 = 沿用 Claude Code 配置"
                options={[
                  { value: 'acceptEdits', label: 'acceptEdits（自动接受编辑）' },
                  { value: 'plan', label: 'plan（仅计划，不改文件）' },
                  { value: 'default', label: 'default（默认）' },
                  { value: 'bypassPermissions', label: 'bypassPermissions（跳过所有权限）' },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="claude_model"
              label="模型"
              extra="留空则沿用 Claude Code 默认模型"
            >
              <Input placeholder="例如 claude-opus-5 / claude-sonnet-5" />
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
