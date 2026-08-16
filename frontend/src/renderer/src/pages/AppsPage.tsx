import {
  DeleteOutlined,
  PlayCircleOutlined,
  ScanOutlined,
  StarFilled,
  StarOutlined,
} from '@ant-design/icons'
import { Button, Empty, message, notification, Space, Spin, Switch, Table, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { api, BACKEND_URL } from '@/services/api'
import { confirmOperation } from '@/services/confirm'
import type { AppEntry } from '@/types'

function formatDateTime(ts: number): string {
  if (!ts) return '-'
  const ms = ts > 1e12 ? ts : ts * 1000
  return new Date(ms).toLocaleString()
}

function AppIcon({ app, size = 20 }: { app: AppEntry; size?: number }): JSX.Element {
  const [failed, setFailed] = useState(false)
  if (failed) {
    return (
      <span
        style={{
          width: size,
          height: size,
          borderRadius: 4,
          background: 'var(--paicc-panel, #f0f0f0)',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: Math.round(size * 0.6),
          color: 'var(--paicc-text, #333)',
          flexShrink: 0,
        }}
      >
        {app.name.slice(0, 1)}
      </span>
    )
  }
  return (
    <img
      src={`${BACKEND_URL}/api/apps/icon/${app.id}`}
      alt=""
      width={size}
      height={size}
      style={{ width: size, height: size, borderRadius: 4, objectFit: 'contain', flexShrink: 0 }}
      onError={() => setFailed(true)}
    />
  )
}

export default function AppsPage(): JSX.Element {
  const [apps, setApps] = useState<AppEntry[]>([])
  const [loading, setLoading] = useState<boolean>(false)
  const [scanning, setScanning] = useState<boolean>(false)
  const [favoritesOnly, setFavoritesOnly] = useState<boolean>(false)
  const [recommended, setRecommended] = useState<AppEntry[]>([])

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

  const loadRecommended = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<AppEntry[]>('/apps/recommend')
      setRecommended(data)
    } catch {
      setRecommended([])
    }
  }, [])

  useEffect(() => {
    void loadApps(favoritesOnly)
  }, [favoritesOnly, loadApps])

  useEffect(() => {
    void loadRecommended()
  }, [loadRecommended])

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
          <AppIcon key={record.id} app={record} size={20} />
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

      {recommended.length > 0 && (
        <div style={{ marginBottom: 16 }}>
          <Typography.Title level={5} style={{ margin: '0 0 8px' }}>
            推荐
          </Typography.Title>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
            {recommended.map((app) => (
              <div
                key={app.id}
                onClick={() => void handleStart(app)}
                title={`启动 ${app.name}`}
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  gap: 6,
                  width: 96,
                  padding: '10px 8px',
                  border: '1px solid var(--paicc-border, #e5e5e5)',
                  borderRadius: 8,
                  cursor: 'pointer',
                  background: 'var(--paicc-panel, #fff)',
                }}
              >
                <AppIcon app={app} size={28} />
                <Typography.Text
                  ellipsis
                  style={{ maxWidth: 84, fontSize: 12, textAlign: 'center' }}
                >
                  {app.name}
                </Typography.Text>
              </div>
            ))}
          </div>
        </div>
      )}

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
