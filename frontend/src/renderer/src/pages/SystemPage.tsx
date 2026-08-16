import {
  Button,
  Card,
  Col,
  Empty,
  Progress,
  Row,
  Select,
  Spin,
  Statistic,
  Switch,
  Table,
  Tag,
  notification,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/services/api'
import { confirmOperation } from '@/services/confirm'
import { useWsEvent } from '@/services/ws'
import { useSystemStore } from '@/store/useSystemStore'
import type { GpuInfo, ProcessInfo, StartupItem, SystemStats } from '@/types'

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function errMsg(e: unknown): string {
  const err = e as { response?: { data?: { detail?: string; message?: string } }; message?: string }
  return err.response?.data?.detail ?? err.response?.data?.message ?? err.message ?? '未知错误'
}

function extractNames(data: unknown): string[] {
  if (!Array.isArray(data)) return []
  return data.map((item) => {
    if (typeof item === 'string') return item
    if (item && typeof item === 'object' && 'name' in item) return String((item as { name: unknown }).name)
    return String(item)
  })
}

const GPU_COLUMNS: ColumnsType<GpuInfo> = [
  { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true },
  {
    title: '负载',
    dataIndex: 'load',
    key: 'load',
    width: 100,
    sorter: (a, b) => a.load - b.load,
    render: (v: number) => `${v.toFixed(1)}%`,
  },
  {
    title: '温度',
    dataIndex: 'temperature',
    key: 'temperature',
    width: 100,
    render: (v: number | null) => (v == null ? '—' : `${v}°C`),
  },
  {
    title: '显存',
    key: 'memory',
    render: (_: unknown, record: GpuInfo) =>
      `${formatBytes(record.memory_used)} / ${formatBytes(record.memory_total)}`,
  },
]

export default function SystemPage(): JSX.Element {
  const { stats, processes, setStats, setProcesses } = useSystemStore()

  const [loading, setLoading] = useState(true)
  const [processLoading, setProcessLoading] = useState(false)
  const [powerLoading, setPowerLoading] = useState(false)
  const [powerPlans, setPowerPlans] = useState<string[]>([])
  const [powerPlan, setPowerPlan] = useState<string | undefined>(undefined)
  const [processSort, setProcessSort] = useState('cpu')
  const [startupItems, setStartupItems] = useState<StartupItem[]>([])
  const [startupLoading, setStartupLoading] = useState(false)
  const [startupToggling, setStartupToggling] = useState<string | null>(null)

  const loadStatus = useCallback(async () => {
    try {
      const res = await api.get<unknown>('/system/status')
      const data = res.data as { stats?: SystemStats } & SystemStats
      setStats((data.stats ?? data) as SystemStats)
    } catch (e) {
      notification.error({ message: '获取系统状态失败', description: errMsg(e) })
    } finally {
      setLoading(false)
    }
  }, [setStats])

  const loadProcesses = useCallback(
    async (sort = 'cpu') => {
      setProcessLoading(true)
      try {
        const res = await api.get<unknown>('/system/processes', { params: { sort } })
        setProcesses(res.data as ProcessInfo[])
      } catch (e) {
        notification.error({ message: '获取进程列表失败', description: errMsg(e) })
      } finally {
        setProcessLoading(false)
      }
    },
    [setProcesses],
  )

  const loadPowerPlans = useCallback(async () => {
    try {
      const res = await api.get<unknown>('/system/power-plans')
      setPowerPlans(extractNames(res.data))
    } catch (e) {
      notification.error({ message: '获取电源计划失败', description: errMsg(e) })
    }
  }, [])

  const loadStartupItems = useCallback(async () => {
    setStartupLoading(true)
    try {
      const res = await api.get<unknown>('/system/startup-items')
      setStartupItems(res.data as StartupItem[])
    } catch (e) {
      notification.error({ message: '获取开机启动项失败', description: errMsg(e) })
    } finally {
      setStartupLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadStatus()
    void loadProcesses()
    void loadPowerPlans()
    void loadStartupItems()
  }, [loadStatus, loadProcesses, loadPowerPlans, loadStartupItems])

  const onSystemStats = useCallback(
    (data: unknown) => {
      setStats(data as SystemStats)
    },
    [setStats],
  )
  useWsEvent('system_stats', onSystemStats)

  const killProcess = async (proc: ProcessInfo): Promise<void> => {
    const confirmationId = await confirmOperation('kill_process', '结束进程', {
      pid: proc.pid,
      name: proc.name,
    })
    if (!confirmationId) return
    try {
      await api.post('/system/process/kill', { pid: proc.pid, confirmation_id: confirmationId })
      notification.success({ message: '进程已结束', description: `PID ${proc.pid} · ${proc.name}` })
      await loadProcesses()
    } catch (e) {
      notification.error({ message: '结束进程失败', description: errMsg(e) })
    }
  }

  const applyPowerPlan = async (name: string): Promise<void> => {
    setPowerLoading(true)
    try {
      await api.post('/system/power-plan', { name })
      setPowerPlan(name)
      notification.success({ message: '电源计划已切换', description: name })
    } catch (e) {
      notification.error({ message: '切换电源计划失败', description: errMsg(e) })
    } finally {
      setPowerLoading(false)
    }
  }

  const changeProcessSort = (sort: string): void => {
    setProcessSort(sort)
    void loadProcesses(sort)
  }

  const revealProcess = async (proc: ProcessInfo): Promise<void> => {
    try {
      await api.post(`/system/process/${proc.pid}/reveal`)
      notification.success({ message: '已在资源管理器中定位', description: proc.exe || proc.name })
    } catch (e) {
      notification.error({ message: '定位文件失败', description: errMsg(e) })
    }
  }

  const toggleStartup = async (item: StartupItem, enabled: boolean): Promise<void> => {
    setStartupToggling(item.name)
    try {
      const res = await api.post<{ ok?: boolean; message?: string }>('/system/startup/toggle', {
        name: item.name,
        enabled,
      })
      const data = res.data
      if (data && data.ok === false) {
        notification.warning({ message: '启动项未变更', description: data.message ?? item.name })
      } else {
        notification.success({
          message: enabled ? '启动项已启用' : '启动项已禁用',
          description: item.name,
        })
      }
      await loadStartupItems()
    } catch (e) {
      notification.error({ message: '切换启动项失败', description: errMsg(e) })
    } finally {
      setStartupToggling(null)
    }
  }

  const processColumns: ColumnsType<ProcessInfo> = [
    { title: 'PID', dataIndex: 'pid', key: 'pid', width: 90, sorter: (a, b) => a.pid - b.pid },
    { title: '进程名', dataIndex: 'name', key: 'name', ellipsis: true },
    {
      title: 'CPU %',
      dataIndex: 'cpu_percent',
      key: 'cpu_percent',
      width: 110,
      sorter: (a, b) => a.cpu_percent - b.cpu_percent,
      render: (v: number) => v.toFixed(1),
    },
    {
      title: '内存 %',
      dataIndex: 'memory_percent',
      key: 'memory_percent',
      width: 110,
      sorter: (a, b) => a.memory_percent - b.memory_percent,
      render: (v: number) => v.toFixed(1),
    },
    {
      title: '磁盘读',
      dataIndex: 'disk_read_bytes',
      key: 'disk_read_bytes',
      width: 120,
      render: (v: number | undefined) => (v == null ? '—' : formatBytes(v)),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 120,
      render: (v: string) => {
        const color =
          v === 'running' ? 'green' : v === 'zombie' ? 'red' : v === 'sleeping' ? 'blue' : 'default'
        return <Tag color={color}>{v}</Tag>
      },
    },
    {
      title: '操作',
      key: 'action',
      width: 160,
      render: (_: unknown, record: ProcessInfo) => (
        <div style={{ display: 'flex', gap: 4 }}>
          {record.exe && (
            <Button size="small" onClick={() => void revealProcess(record)}>
              定位
            </Button>
          )}
          <Button danger size="small" onClick={() => void killProcess(record)}>
            结束
          </Button>
        </div>
      ),
    },
  ]

  const startupColumns: ColumnsType<StartupItem> = [
    { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true },
    {
      title: '位置',
      dataIndex: 'location',
      key: 'location',
      width: 90,
      render: (v: string) => <Tag>{v}</Tag>,
    },
    {
      title: '来源',
      dataIndex: 'source',
      key: 'source',
      width: 100,
      render: (v: string) => <Tag color={v === '注册表' ? 'blue' : 'default'}>{v}</Tag>,
    },
    {
      title: '命令',
      dataIndex: 'command',
      key: 'command',
      ellipsis: true,
      render: (v: string) => <span className="mono">{v}</span>,
    },
    {
      title: '启用',
      key: 'enabled',
      width: 80,
      render: (_: unknown, record: StartupItem) => (
        <Switch
          size="small"
          checked={record.enabled}
          loading={startupToggling === record.name}
          onChange={(checked) => void toggleStartup(record, checked)}
        />
      ),
    },
  ]

  const diskPercent = stats && stats.disk.length > 0 ? stats.disk[0].percent : 0
  const diskIO = stats?.disk_io ?? { read_bytes: 0, write_bytes: 0, read_per_sec: 0, write_per_sec: 0 }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {loading ? (
        <div style={{ display: 'flex', justifyContent: 'center', padding: 48 }}>
          <Spin size="large" />
        </div>
      ) : stats ? (
        <>
          <Row gutter={[16, 16]}>
            <Col xs={12} sm={8} md={6} xl={4}>
              <Card className="stat-card" size="small">
                <Statistic title="CPU" value={stats.cpu_percent} suffix="%" precision={1} />
              </Card>
            </Col>
            <Col xs={12} sm={8} md={6} xl={4}>
              <Card className="stat-card" size="small">
                <Statistic title="内存" value={stats.memory.percent} suffix="%" precision={1} />
              </Card>
            </Col>
            <Col xs={12} sm={8} md={6} xl={4}>
              <Card className="stat-card" size="small">
                <Statistic title="磁盘" value={diskPercent} suffix="%" precision={1} />
              </Card>
            </Col>
            <Col xs={12} sm={8} md={6} xl={4}>
              <Card className="stat-card" size="small">
                <Statistic title="磁盘读取" value={`${formatBytes(diskIO.read_per_sec)}/s`} />
              </Card>
            </Col>
            <Col xs={12} sm={8} md={6} xl={4}>
              <Card className="stat-card" size="small">
                <Statistic title="磁盘写入" value={`${formatBytes(diskIO.write_per_sec)}/s`} />
              </Card>
            </Col>
            <Col xs={12} sm={8} md={6} xl={4}>
              <Card className="stat-card" size="small">
                <Statistic title="网络下载" value={`${formatBytes(stats.net.recv_per_sec)}/s`} />
              </Card>
            </Col>
            <Col xs={12} sm={8} md={6} xl={4}>
              <Card className="stat-card" size="small">
                <Statistic title="网络上传" value={`${formatBytes(stats.net.sent_per_sec)}/s`} />
              </Card>
            </Col>
          </Row>

          <Row gutter={[16, 16]}>
            <Col xs={24} lg={12}>
              <Card title="CPU 各核心" size="small">
                {stats.cpu_per_core.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无核心数据" />
                ) : (
                  stats.cpu_per_core.map((v, i) => (
                    <div
                      key={i}
                      style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}
                    >
                      <span className="mono" style={{ width: 56, flexShrink: 0 }}>
                        核心 {i}
                      </span>
                      <Progress
                        percent={Math.max(0, Math.min(100, Math.round(v)))}
                        size="small"
                        style={{ flex: 1, margin: 0 }}
                      />
                    </div>
                  ))
                )}
              </Card>
            </Col>
            <Col xs={24} lg={12}>
              <Card title="磁盘使用" size="small">
                {stats.disk.length === 0 ? (
                  <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="无磁盘数据" />
                ) : (
                  stats.disk.map((d) => (
                    <div key={d.mountpoint} style={{ marginBottom: 12 }}>
                      <div
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          marginBottom: 4,
                        }}
                      >
                        <span>
                          {d.mountpoint} <span className="mono">({d.device})</span>
                        </span>
                        <span className="mono">
                          {formatBytes(d.used)} / {formatBytes(d.total)}
                        </span>
                      </div>
                      <Progress percent={d.percent} size="small" style={{ margin: 0 }} />
                    </div>
                  ))
                )}
              </Card>
            </Col>
          </Row>

          {stats.gpu.length > 0 && (
            <Card title="GPU" size="small">
              <Table
                rowKey="name"
                columns={GPU_COLUMNS}
                dataSource={stats.gpu}
                size="small"
                pagination={false}
              />
            </Card>
          )}
        </>
      ) : (
        <Empty description="无法获取系统状态" />
      )}

      <Card title="电源计划" size="small">
        <Select
          allowClear
          placeholder="选择电源计划"
          value={powerPlan}
          loading={powerLoading}
          style={{ width: 260 }}
          options={powerPlans.map((p) => ({ value: p, label: p }))}
          onChange={(value: string) => void applyPowerPlan(value)}
        />
      </Card>

      <Card
        title="进程列表"
        size="small"
        extra={
          <Select
            value={processSort}
            style={{ width: 120 }}
            options={[
              { value: 'cpu', label: 'CPU' },
              { value: 'memory', label: '内存' },
              { value: 'name', label: '名称' },
              { value: 'disk', label: '磁盘' },
            ]}
            onChange={changeProcessSort}
          />
        }
      >
        <Table
          rowKey="pid"
          columns={processColumns}
          dataSource={processes}
          loading={processLoading}
          size="small"
          pagination={{ pageSize: 10, showSizeChanger: true, showTotal: (total) => `共 ${total} 个进程` }}
          scroll={{ x: 840 }}
        />
      </Card>

      <Card title="开机启动项" size="small">
        <Table
          rowKey={(record: StartupItem) => `${record.source}-${record.location}-${record.name}`}
          columns={startupColumns}
          dataSource={startupItems}
          loading={startupLoading}
          size="small"
          pagination={false}
          scroll={{ x: 720 }}
        />
      </Card>
    </div>
  )
}
