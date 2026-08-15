import {
  AppstoreOutlined,
  DeleteOutlined,
  PlayCircleOutlined,
  ScanOutlined,
  StarFilled,
  StarOutlined,
} from '@ant-design/icons'
import { Button, Empty, message, notification, Space, Spin, Switch, Table, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/services/api'
import { confirmOperation } from '@/services/confirm'
import type { AppEntry } from '@/types'

function formatDateTime(ts: number): string {
  if (!ts) return '-'
  const ms = ts > 1e12 ? ts : ts * 1000
  return new Date(ms).toLocaleString()
}

export default function AppsPage(): JSX.Element {
  const [apps, setApps] = useState<AppEntry[]>([])
  const [loading, setLoading] = useState<boolean>(false)
  const [scanning, setScanning] = useState<boolean>(false)
  const [favoritesOnly, setFavoritesOnly] = useState<boolean>(false)

  const loadApps = useCallback(async (favOnly: boolean): Promise<void> => {
    setLoading(true)
    try {
      const { data } = await api.get<AppEntry[]>('/apps/list', {
        params: { favorites_only: favOnly },
      })
      setApps(data)
    } catch {
      notification.error({ message: '加载失败', description: '无法加载应用列表，请检查后端连接。' })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadApps(favoritesOnly)
  }, [favoritesOnly, loadApps])

  const handleScan = async (): Promise<void> => {
    setScanning(true)
    try {
      await api.post('/apps/scan')
      message.success('扫描完成')
      await loadApps(favoritesOnly)
    } catch {
      message.error('扫描失败，请检查后端连接。')
    } finally {
      setScanning(false)
    }
  }

  const toggleFavorite = async (record: AppEntry): Promise<void> => {
    const next = !record.is_favorite
    try {
      await api.post('/apps/favorite', { id: record.id, is_favorite: next })
      setApps((prev) => prev.map((a) => (a.id === record.id ? { ...a, is_favorite: next } : a)))
      message.success(next ? '已收藏' : '已取消收藏')
    } catch {
      message.error('操作失败，请稍后重试。')
    }
  }

  const handleStart = async (record: AppEntry): Promise<void> => {
    try {
      await api.post('/apps/start', { id: record.id })
      message.success(`已启动「${record.name}」`)
    } catch {
      message.error(`启动「${record.name}」失败。`)
    }
  }

  const handleUninstall = async (record: AppEntry): Promise<void> => {
    const confirmationId = await confirmOperation('uninstall', '卸载应用', {
      name: record.name,
      path: record.path,
    })
    if (!confirmationId) return
    try {
      await api.post('/apps/uninstall', { id: record.id, confirmation_id: confirmationId })
      message.success(`已卸载「${record.name}」`)
      await loadApps(favoritesOnly)
    } catch {
      message.error(`卸载「${record.name}」失败。`)
    }
  }

  const columns: TableColumnsType<AppEntry> = [
    {
      title: '名称',
      dataIndex: 'name',
      key: 'name',
      render: (name: string, record: AppEntry) => (
        <Space>
          {record.icon_path ? (
            <img src={record.icon_path} alt="" style={{ width: 20, height: 20 }} />
          ) : (
            <AppstoreOutlined style={{ fontSize: 16 }} />
          )}
          <Typography.Text strong>{name}</Typography.Text>
        </Space>
      ),
    },
    {
      title: '路径',
      dataIndex: 'path',
      key: 'path',
      ellipsis: true,
      render: (path: string) => <span className="mono">{path}</span>,
    },
    {
      title: '收藏',
      key: 'favorite',
      width: 80,
      align: 'center',
      render: (_: unknown, record: AppEntry) => (
        <Button
          type="text"
          aria-label={record.is_favorite ? '取消收藏' : '收藏'}
          icon={
            record.is_favorite ? (
              <StarFilled style={{ color: '#faad14' }} />
            ) : (
              <StarOutlined />
            )
          }
          onClick={() => void toggleFavorite(record)}
        />
      ),
    },
    {
      title: '操作',
      key: 'actions',
      width: 200,
      render: (_: unknown, record: AppEntry) => (
        <Space>
          <Button
            type="primary"
            size="small"
            icon={<PlayCircleOutlined />}
            onClick={() => void handleStart(record)}
          >
            启动
          </Button>
          <Button
            danger
            size="small"
            icon={<DeleteOutlined />}
            onClick={() => void handleUninstall(record)}
          >
            卸载
          </Button>
        </Space>
      ),
    },
  ]

  return (
    <div>
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 16,
        }}
      >
        <Typography.Title level={4} style={{ margin: 0 }}>
          应用管理
        </Typography.Title>
        <Space size="middle">
          <Space>
            <Typography.Text>仅显示收藏</Typography.Text>
            <Switch
              checked={favoritesOnly}
              onChange={(checked) => setFavoritesOnly(checked)}
            />
          </Space>
          <Button
            type="primary"
            icon={<ScanOutlined />}
            loading={scanning}
            onClick={() => void handleScan()}
          >
            扫描应用
          </Button>
        </Space>
      </div>

      {loading ? (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin size="large" />
        </div>
      ) : apps.length === 0 ? (
        <Empty description={favoritesOnly ? '暂无收藏的应用' : '暂无应用，点击「扫描应用」开始'} />
      ) : (
        <Table<AppEntry>
          rowKey="id"
          columns={columns}
          dataSource={apps}
          pagination={false}
          scroll={{ y: 'calc(100vh - 220px)' }}
          expandable={{
            expandedRowRender: (record: AppEntry) => (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                最近启动：{formatDateTime(record.last_launched)}
                {'  ·  启动次数：'}
                {record.launch_count}
              </Typography.Text>
            ),
          }}
        />
      )}
    </div>
  )
}
