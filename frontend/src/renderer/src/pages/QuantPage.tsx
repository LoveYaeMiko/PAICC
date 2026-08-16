import {
  FolderOpenOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SaveOutlined,
  ScanOutlined,
  StopOutlined,
} from '@ant-design/icons'
import {
  Button,
  Card,
  Col,
  Empty,
  Form,
  Input,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  notification,
} from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/services/api'
import { confirmOperation } from '@/services/confirm'
import { useWsEvent } from '@/services/ws'
import type {
  QuantCommand,
  QuantProcessInfo,
  QuantProject,
  RedLine,
  RedLineLevel,
  RedLineStatus,
} from '@/types'

interface DetectResult {
  root_path: string
  config_files: string[]
  entry_points: string[]
  log_dir: string | null
  dashboard_script: string
  has_simulation_dashboard: boolean
}

interface QuantConfig {
  project_id: number
  config_file: string
  path: string
  content: string
}

const LEVEL_COLOR: Record<RedLineLevel, string> = {
  ok: '#52c41a',
  warning: '#faad14',
  critical: '#ff4d4f',
  unknown: '#8a93a6',
}

const LEVEL_LABEL: Record<RedLineLevel, string> = {
  ok: '正常',
  warning: '警告',
  critical: '严重',
  unknown: '未知',
}

function describeError(err: unknown): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as { response?: { data?: unknown } }).response
    const data = resp?.data
    if (data && typeof data === 'object' && 'detail' in data) {
      return String((data as { detail: unknown }).detail)
    }
    if (typeof data === 'string') return data
  }
  if (err instanceof Error) return err.message
  return String(err)
}

function formatPercent(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${v.toFixed(1)}%`
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return '—'
  const s = Math.floor(seconds)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (d > 0) return `${d}天${h}时`
  if (h > 0) return `${h}时${m}分`
  if (m > 0) return `${m}分${sec}秒`
  return `${sec}秒`
}

function formatTime(ts: number | null | undefined): string {
  if (ts == null) return '—'
  const ms = ts > 1e12 ? ts : ts * 1000
  return new Date(ms).toLocaleString()
}

function isErrorLine(line: string): boolean {
  return /(error|trace|错误)/i.test(line)
}

function RedLineCard({ redLine }: { redLine: RedLine }): JSX.Element {
  const color = LEVEL_COLOR[redLine.level] ?? LEVEL_COLOR.ok
  return (
    <Card size="small" style={{ borderTop: `3px solid ${color}`, height: '100%' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Typography.Text strong ellipsis style={{ maxWidth: 160 }}>
          {redLine.label ?? redLine.name}
        </Typography.Text>
        <Tag color={color} style={{ marginInlineEnd: 0 }}>
          {LEVEL_LABEL[redLine.level] ?? redLine.level}
        </Tag>
      </div>
      <div className="mono" style={{ fontSize: 22, fontWeight: 700, color, margin: '8px 0' }}>
        {redLine.value == null ? '—' : redLine.value}
      </div>
      <div style={{ fontSize: 12, color: '#8a93a6' }}>
        阈值：{redLine.threshold == null ? '—' : redLine.threshold}
      </div>
      {redLine.detail ? (
        <div style={{ fontSize: 12, color: '#8a93a6', marginTop: 4, wordBreak: 'break-all' }}>
          {redLine.detail}
        </div>
      ) : null}
    </Card>
  )
}

const projectColumns: TableColumnsType<QuantProject> = [
  { title: '名称', dataIndex: 'name', key: 'name' },
  {
    title: '根路径',
    dataIndex: 'root_path',
    key: 'root_path',
    className: 'mono',
    ellipsis: true,
    render: (v: string) => v ?? '—',
  },
  {
    title: '配置文件',
    dataIndex: 'config_file',
    key: 'config_file',
    className: 'mono',
    ellipsis: true,
    render: (v: string) => v ?? '—',
  },
  {
    title: '状态',
    dataIndex: 'is_active',
    key: 'is_active',
    width: 90,
    render: (v: boolean) => <Tag color={v ? 'green' : 'default'}>{v ? '启用' : '停用'}</Tag>,
  },
]

const processColumns: TableColumnsType<QuantProcessInfo> = [
  { title: 'PID', dataIndex: 'pid', key: 'pid', width: 80, className: 'mono' },
  {
    title: '命令行',
    dataIndex: 'cmdline',
    key: 'cmdline',
    className: 'mono',
    ellipsis: true,
    render: (v: string) => v ?? '—',
  },
  {
    title: 'CPU',
    dataIndex: 'cpu_percent',
    key: 'cpu_percent',
    width: 100,
    render: (v: number) => formatPercent(v),
  },
  {
    title: '内存',
    dataIndex: 'memory_percent',
    key: 'memory_percent',
    width: 100,
    render: (v: number) => formatPercent(v),
  },
  {
    title: '运行时长',
    dataIndex: 'running_time',
    key: 'running_time',
    width: 130,
    render: (v: number) => formatDuration(v),
  },
]

export default function QuantPage(): JSX.Element {
  const [status, setStatus] = useState<RedLineStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(true)

  const [projects, setProjects] = useState<QuantProject[]>([])
  const [projectsLoading, setProjectsLoading] = useState(true)

  const [processes, setProcesses] = useState<QuantProcessInfo[]>([])
  const [processesLoading, setProcessesLoading] = useState(true)

  const [commands, setCommands] = useState<QuantCommand[]>([])
  const [commandsLoading, setCommandsLoading] = useState(true)

  const [logs, setLogs] = useState<string[]>([])
  const [logsLoading, setLogsLoading] = useState(true)

  const [savingToKb, setSavingToKb] = useState(false)

  const [configProjectId, setConfigProjectId] = useState<number | undefined>()
  const [configContent, setConfigContent] = useState('')
  const [configPath, setConfigPath] = useState('')
  const [configLoading, setConfigLoading] = useState(false)
  const [configSaving, setConfigSaving] = useState(false)

  const [form] = Form.useForm<{ root_path: string; name: string }>()
  const logRef = useRef<HTMLPreElement>(null)

  const refreshStatus = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<RedLineStatus>('/quant/status')
      setStatus(data)
    } catch {
      setStatus(null)
    } finally {
      setStatusLoading(false)
    }
  }, [])

  const refreshProjects = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<QuantProject[]>('/quant/projects')
      setProjects(Array.isArray(data) ? data : [])
    } catch {
      setProjects([])
    } finally {
      setProjectsLoading(false)
    }
  }, [])

  const refreshProcesses = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<QuantProcessInfo[]>('/quant/processes')
      setProcesses(Array.isArray(data) ? data : [])
    } catch {
      setProcesses([])
    } finally {
      setProcessesLoading(false)
    }
  }, [])

  const refreshCommands = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<QuantCommand[]>('/quant/commands')
      setCommands(Array.isArray(data) ? data : [])
    } catch {
      setCommands([])
    } finally {
      setCommandsLoading(false)
    }
  }, [])

  const refreshLogs = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<unknown>('/quant/logs', { params: { lines: 100 } })
      setLogs(normalizeLogs(data))
    } catch {
      setLogs([])
    } finally {
      setLogsLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshStatus()
    void refreshProjects()
    void refreshProcesses()
    void refreshCommands()
    void refreshLogs()
  }, [refreshStatus, refreshProjects, refreshProcesses, refreshCommands, refreshLogs])

  const onRedLineAlert = useCallback(() => {
    void refreshStatus()
  }, [refreshStatus])

  const onLogLine = useCallback((data: unknown) => {
    let line: string
    if (typeof data === 'string') {
      line = data
    } else if (data && typeof data === 'object') {
      const obj = data as Record<string, unknown>
      line = String(obj.line ?? obj.message ?? obj.text ?? '')
    } else {
      line = String(data)
    }
    setLogs((prev) => [...prev, line])
  }, [])

  const onQuantProcesses = useCallback((data: unknown) => {
    if (Array.isArray(data)) {
      setProcesses(data as QuantProcessInfo[])
      setProcessesLoading(false)
    }
  }, [])

  useWsEvent('red_line_alert', onRedLineAlert)
  useWsEvent('log_line', onLogLine)
  useWsEvent('quant_processes', onQuantProcesses)

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [logs])

  useEffect(() => {
    if (configProjectId == null && projects.length > 0) {
      setConfigProjectId(projects[0].id)
    }
  }, [projects, configProjectId])

  const onCreateProject = async (values: { root_path: string; name: string }): Promise<void> => {
    try {
      await api.post('/quant/projects', values)
      notification.success({ message: '项目已注册', description: values.name })
      form.resetFields()
      await refreshProjects()
    } catch (err) {
      notification.error({ message: '注册失败', description: describeError(err) })
    }
  }

  const handleRunCommand = async (cmd: QuantCommand): Promise<void> => {
    const confirmationId = await confirmOperation('run_quant_command', '执行量化命令', {
      name: cmd.name,
      command: cmd.command,
    })
    if (!confirmationId) return
    try {
      await api.post('/quant/command', { command_id: cmd.id, confirmation_id: confirmationId })
      notification.success({ message: '命令已执行', description: cmd.name })
    } catch (err) {
      notification.error({ message: '执行失败', description: describeError(err) })
    }
  }

  const handleStopCommand = async (projectId: number): Promise<void> => {
    const confirmationId = await confirmOperation('stop_quant_command', '停止量化进程', {
      project_id: projectId,
    })
    if (!confirmationId) return
    try {
      const { data } = await api.post<{ stopped: number[]; errors: number[] }>('/quant/stop', {
        project_id: projectId,
        confirmation_id: confirmationId,
      })
      notification.success({
        message: '进程已停止',
        description: data.stopped?.length ? `已停止 PID：${data.stopped.join(', ')}` : '无正在运行的进程',
      })
      await refreshProcesses()
    } catch (err) {
      notification.error({ message: '停止失败', description: describeError(err) })
    }
  }

  const handleDetect = async (): Promise<void> => {
    const values = form.getFieldsValue()
    let rootPath = values.root_path?.trim()
    if (!rootPath && projects.length > 0) rootPath = projects[0].root_path
    if (!rootPath) {
      notification.warning({ message: '请先输入项目根路径' })
      return
    }
    try {
      const { data } = await api.get<DetectResult>('/quant/detect', { params: { root_path: rootPath } })
      notification.success({
        message: '检测完成',
        description: (
          <div style={{ maxHeight: 240, overflow: 'auto' }}>
            <p>根路径：{data.root_path}</p>
            <p>配置文件：{data.config_files?.length ? data.config_files.join(', ') : '未找到'}</p>
            <p>仪表盘脚本：{data.dashboard_script || '未找到'}</p>
            <p>入口：{data.entry_points?.length ? data.entry_points.join(', ') : '未找到'}</p>
            <p>日志目录：{data.log_dir ?? '未找到'}</p>
            <p>模拟盘仪表盘：{data.has_simulation_dashboard ? '已检测到' : '未检测到'}</p>
          </div>
        ),
      })
    } catch (err) {
      notification.error({ message: '检测失败', description: describeError(err) })
    }
  }

  const loadConfig = async (): Promise<void> => {
    setConfigLoading(true)
    try {
      const { data } = await api.get<QuantConfig>('/quant/config', {
        params: { project_id: configProjectId },
      })
      setConfigContent(data.content ?? '')
      setConfigPath(data.path ?? data.config_file ?? '')
      notification.success({ message: '配置已加载', description: data.path })
    } catch (err) {
      notification.error({ message: '加载配置失败', description: describeError(err) })
    } finally {
      setConfigLoading(false)
    }
  }

  const saveConfig = async (): Promise<void> => {
    const confirmationId = await confirmOperation('save_quant_config', '保存量化配置文件', {
      project_id: configProjectId,
    })
    if (!confirmationId) return
    setConfigSaving(true)
    try {
      await api.post('/quant/config', {
        project_id: configProjectId,
        content: configContent,
        confirmation_id: confirmationId,
      })
      notification.success({ message: '配置已保存', description: configPath })
    } catch (err) {
      notification.error({ message: '保存配置失败', description: describeError(err) })
    } finally {
      setConfigSaving(false)
    }
  }

  const handleSaveToKb = async (): Promise<void> => {
    if (!status) return
    const title = `量化红线报告 ${formatTime(status.timestamp)}`
    const content = buildStatusReport(status)
    setSavingToKb(true)
    try {
      await api.post('/quant/save-report', { title, content })
      notification.success({ message: '已保存到知识库', description: title })
    } catch (err) {
      notification.error({ message: '保存失败', description: describeError(err) })
    } finally {
      setSavingToKb(false)
    }
  }

  const commandColumns: TableColumnsType<QuantCommand> = [
    { title: '名称', dataIndex: 'name', key: 'name' },
    {
      title: '命令',
      dataIndex: 'command',
      key: 'command',
      className: 'mono',
      ellipsis: true,
      render: (v: string) => v ?? '—',
    },
    {
      title: '描述',
      dataIndex: 'description',
      key: 'description',
      ellipsis: true,
      render: (v: string) => v ?? '—',
    },
    {
      title: '操作',
      key: 'action',
      width: 180,
      render: (_, record) => (
        <Space size={4}>
          <Button
            size="small"
            type="primary"
            danger
            icon={<PlayCircleOutlined />}
            onClick={() => void handleRunCommand(record)}
          >
            运行
          </Button>
          <Button
            size="small"
            icon={<StopOutlined />}
            onClick={() => void handleStopCommand(record.project_id)}
          >
            停止
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <Card
        title="红线仪表盘"
        extra={
          <Space size={8}>
            <Button
              size="small"
              icon={<SaveOutlined />}
              disabled={!status}
              loading={savingToKb}
              onClick={() => void handleSaveToKb()}
            >
              保存到知识库
            </Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshStatus()}>
              刷新
            </Button>
          </Space>
        }
      >
        {statusLoading ? (
          <Spin />
        ) : status == null ? (
          <Empty description="无法获取红线状态" />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            <Space size={8}>
              <Typography.Text>总体状态：</Typography.Text>
              <Tag color={LEVEL_COLOR[status.overall] ?? LEVEL_COLOR.ok}>
                {LEVEL_LABEL[status.overall] ?? status.overall}
              </Tag>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {formatTime(status.timestamp)}
              </Typography.Text>
            </Space>
            <Row gutter={[12, 12]}>
              {(status.red_lines ?? []).map((rl) => (
                <Col key={rl.name} xs={24} sm={12} md={8} lg={6}>
                  <RedLineCard redLine={rl} />
                </Col>
              ))}
            </Row>
          </Space>
        )}
      </Card>

      <Card
        title="量化项目"
        extra={
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshProjects()}>
            刷新
          </Button>
        }
      >
        <Form form={form} layout="inline" onFinish={(v) => void onCreateProject(v)} style={{ marginBottom: 16, rowGap: 8 }}>
          <Form.Item name="root_path" rules={[{ required: true, message: '请输入项目根路径' }]}>
            <Input placeholder="项目根路径" style={{ width: 280 }} />
          </Form.Item>
          <Form.Item name="name" rules={[{ required: true, message: '请输入项目名称' }]}>
            <Input placeholder="项目名称" style={{ width: 180 }} />
          </Form.Item>
          <Form.Item>
            <Space size={8}>
              <Button type="primary" htmlType="submit">
                注册
              </Button>
              <Button icon={<ScanOutlined />} onClick={() => void handleDetect()}>
                检测项目
              </Button>
            </Space>
          </Form.Item>
        </Form>
        {projectsLoading ? (
          <Spin />
        ) : (
          <Table<QuantProject>
            rowKey="id"
            columns={projectColumns}
            dataSource={projects}
            size="small"
            pagination={false}
          />
        )}
      </Card>

      <Card
        title="量化进程"
        extra={
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshProcesses()}>
            刷新
          </Button>
        }
      >
        {processesLoading ? (
          <Spin />
        ) : (
          <Table<QuantProcessInfo>
            rowKey="pid"
            columns={processColumns}
            dataSource={processes}
            size="small"
            pagination={false}
          />
        )}
      </Card>

      <Card
        title="量化命令"
        extra={
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshCommands()}>
            刷新
          </Button>
        }
      >
        {commandsLoading ? (
          <Spin />
        ) : (
          <Table<QuantCommand>
            rowKey="id"
            columns={commandColumns}
            dataSource={commands}
            size="small"
            pagination={false}
          />
        )}
      </Card>

      <Card
        title="日志"
        styles={{ body: { padding: 0 } }}
        extra={
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshLogs()}>
            刷新
          </Button>
        }
      >
        {logsLoading ? (
          <div style={{ padding: 16 }}>
            <Spin />
          </div>
        ) : (
          <pre
            ref={logRef}
            className="mono"
            style={{
              margin: 0,
              padding: 12,
              height: 320,
              overflow: 'auto',
              fontSize: 12,
              lineHeight: 1.6,
              background: '#0b0d11',
              borderBottomLeftRadius: 8,
              borderBottomRightRadius: 8,
            }}
          >
            {logs.length === 0 ? (
              <span style={{ color: '#8a93a6' }}>（暂无日志）</span>
            ) : (
              logs.map((line, i) => (
                <span
                  key={i}
                  style={{ display: 'block', color: isErrorLine(line) ? '#ff4d4f' : undefined }}
                >
                  {line || ' '}
                </span>
              ))
            )}
          </pre>
        )}
      </Card>

      <Card
        title="配置文件编辑器"
        extra={
          <Space size={8}>
            <Button
              size="small"
              icon={<FolderOpenOutlined />}
              loading={configLoading}
              onClick={() => void loadConfig()}
            >
              加载
            </Button>
            <Button
              size="small"
              type="primary"
              icon={<SaveOutlined />}
              loading={configSaving}
              onClick={() => void saveConfig()}
            >
              保存
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" style={{ width: '100%' }} size={8}>
          <Select<number>
            placeholder="选择项目"
            style={{ width: 320 }}
            value={configProjectId}
            onChange={(v) => setConfigProjectId(v)}
            options={projects.map((p) => ({ label: p.name, value: p.id }))}
          />
          {configPath ? (
            <Typography.Text type="secondary" className="mono" style={{ fontSize: 12 }}>
              {configPath}
            </Typography.Text>
          ) : null}
          <Input.TextArea
            className="mono"
            value={configContent}
            onChange={(e) => setConfigContent(e.target.value)}
            rows={16}
            spellCheck={false}
            placeholder="选择项目后点击「加载」读取配置文件"
            style={{ fontSize: 12, lineHeight: 1.5 }}
          />
        </Space>
      </Card>
    </div>
  )
}

function normalizeLogs(data: unknown): string[] {
  if (Array.isArray(data)) return data.map((l) => String(l))
  if (typeof data === 'string') return data.split('\n')
  if (data && typeof data === 'object') {
    const obj = data as Record<string, unknown>
    const candidate = obj.lines ?? obj.log ?? obj.content ?? obj.logs
    if (typeof candidate === 'string') return candidate.split('\n')
    if (Array.isArray(candidate)) return candidate.map((l) => String(l))
  }
  return []
}

function buildStatusReport(status: RedLineStatus): string {
  const lines: string[] = []
  lines.push('# 量化红线报告')
  lines.push('')
  lines.push(`- 总体状态：${LEVEL_LABEL[status.overall] ?? status.overall}`)
  lines.push(`- 时间：${formatTime(status.timestamp)}`)
  lines.push('')
  lines.push('## 红线指标')
  for (const rl of status.red_lines ?? []) {
    const label = rl.label ?? rl.name
    const level = LEVEL_LABEL[rl.level] ?? rl.level
    const value = rl.value == null ? '—' : String(rl.value)
    const detail = rl.detail ? ` — ${rl.detail}` : ''
    lines.push(`- ${label}：${level}（${value}）${detail}`)
  }
  return lines.join('\n')
}
