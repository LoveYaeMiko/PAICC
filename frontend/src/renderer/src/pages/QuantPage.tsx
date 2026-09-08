import {
  LineChartOutlined,
  ReloadOutlined,
  RobotOutlined,
  SaveOutlined,
} from '@ant-design/icons'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Segmented,
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
import DTrackPanel from '@/components/DTrackPanel'
import { api } from '@/services/api'
import { confirmOperation } from '@/services/confirm'
import { useWsEvent } from '@/services/ws'
import type {
  QuantProcessInfo,
  QuantScheduleJob,
  QuantScheduleStatus,
  RedLine,
  RedLineHistoryPoint,
  RedLineLevel,
  RedLineStatus,
} from '@/types'

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

// ECharts heatmap visualMap.pieces only matches numeric values (string values
// silently fail and render the cells blank), so red-line levels are mapped to a
// stable index before being fed to the heatmap series.
const LEVEL_ORDER: RedLineLevel[] = ['ok', 'warning', 'critical', 'unknown']
function levelIndex(lv: string): number {
  const i = LEVEL_ORDER.indexOf(lv as RedLineLevel)
  return i >= 0 ? i : LEVEL_ORDER.indexOf('unknown')
}

const JOB_STATUS_COLOR: Record<string, string> = {
  ok: 'green',
  success: 'green',
  succeeded: 'green',
  running: 'processing',
  error: 'red',
  failed: 'red',
  failure: 'red',
  skipped: 'default',
}

const JOB_STATUS_LABEL: Record<string, string> = {
  ok: '成功',
  success: '成功',
  succeeded: '成功',
  running: '运行中',
  error: '失败',
  failed: '失败',
  failure: '失败',
  skipped: '跳过',
}

function jobStatusColor(v: string | null | undefined): string {
  if (!v) return 'default'
  return JOB_STATUS_COLOR[v.toLowerCase()] ?? 'default'
}

function jobStatusLabel(v: string | null | undefined): string {
  if (!v) return '—'
  return JOB_STATUS_LABEL[v.toLowerCase()] ?? v
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

/** Scheduler timestamps may arrive as ISO strings or epoch numbers. */
function formatWhen(v: string | number | null | undefined): string {
  if (v == null || v === '') return '—'
  if (typeof v === 'number') {
    const ms = v > 1e12 ? v : v * 1000
    return new Date(ms).toLocaleString()
  }
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

function isErrorLine(line: string): boolean {
  return /(error|trace|错误)/i.test(line)
}

const jobColumns: TableColumnsType<QuantScheduleJob> = [
  {
    title: '任务名',
    dataIndex: 'name',
    key: 'name',
    render: (v: string, r) => v || r.id || '—',
  },
  {
    title: '计划时间',
    dataIndex: 'cron',
    key: 'cron',
    className: 'mono',
    render: (v: string) => v || '—',
  },
  {
    title: '下次运行',
    dataIndex: 'next_run',
    key: 'next_run',
    render: (v: string | number | null) => formatWhen(v),
  },
  {
    title: '上次运行',
    dataIndex: 'last_run',
    key: 'last_run',
    render: (v: string | number | null) => formatWhen(v),
  },
  {
    title: '状态',
    dataIndex: 'last_status',
    key: 'last_status',
    width: 110,
    render: (v: string | null) => <Tag color={jobStatusColor(v)}>{jobStatusLabel(v)}</Tag>,
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

export default function QuantPage(): JSX.Element {
  const [status, setStatus] = useState<RedLineStatus | null>(null)
  const [statusLoading, setStatusLoading] = useState(true)

  const [processes, setProcesses] = useState<QuantProcessInfo[]>([])
  const [processesLoading, setProcessesLoading] = useState(true)

  const [logs, setLogs] = useState<string[]>([])
  const [logsLoading, setLogsLoading] = useState(true)

  const [savingToKb, setSavingToKb] = useState(false)

  const [schedule, setSchedule] = useState<QuantScheduleStatus | null>(null)
  const [runningShadow, setRunningShadow] = useState(false)
  const [runningAutopilot, setRunningAutopilot] = useState(false)
  const [panelKey, setPanelKey] = useState(0)

  const [redLineHistory, setRedLineHistory] = useState<RedLineHistoryPoint[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyAccount, setHistoryAccount] = useState<string>('')

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

  const refreshSchedule = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<QuantScheduleStatus>('/quant/schedule')
      setSchedule(data ?? null)
    } catch {
      setSchedule(null)
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
    void refreshProcesses()
    void refreshLogs()
    void refreshSchedule()
    void refreshHistory()
  }, [refreshStatus, refreshProcesses, refreshLogs, refreshSchedule, refreshHistory])

  // With a single account the history view defaults to it (no switcher shown).
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
    setPanelKey((k) => k + 1)
  }, [refreshStatus, refreshSchedule, refreshHistory])

  const onQuantAutopilotRan = useCallback(() => {
    setRunningAutopilot(false)
    void refreshStatus()
    void refreshSchedule()
    void refreshHistory()
    setPanelKey((k) => k + 1)
  }, [refreshStatus, refreshSchedule, refreshHistory])

  const onQuantShadowStarted = useCallback(() => setRunningShadow(true), [])
  const onQuantAutopilotStarted = useCallback(() => setRunningAutopilot(true), [])

  useWsEvent('red_line_alert', onRedLineAlert)
  useWsEvent('log_line', onLogLine)
  useWsEvent('quant_processes', onQuantProcesses)
  useWsEvent('quant_shadow_ran', onQuantShadowRan)
  useWsEvent('quant_shadow_started', onQuantShadowStarted)
  useWsEvent('quant_autopilot_ran', onQuantAutopilotRan)
  useWsEvent('quant_autopilot_started', onQuantAutopilotStarted)

  useEffect(() => {
    const el = logRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [logs])

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
    const confirmationId = await confirmOperation('run_quant_shadow', '立即运行日度闭环', {
      note: '在 FQA 项目根目录执行 python cli.py shadow：按当日 14:50 委托清单在 15:00 竞价价成交，逐日推进 D_5W 影子账本并记录目标持仓与 PnL',
    })
    if (!confirmationId) return
    setRunningShadow(true)
    try {
      await api.post('/quant/shadow/run', { confirmation_id: confirmationId })
      notification.success({ message: '日度闭环已启动', description: '后台运行中，完成后将自动刷新并推送日报' })
    } catch (err) {
      notification.error({ message: '启动失败', description: describeError(err) })
    } finally {
      setRunningShadow(false)
    }
  }

  const handleRunAutopilot = async (): Promise<void> => {
    const confirmationId = await confirmOperation('run_quant_autopilot', '运行闭环', {
      note: '在 FQA 项目根目录执行 python cli.py autopilot：推进影子账本 → 风险闸门(kill-switch) → 持久化 D_5W 档位',
    })
    if (!confirmationId) return
    setRunningAutopilot(true)
    try {
      await api.post('/quant/autopilot/run', { confirmation_id: confirmationId })
      notification.success({ message: '闭环已启动', description: '后台运行中，完成后将自动刷新并推送闭环日报' })
    } catch (err) {
      notification.error({ message: '启动失败', description: describeError(err) })
    } finally {
      setRunningAutopilot(false)
    }
  }

  const accountNames = Object.keys(status?.accounts ?? {})
  const jobs = schedule?.jobs ?? []

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <DTrackPanel refreshKey={panelKey} />

      <Card
        title="任务调度"
        extra={
          <Space size={8}>
            <Button
              size="small"
              type="primary"
              icon={<LineChartOutlined />}
              loading={runningShadow}
              onClick={() => void handleRunShadow()}
            >
              立即运行日度闭环
            </Button>
            <Button
              size="small"
              icon={<RobotOutlined />}
              loading={runningAutopilot}
              onClick={() => void handleRunAutopilot()}
            >
              运行闭环
            </Button>
            <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshSchedule()}>
              刷新
            </Button>
          </Space>
        }
      >
        {schedule?.scheduler_running === false ? (
          <Alert type="warning" showIcon message="调度器未运行" style={{ marginBottom: 12 }} />
        ) : null}
        <Table<QuantScheduleJob>
          rowKey={(r) => r.id ?? r.name}
          columns={jobColumns}
          dataSource={jobs}
          size="small"
          pagination={false}
          locale={{ emptyText: '暂无调度任务' }}
        />
      </Card>

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
                上次运行：{formatTime(status.timestamp)} · 账户数 {accountNames.length}
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
            {accountNames.length > 1 ? (
              <Segmented<string>
                size="small"
                value={historyAccount}
                onChange={(v) => {
                  setHistoryAccount(String(v))
                  setHistoryLoading(true)
                }}
                options={accountNames.map((n) => ({ label: n, value: n }))}
              />
            ) : null}
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
