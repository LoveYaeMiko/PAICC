import { DeleteOutlined, FileTextOutlined, MailOutlined } from '@ant-design/icons'
import {
  Button,
  Card,
  Empty,
  message,
  notification,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { TableColumnsType } from 'antd'
import type { Key } from 'react'
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/services/api'
import { confirmOperation } from '@/services/confirm'
import type { CleanableItem, StorageReport } from '@/types'

function formatBytes(bytes: number): string {
  if (!bytes || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1)
  const val = bytes / Math.pow(1024, i)
  return `${val.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function formatDateTime(ts: number): string {
  if (!ts) return '-'
  const ms = ts > 1e12 ? ts : ts * 1000
  return new Date(ms).toLocaleString()
}

export default function StoragePage(): JSX.Element {
  const [reportContent, setReportContent] = useState<string | null>(null)
  const [reportGenerating, setReportGenerating] = useState<boolean>(false)
  const [reports, setReports] = useState<StorageReport[]>([])
  const [reportsLoading, setReportsLoading] = useState<boolean>(false)

  const [schedule, setSchedule] = useState<string>('off')
  const [scheduleSaving, setScheduleSaving] = useState<boolean>(false)

  const [cleanableItems, setCleanableItems] = useState<CleanableItem[]>([])
  const [cleanableLoading, setCleanableLoading] = useState<boolean>(false)
  const [selectedRowKeys, setSelectedRowKeys] = useState<Key[]>([])
  const [cleaning, setCleaning] = useState<boolean>(false)

  const loadReports = useCallback(async (): Promise<void> => {
    setReportsLoading(true)
    try {
      const { data } = await api.get<StorageReport[]>('/storage/reports')
      setReports(data)
    } catch {
      notification.error({ message: '加载失败', description: '无法加载报告历史，请检查后端连接。' })
    } finally {
      setReportsLoading(false)
    }
  }, [])

  const loadCleanable = useCallback(async (): Promise<void> => {
    setCleanableLoading(true)
    try {
      const { data } = await api.get<CleanableItem[]>('/storage/cleanable-items')
      setCleanableItems(data)
    } catch {
      notification.error({ message: '加载失败', description: '无法加载可清理项，请检查后端连接。' })
    } finally {
      setCleanableLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadReports()
    void loadCleanable()
  }, [loadReports, loadCleanable])

  const handleGenerate = async (): Promise<void> => {
    setReportGenerating(true)
    try {
      const { data } = await api.post<{ content: string }>('/storage/analysis')
      setReportContent(data.content)
      message.success('报告已生成')
      await loadReports()
    } catch {
      message.error('生成报告失败，请稍后重试。')
    } finally {
      setReportGenerating(false)
    }
  }

  const handleViewReport = async (id: number): Promise<void> => {
    try {
      const { data } = await api.get<StorageReport>(`/storage/reports/${id}`)
      setReportContent(data.content)
    } catch {
      message.error('查看报告失败，请稍后重试。')
    }
  }

  const handleSendEmail = async (id: number): Promise<void> => {
    try {
      await api.post('/storage/send-email', { report_id: id })
      message.success('邮件已发送')
      await loadReports()
    } catch {
      message.error('发送邮件失败，请稍后重试。')
    }
  }

  const handleScheduleChange = async (value: string): Promise<void> => {
    setScheduleSaving(true)
    try {
      await api.post('/storage/analysis/schedule', { schedule: value })
      setSchedule(value)
      message.success('定时已更新')
    } catch {
      message.error('更新定时失败，请稍后重试。')
    } finally {
      setScheduleSaving(false)
    }
  }

  const handleClean = async (): Promise<void> => {
    const itemIds = selectedRowKeys.map((k) => String(k))
    const confirmationId = await confirmOperation('clean', '清理临时文件', { items: itemIds })
    if (!confirmationId) return
    setCleaning(true)
    try {
      await api.post('/storage/clean', { items: itemIds, confirmation_id: confirmationId })
      message.success('清理完成')
      setSelectedRowKeys([])
      await loadCleanable()
    } catch {
      message.error('清理失败，请稍后重试。')
    } finally {
      setCleaning(false)
    }
  }

  const reportColumns: TableColumnsType<StorageReport> = [
    {
      title: '生成时间',
      dataIndex: 'generated_at',
      key: 'generated_at',
      width: 220,
      render: (ts: number) => formatDateTime(ts),
    },
    {
      title: '邮件',
      dataIndex: 'sent_to_email',
      key: 'sent_to_email',
      width: 100,
      render: (sent: number) =>
        sent ? <Tag color="green">已发送</Tag> : <Tag>未发送</Tag>,
    },
    {
      title: '操作',
      key: 'actions',
      width: 200,
      render: (_: unknown, record: StorageReport) => (
        <Space>
          <Button
            size="small"
            icon={<FileTextOutlined />}
            onClick={() => void handleViewReport(record.id)}
          >
            查看
          </Button>
          <Button
            size="small"
            icon={<MailOutlined />}
            onClick={() => void handleSendEmail(record.id)}
          >
            发送邮件
          </Button>
        </Space>
      ),
    },
  ]

  const cleanableColumns: TableColumnsType<CleanableItem> = [
    { title: '类别', dataIndex: 'category', key: 'category', width: 120 },
    {
      title: '路径',
      dataIndex: 'path',
      key: 'path',
      ellipsis: true,
      render: (path: string) => <span className="mono">{path}</span>,
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 120,
      render: (size: number) => formatBytes(size),
    },
    { title: '描述', dataIndex: 'description', key: 'description', ellipsis: true },
  ]

  return (
    <div>
      <Typography.Title level={4} style={{ marginTop: 0 }}>
        存储分析
      </Typography.Title>

      {/* Section 1: 报告 */}
      <Card
        title="分析报告"
        style={{ marginBottom: 16 }}
        extra={
          <Button
            type="primary"
            icon={<FileTextOutlined />}
            loading={reportGenerating}
            onClick={() => void handleGenerate()}
          >
            生成报告
          </Button>
        }
      >
        {reportContent ? (
          <pre
            className="mono"
            style={{
              whiteSpace: 'pre-wrap',
              maxHeight: 320,
              overflow: 'auto',
              background: '#0f1115',
              border: '1px solid #262b36',
              borderRadius: 8,
              padding: 12,
              fontSize: 12,
              margin: '0 0 16px 0',
            }}
          >
            {reportContent}
          </pre>
        ) : (
          <Empty description="尚未生成报告" style={{ margin: '16px 0' }} />
        )}

        <Typography.Text strong style={{ display: 'block', marginBottom: 8 }}>
          历史报告
        </Typography.Text>
        {reportsLoading ? (
          <Spin />
        ) : (
          <Table<StorageReport>
            rowKey="id"
            columns={reportColumns}
            dataSource={reports}
            pagination={false}
            size="small"
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史报告" /> }}
          />
        )}
      </Card>

      {/* Section 2: 定时 */}
      <Card title="定时分析" style={{ marginBottom: 16 }}>
        <Space>
          <Typography.Text>定时计划：</Typography.Text>
          <Select
            value={schedule}
            style={{ width: 160 }}
            loading={scheduleSaving}
            options={[
              { value: 'weekly', label: '每周' },
              { value: 'monthly', label: '每月' },
              { value: 'off', label: '关闭' },
            ]}
            onChange={(value) => void handleScheduleChange(value)}
          />
        </Space>
      </Card>

      {/* Section 3: 清理 */}
      <Card
        title="清理临时文件"
        extra={
          <Button
            type="primary"
            danger
            icon={<DeleteOutlined />}
            loading={cleaning}
            disabled={selectedRowKeys.length === 0}
            onClick={() => void handleClean()}
          >
            清理选中（{selectedRowKeys.length}）
          </Button>
        }
      >
        {cleanableLoading ? (
          <Spin />
        ) : (
          <Table<CleanableItem>
            rowKey="id"
            columns={cleanableColumns}
            dataSource={cleanableItems}
            pagination={false}
            size="small"
            rowSelection={{
              selectedRowKeys,
              onChange: (keys: Key[]) => setSelectedRowKeys(keys),
            }}
            locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可清理的临时文件" /> }}
          />
        )}
      </Card>
    </div>
  )
}
