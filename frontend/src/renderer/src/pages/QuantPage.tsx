import {
  CalculatorOutlined,
  ExperimentOutlined,
  FolderOpenOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  RobotOutlined,
  SaveOutlined,
  ScanOutlined,
  StopOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Form,
  Input,
  Row,
  Segmented,
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
import dayjs from 'dayjs'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart'
import DualShadowPanel from '@/components/DualShadowPanel'
import { api } from '@/services/api'
import { confirmOperation } from '@/services/confirm'
import { useWsEvent } from '@/services/ws'
import type {
  AutopilotState,
  QuantCommand,
  QuantProcessInfo,
  QuantProject,
  QuantScheduleStatus,
  RedLine,
  RedLineHistoryPoint,
  RedLineLevel,
  RedLineStatus,
  S7Calibration,
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

const AUTOPILOT_MODE_COLOR: Record<string, string> = {
  normal: '#52c41a',
  de_risk: '#faad14',
  halt: '#ff4d4f',
}

const AUTOPILOT_MODE_LABEL: Record<string, string> = {
  normal: '正常',
  de_risk: '收缩',
  halt: '停止',
}

// ECharts heatmap visualMap.pieces only matches numeric values (string values
// silently fail and render the cells blank), so red-line levels are mapped to a
// stable index before being fed to the heatmap series.
const LEVEL_ORDER: RedLineLevel[] = ['ok', 'warning', 'critical', 'unknown']
function levelIndex(lv: string): number {
  const i = LEVEL_ORDER.indexOf(lv as RedLineLevel)
  return i >= 0 ? i : LEVEL_ORDER.indexOf('unknown')
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

function formatIso(v: string | null | undefined): string {
  if (!v) return '—'
  const d = dayjs(v)
  return d.isValid() ? d.format('YYYY-MM-DD HH:mm') : v
}

function fmtRedValue(v: number | string | null | undefined): string {
  if (v == null) return ''
  const s = String(v)
  if (s === 'True' || s === 'true' || s === '1') return '异常'
  if (s === 'False' || s === 'false' || s === '0') return '正常'
  return s
}

// The kill-switch reasons about *current* drawdown-from-peak (the last point of
// the equity curve), not the historical max-drawdown. The dual-track panel
// (DualShadowPanel) surfaces the historical figure directly.

function isErrorLine(line: string): boolean {
  return /(error|trace|错误)/i.test(line)
}

// --------------------------------------------------------------------------- #
// ECharts option builders (calibration charts; the dual-track shadow charts
// live in DualShadowPanel)
// --------------------------------------------------------------------------- #
function buildAmplitudeOption(cal: S7Calibration): EChartsOption {
  const results = cal.amplitude?.results ?? []
  if (!results.length) return {} as EChartsOption
  const sorted = [...results].sort((a, b) => a.amplitude - b.amplitude)
  const best = sorted.find((r) => r.amplitude === cal.amplitude.recommended) ?? null
  return {
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0 },
    grid: { left: 52, right: 52, top: 20, bottom: 44 },
    xAxis: { type: 'category', data: sorted.map((r) => String(r.amplitude)), name: '幅度' },
    yAxis: [
      { type: 'value', name: 'Sharpe', scale: true },
      { type: 'value', name: '回撤%', axisLabel: { formatter: '{value}%' }, splitLine: { show: false } },
    ],
    series: [
      {
        name: 'Sharpe',
        type: 'line',
        data: sorted.map((r) => r.sharpe),
        markPoint: best
          ? { data: [{ coord: [String(best.amplitude), best.sharpe], name: '推荐' }] }
          : undefined,
      },
      {
        name: '最大回撤',
        type: 'line',
        yAxisIndex: 1,
        data: sorted.map((r) => +(r.max_drawdown * 100).toFixed(2)),
      },
    ],
  } as EChartsOption
}

function buildSentimentHeatmap(cal: S7Calibration): EChartsOption {
  const results = cal.sentiment?.results ?? []
  if (!results.length) return {} as EChartsOption
  const zs = cal.sentiment.zscore_grid ?? [...new Set(results.map((r) => r.zscore_threshold))].sort((a, b) => a - b)
  const fz = cal.sentiment.freeze_grid ?? [...new Set(results.map((r) => r.freeze_days))].sort((a, b) => a - b)
  const zi = new Map(zs.map((z, i) => [z, i]))
  const fi = new Map(fz.map((f, i) => [f, i]))
  const data = results.map((r) => [zi.get(r.zscore_threshold), fi.get(r.freeze_days), r.sharpe])
  const sharpe = results.map((r) => r.sharpe)
  return {
    tooltip: {
      formatter: (p: unknown) => {
        const v = (p as { value: number[] }).value
        return `z=${zs[v[0]]}, freeze=${fz[v[1]]}<br/>Sharpe ${v[2].toFixed(3)}`
      },
    },
    grid: { left: 60, right: 20, top: 20, bottom: 72 },
    xAxis: { type: 'category', data: fz.map(String), name: 'freeze_days' },
    yAxis: { type: 'category', data: zs.map(String), name: 'zscore' },
    visualMap: {
      min: Math.min(...sharpe),
      max: Math.max(...sharpe),
      show: true,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      text: ['高', '低'],
    },
    series: [
      {
        type: 'heatmap',
        data,
        label: { show: true, formatter: (p: unknown) => (p as { value: number[] }).value[2].toFixed(2) },
      },
    ],
  } as EChartsOption
}

function buildRedLineHistoryOption(history: RedLineHistoryPoint[]): EChartsOption {
  if (!history.length) return {} as EChartsOption
  const byTs = new Map<number, RedLineHistoryPoint[]>()
  for (const h of history) {
    const arr = byTs.get(h.ts) ?? []
    arr.push(h)
    byTs.set(h.ts, arr)
  }
  const times = [...byTs.keys()].sort((a, b) => a - b)
  const names = Array.from(new Set(history.map((h) => h.name)))
  const labels = names.map((n) => history.find((h) => h.name === n)?.label || n)
  const data = times.flatMap((t, ti) =>
    (byTs.get(t) ?? []).map((p) => [ti, names.indexOf(p.name), levelIndex(p.level)]),
  )
  return {
    tooltip: {
      formatter: (p: unknown) => {
        const v = (p as { value: [number, number, number] }).value
        const name = names[v[1]]
        const label = labels[v[1]]
        const pt = (byTs.get(times[v[0]]) ?? []).find((h) => h.name === name)
        const level = LEVEL_ORDER[v[2]] ?? 'unknown'
        const lv = LEVEL_LABEL[level] ?? level
        return `${label} · ${formatTime(times[v[0]])}<br/>状态：${lv}${pt && pt.value != null ? `（值 ${fmtRedValue(pt.value)}）` : ''}`
      },
    },
    grid: { left: 120, right: 20, top: 20, bottom: 44 },
    xAxis: { type: 'category', data: times.map((t) => formatTime(t)) },
    yAxis: { type: 'category', data: labels },
    visualMap: {
      show: false,
      dimension: 2,
      pieces: LEVEL_ORDER.map((lvl, i) => ({ value: i, color: LEVEL_COLOR[lvl] })),
    },
    series: [{ type: 'heatmap', data }],
  } as EChartsOption
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
        {typeof redLine.value === 'boolean'
          ? redLine.value
            ? '异常'
            : '正常'
          : redLine.value == null
            ? '—'
            : redLine.value}
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

function asStr(v: unknown): string {
  if (v == null) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

function fmtSent(v: unknown): string {
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    return `z=${o.zscore_threshold ?? '—'}, freeze=${o.freeze_days ?? '—'}`
  }
  return asStr(v)
}

function fmtCost(v: unknown): string {
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>
    return `佣金 ${o.commission_bps ?? '—'} bps`
  }
  return asStr(v)
}

function CalibrationOverview({ cal }: { cal: S7Calibration }): JSX.Element {
  const item = (label: string, cur: string, rec: string): JSX.Element => (
    <Descriptions.Item label={label}>
      <span style={{ color: '#8a93a6' }}>{cur}</span>
      <span style={{ margin: '0 8px' }}>→</span>
      <span style={{ color: '#52c41a', fontWeight: 600 }}>{rec}</span>
    </Descriptions.Item>
  )
  return (
    <Descriptions size="small" column={1} bordered>
      {item('PEAD 倾斜幅度', asStr(cal.amplitude?.current), asStr(cal.amplitude?.recommended))}
      {item('舆情阈值', fmtSent(cal.sentiment?.current), fmtSent(cal.sentiment?.recommended))}
      {item('交易成本模型', fmtCost(cal.cost?.current), fmtCost(cal.cost?.recommended))}
    </Descriptions>
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

  const [shadowPanelKey, setShadowPanelKey] = useState(0)
  const [calibration, setCalibration] = useState<S7Calibration | null>(null)
  const [calibrationLoading, setCalibrationLoading] = useState(true)
  const [calibrationError, setCalibrationError] = useState<string | null>(null)
  const [schedule, setSchedule] = useState<QuantScheduleStatus | null>(null)
  const [autopilotStates, setAutopilotStates] = useState<Record<string, AutopilotState>>({})
  const [autopilotLoading, setAutopilotLoading] = useState(true)
  const [autopilotError, setAutopilotError] = useState<string | null>(null)
  const [runningShadow, setRunningShadow] = useState(false)
  const [runningCalibration, setRunningCalibration] = useState(false)
  const [runningAutopilot, setRunningAutopilot] = useState(false)
  const [runningWeekly, setRunningWeekly] = useState(false)
  const [redLineHistory, setRedLineHistory] = useState<RedLineHistoryPoint[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyAccount, setHistoryAccount] = useState<string>('')

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

  const refreshCalibration = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<S7Calibration | null>('/quant/calibration')
      setCalibration(data ?? null)
      setCalibrationError(null)
    } catch (err) {
      setCalibration(null)
      setCalibrationError(describeError(err))
    } finally {
      setCalibrationLoading(false)
    }
  }, [])

  const refreshSchedule = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<QuantScheduleStatus>('/quant/schedule')
      setSchedule(data ?? null)
    } catch {
      setSchedule(null)
    }
  }, [])

  const refreshAutopilot = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<Record<string, AutopilotState>>('/quant/autopilot')
      setAutopilotStates(data ?? {})
      setAutopilotError(null)
    } catch (err) {
      setAutopilotStates({})
      setAutopilotError(describeError(err))
    } finally {
      setAutopilotLoading(false)
    }
  }, [])

  const refreshHistory = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<RedLineHistoryPoint[]>('/quant/redline-history', {
        params: { limit: 300, account: historyAccount },
      })
      setRedLineHistory(Array.isArray(data) ? data : [])
    } catch {
      setRedLineHistory([])
    } finally {
      setHistoryLoading(false)
    }
  }, [historyAccount])

  useEffect(() => {
    void refreshStatus()
    void refreshProjects()
    void refreshProcesses()
    void refreshCommands()
    void refreshLogs()
    void refreshCalibration()
    void refreshSchedule()
    void refreshHistory()
    void refreshAutopilot()
  }, [refreshStatus, refreshProjects, refreshProcesses, refreshCommands, refreshLogs, refreshCalibration, refreshSchedule, refreshHistory, refreshAutopilot])

  // When the dual-track accounts first load, default the history view to the
  // first account so the heatmap is not empty.
  useEffect(() => {
    const names = Object.keys(status?.accounts ?? {})
    if (historyAccount === '' && names.length > 0) {
      setHistoryAccount(names[0])
    }
  }, [status, historyAccount])

  const onRedLineAlert = useCallback(() => {
    void refreshStatus()
    void refreshHistory()
  }, [refreshStatus, refreshHistory])

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

  const onQuantShadowRan = useCallback(() => {
    setRunningShadow(false)
    void refreshStatus()
    void refreshSchedule()
    void refreshHistory()
    setShadowPanelKey((k) => k + 1)
  }, [refreshStatus, refreshSchedule, refreshHistory])

  const onQuantCalibrated = useCallback(() => {
    setRunningCalibration(false)
    void refreshCalibration()
    void refreshSchedule()
  }, [refreshCalibration, refreshSchedule])

  const onQuantAutopilotRan = useCallback(() => {
    setRunningAutopilot(false)
    void refreshStatus()
    void refreshAutopilot()
    void refreshCalibration()
    void refreshSchedule()
    void refreshHistory()
    setShadowPanelKey((k) => k + 1)
  }, [refreshStatus, refreshAutopilot, refreshCalibration, refreshSchedule, refreshHistory])

  const onQuantWeeklyRan = useCallback(() => {
    setRunningWeekly(false)
    void refreshSchedule()
  }, [refreshSchedule])

  const onQuantShadowStarted = useCallback(() => setRunningShadow(true), [])
  const onQuantAutopilotStarted = useCallback(() => setRunningAutopilot(true), [])
  const onQuantCalibrationStarted = useCallback(() => setRunningCalibration(true), [])
  const onQuantWeeklyStarted = useCallback(() => setRunningWeekly(true), [])

  useWsEvent('red_line_alert', onRedLineAlert)
  useWsEvent('log_line', onLogLine)
  useWsEvent('quant_processes', onQuantProcesses)
  useWsEvent('quant_shadow_ran', onQuantShadowRan)
  useWsEvent('quant_shadow_started', onQuantShadowStarted)
  useWsEvent('quant_calibrated', onQuantCalibrated)
  useWsEvent('quant_calibration_started', onQuantCalibrationStarted)
  useWsEvent('quant_autopilot_ran', onQuantAutopilotRan)
  useWsEvent('quant_autopilot_started', onQuantAutopilotStarted)
  useWsEvent('quant_weekly_ran', onQuantWeeklyRan)
  useWsEvent('quant_weekly_started', onQuantWeeklyStarted)

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

  const handleRunShadow = async (): Promise<void> => {
    const confirmationId = await confirmOperation('run_quant_shadow', '运行影子模式', {
      note: '在 FQA 项目根目录执行 python cli.py shadow，逐日推进影子账本并记录目标持仓与 PnL（起点由 master_config.yaml 的 shadow.start_date 决定）',
    })
    if (!confirmationId) return
    setRunningShadow(true)
    try {
      await api.post('/quant/shadow/run', { confirmation_id: confirmationId })
      notification.success({ message: '影子模式已启动', description: '后台运行中，完成后将自动刷新并推送日报' })
    } catch (err) {
      notification.error({ message: '启动失败', description: describeError(err) })
    } finally {
      setRunningShadow(false)
    }
  }

  const handleRunCalibration = async (): Promise<void> => {
    const confirmationId = await confirmOperation('run_quant_calibrate', '运行 §7 回校', {
      note: '在 FQA 项目根目录执行 python cli.py calibrate，回校 PEAD 幅度 / 舆情阈值 / 成本模型并自动写回 master_config.yaml',
    })
    if (!confirmationId) return
    setRunningCalibration(true)
    try {
      await api.post('/quant/calibrate/run', { confirmation_id: confirmationId })
      notification.success({ message: '回校已启动', description: '后台运行中，完成后将自动刷新并推送报告' })
    } catch (err) {
      notification.error({ message: '启动失败', description: describeError(err) })
    } finally {
      setRunningCalibration(false)
    }
  }

  const handleRunAutopilot = async (): Promise<void> => {
    const confirmationId = await confirmOperation('run_quant_autopilot', '运行自动闭环', {
      note: '在 FQA 项目根目录执行 python cli.py autopilot：逐账户推进影子账本 → 逐账户风险闸门(kill-switch) → 持久化各账户档位',
    })
    if (!confirmationId) return
    setRunningAutopilot(true)
    try {
      await api.post('/quant/autopilot/run', { confirmation_id: confirmationId })
      notification.success({ message: '自动闭环已启动', description: '后台运行中，完成后将自动刷新并推送闭环日报' })
    } catch (err) {
      notification.error({ message: '启动失败', description: describeError(err) })
    } finally {
      setRunningAutopilot(false)
    }
  }

  const handleRunWeekly = async (): Promise<void> => {
    const confirmationId = await confirmOperation('run_quant_weekly', '运行周度闭环', {
      note: '在 FQA 项目根目录执行 python cli.py weekly：纳入当周数据重训 LightGBM，尾部 Sharpe 改善才 promote 新工件',
    })
    if (!confirmationId) return
    setRunningWeekly(true)
    try {
      await api.post('/quant/weekly/run', { confirmation_id: confirmationId })
      notification.success({ message: '周度闭环已启动', description: '后台运行中（重训 + promote 闸门），完成后邮件通知' })
    } catch (err) {
      notification.error({ message: '启动失败', description: describeError(err) })
    } finally {
      setRunningWeekly(false)
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
      <DualShadowPanel
        key={shadowPanelKey}
        schedule={schedule}
        runningShadow={runningShadow}
        onRunShadow={() => void handleRunShadow()}
      />
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
                上次运行：{formatTime(status.timestamp)} · 账户数 {Object.keys(status.accounts ?? {}).length}
              </Typography.Text>
            </Space>
            {Object.entries(status.accounts ?? {}).map(([name, acc]) => (
              <div key={name} style={{ width: '100%' }}>
                <Space size={8} style={{ marginBottom: 8 }}>
                  <Typography.Text strong>{name}</Typography.Text>
                  <Tag color={LEVEL_COLOR[acc.overall] ?? LEVEL_COLOR.ok}>
                    {LEVEL_LABEL[acc.overall] ?? acc.overall}
                  </Tag>
                  {acc.data_freshness_days != null ? (
                    <Tag
                      color={
                        acc.data_freshness_days <= 1 ? 'green' : acc.data_freshness_days <= 3 ? 'orange' : 'red'
                      }
                    >
                      数据新鲜度 {acc.data_freshness_days} 天
                    </Tag>
                  ) : null}
                  {acc.last_trading_date ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      截至 {formatIso(acc.last_trading_date)}
                    </Typography.Text>
                  ) : null}
                </Space>
                <Row gutter={[12, 12]}>
                  {(acc.red_lines ?? []).map((rl) => (
                    <Col key={rl.name} xs={24} sm={12} md={8} lg={6}>
                      <RedLineCard redLine={rl} />
                    </Col>
                  ))}
                </Row>
              </div>
            ))}
          </Space>
        )}
      </Card>

      <Card
        title="红线历史"
        extra={
          <Space size={8}>
            <Segmented<string>
              size="small"
              value={historyAccount}
              onChange={(v) => {
                setHistoryAccount(String(v))
                setHistoryLoading(true)
              }}
              options={Object.keys(status?.accounts ?? {}).map((n) => ({ label: n, value: n }))}
            />
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshHistory()}>
              刷新
            </Button>
          </Space>
        }
      >
        {historyLoading ? (
          <Spin />
        ) : redLineHistory.length === 0 ? (
          <Empty description={`暂无${historyAccount ? ` ${historyAccount} ` : ' '}红线历史 — 每日影子运行后累积`} />
        ) : (
          <EChart option={buildRedLineHistoryOption(redLineHistory)} height={320} />
        )}
      </Card>

      <Card
        title="自动闭环 (Autopilot)"
        extra={
          <Space size={8}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {schedule?.autopilot_enabled ? '每日自动 · 影子→逐账户风险闸门' : '已禁用（回退为影子）'}
              {schedule?.weekly_time ? ` · 周度周日 ${schedule.weekly_time}` : ''}
            </Typography.Text>
            <Button
              size="small"
              type="primary"
              icon={<RobotOutlined />}
              loading={runningAutopilot}
              onClick={() => void handleRunAutopilot()}
            >
              运行闭环
            </Button>
            <Button
              size="small"
              icon={<CalculatorOutlined />}
              loading={runningWeekly}
              onClick={() => void handleRunWeekly()}
            >
              周度闭环
            </Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshAutopilot()}>
              刷新
            </Button>
          </Space>
        }
      >
        {autopilotLoading ? (
          <Spin />
        ) : autopilotError != null ? (
          <Alert type="error" showIcon message="自动闭环输出损坏" description={autopilotError} />
        ) : Object.keys(autopilotStates).length === 0 ? (
          <Empty description="尚未运行自动闭环 — 点击「运行闭环」驱动影子→逐账户风险闸门全链路" />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            {Object.entries(autopilotStates).map(([name, st]) => (
              <div key={name} style={{ width: '100%' }}>
                <Space size={12} wrap style={{ marginBottom: 8 }}>
                  <Typography.Text strong>{name}</Typography.Text>
                  <Tag color={AUTOPILOT_MODE_COLOR[st.mode] ?? '#8a93a6'}>
                    {AUTOPILOT_MODE_LABEL[st.mode] ?? st.mode}
                  </Tag>
                  <Tag>总敞口 ×{st.gross_scale ?? 1}</Tag>
                  {st.factor_decayed ? <Tag color="warning">因子衰减</Tag> : null}
                  {st.reason ? (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      档位原因：{st.reason}
                    </Typography.Text>
                  ) : null}
                </Space>
                {st.decay_detail && Object.keys(st.decay_detail).length > 0 ? (
                  <Space direction="vertical" size={2}>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      因子衰减明细：
                    </Typography.Text>
                    {Object.entries(st.decay_detail).map(([formula, d]) => (
                      <Typography.Text
                        key={formula}
                        type={d.decayed ? 'danger' : 'secondary'}
                        style={{ fontSize: 12 }}
                      >
                        {formula}: ICIR {d.recent_icir != null ? d.recent_icir.toFixed(3) : '—'}（
                        {d.decayed ? '衰减' : '正常'}）
                      </Typography.Text>
                    ))}
                  </Space>
                ) : null}
                <Descriptions size="small" column={{ xs: 2, sm: 3, md: 6 }} bordered>
                  <Descriptions.Item label="进入档位">{formatIso(st.since_date)}</Descriptions.Item>
                  <Descriptions.Item label="上次评估">{formatIso(st.last_evaluated)}</Descriptions.Item>
                  <Descriptions.Item label="上次回校">{formatIso(st.last_calibrate)}</Descriptions.Item>
                  <Descriptions.Item label="上次监控">{formatIso(st.last_monitor)}</Descriptions.Item>
                  <Descriptions.Item label="上次重挖">{formatIso(st.last_remine)}</Descriptions.Item>
                  <Descriptions.Item label="模型迭代">{String(st.extra?.model_update ?? '—')}</Descriptions.Item>
                </Descriptions>
              </div>
            ))}
          </Space>
        )}
      </Card>

      <Card
        title="§7 三项回校"
        extra={
          <Space size={8}>
            <Button
              size="small"
              type="primary"
              icon={<ExperimentOutlined />}
              loading={runningCalibration}
              onClick={() => void handleRunCalibration()}
            >
              运行校准
            </Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshCalibration()}>
              刷新
            </Button>
          </Space>
        }
      >
        {calibrationLoading ? (
          <Spin />
        ) : calibrationError != null ? (
          <Alert type="error" showIcon message="回校输出损坏" description={calibrationError} />
        ) : calibration == null ? (
          <Empty description="尚未运行回校 — 用积累的真实时点数据校准 §7 三项" />
        ) : (
          <Space direction="vertical" style={{ width: '100%' }} size={12}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              回校窗口：{formatIso(calibration.window?.start)} ~ {formatIso(calibration.window?.end)} · 自动写回：
              {calibration.auto_apply ? '是' : '否'} · 运行时间：{formatIso(calibration.last_run)}
            </Typography.Text>
            <CalibrationOverview cal={calibration} />
            {calibration.amplitude?.results?.length || calibration.sentiment?.results?.length ? (
              <Row gutter={[12, 12]}>
                {calibration.amplitude?.results?.length ? (
                  <Col xs={24} lg={12}>
                    <Card size="small" title="幅度扫描（Sharpe vs 幅度）">
                      <EChart option={buildAmplitudeOption(calibration)} height={260} />
                    </Card>
                  </Col>
                ) : null}
                {calibration.sentiment?.results?.length ? (
                  <Col xs={24} lg={12}>
                    <Card size="small" title="舆情阈值热力图（z × freeze × Sharpe）">
                      <EChart option={buildSentimentHeatmap(calibration)} height={260} />
                    </Card>
                  </Col>
                ) : null}
              </Row>
            ) : null}
            {calibration.applied?.changed && Object.keys(calibration.applied.changed).length > 0 ? (
              <Table
                rowKey={(r) => r.path}
                size="small"
                pagination={false}
                dataSource={Object.entries(calibration.applied.changed).map(([path, kv]) => ({
                  path,
                  old: kv.old,
                  new: kv.new,
                }))}
                columns={[
                  { title: '配置项', dataIndex: 'path', key: 'path', className: 'mono' },
                  { title: '旧值', dataIndex: 'old', key: 'old', className: 'mono' },
                  {
                    title: '新值',
                    dataIndex: 'new',
                    key: 'new',
                    className: 'mono',
                    render: (v: string) => <Tag color="green">{v}</Tag>,
                  },
                ]}
              />
            ) : null}
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
  lines.push('# 量化红线报告（双资金轨）')
  lines.push('')
  lines.push(`- 总体状态：${LEVEL_LABEL[status.overall] ?? status.overall}`)
  lines.push(`- 时间：${formatTime(status.timestamp)}`)
  for (const [name, acc] of Object.entries(status.accounts ?? {})) {
    lines.push('')
    lines.push(`## 账户 ${name} — ${LEVEL_LABEL[acc.overall] ?? acc.overall}`)
    for (const rl of acc.red_lines ?? []) {
      const label = rl.label ?? rl.name
      const level = LEVEL_LABEL[rl.level] ?? rl.level
      const value = rl.value == null ? '—' : String(rl.value)
      const detail = rl.detail ? ` — ${rl.detail}` : ''
      lines.push(`- ${label}：${level}（${value}）${detail}`)
    }
  }
  if (Object.keys(status.accounts ?? {}).length === 0) {
    lines.push('')
    lines.push('## 红线指标')
    for (const rl of status.red_lines ?? []) {
      const label = rl.label ?? rl.name
      const level = LEVEL_LABEL[rl.level] ?? rl.level
      const value = rl.value == null ? '—' : String(rl.value)
      const detail = rl.detail ? ` — ${rl.detail}` : ''
      lines.push(`- ${label}：${level}（${value}）${detail}`)
    }
  }
  return lines.join('\n')
}
