import {
  AppstoreOutlined,
  BookOutlined,
  CodeOutlined,
  CommentOutlined,
  DashboardOutlined,
  FileSearchOutlined,
  LineChartOutlined,
  PieChartOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { Badge, Button, Layout, Menu, Tag, notification } from 'antd'
import { useEffect, useMemo } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { api } from '@/services/api'
import { useWsEvent } from '@/services/ws'
import { useBackendStore } from '@/store/useBackendStore'
import ConfirmDialog from '@/components/ConfirmDialog'

const { Sider, Header, Content } = Layout

const MENU_ITEMS = [
  { key: '/', icon: <DashboardOutlined />, label: '系统监控' },
  { key: '/files', icon: <FileSearchOutlined />, label: '文件搜索' },
  { key: '/apps', icon: <AppstoreOutlined />, label: '应用管理' },
  { key: '/storage', icon: <PieChartOutlined />, label: '存储分析' },
  { key: '/quant', icon: <LineChartOutlined />, label: '量化面板' },
  { key: '/research', icon: <BookOutlined />, label: '研究台' },
  { key: '/chat', icon: <CommentOutlined />, label: 'AI 助手' },
  { key: '/claude', icon: <CodeOutlined />, label: 'Claude Code' },
  { key: '/settings', icon: <SettingOutlined />, label: '设置' },
]

export default function MainLayout(): JSX.Element {
  const navigate = useNavigate()
  const location = useLocation()
  const { ready, setStatus } = useBackendStore()
  const [api2, contextHolder] = notification.useNotification()

  const selectedKey = useMemo(() => {
    const match = MENU_ITEMS.filter((m) => m.key !== '/').find((m) =>
      location.pathname.startsWith(m.key),
    )
    return match?.key ?? '/'
  }, [location.pathname])

  useEffect(() => {
    const poll = async (): Promise<void> => {
      try {
        await api.get('/health')
        setStatus({ ready: true, url: '' })
      } catch {
        setStatus({ ready: false, url: '' })
      }
    }
    poll()
    const id = window.setInterval(poll, 5000)
    return () => window.clearInterval(id)
  }, [setStatus])

  useEffect(() => {
    window.paicc?.onNavigate((route) => navigate(route))
  }, [navigate])

  // Proactive notifications from the backend (red lines, low disk, high temp…).
  const openAIAnalysis = (d: { title?: string; message?: string; names?: string[] }): void => {
    const names = Array.isArray(d.names) && d.names.length > 0 ? d.names : []
    const label = names.length > 0 ? names.join('、') : (d.title ?? '量化红线')
    navigate('/chat', { state: { prefill: `红线 ${label} 触发，帮我分析并给出处理建议` } })
  }

  useWsEvent('red_line_alert', (data) => {
    const d = data as { title?: string; message?: string; names?: string[] }
    api2.warning({
      message: d.title ?? '量化红线触发',
      description: d.message ?? '',
      btn: (
        <Button size="small" type="primary" onClick={() => openAIAnalysis(d)}>
          让 AI 分析
        </Button>
      ),
    })
  })
  useWsEvent('system_alert', (data) => {
    const d = data as { title?: string; message?: string }
    api2.warning({ message: d.title ?? '系统提醒', description: d.message ?? '' })
  })

  return (
    <Layout style={{ height: '100vh' }}>
      {contextHolder}
      <Sider width={190} theme="dark" style={{ borderRight: '1px solid #262b36' }}>
        <div
          style={{
            height: 56,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontSize: 18,
            fontWeight: 700,
            color: '#4a90e2',
            letterSpacing: 1,
          }}
        >
          PAICC
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={MENU_ITEMS}
          onClick={({ key }) => navigate(key)}
          style={{ background: 'transparent' }}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            height: 56,
            padding: '0 24px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            background: '#171a21',
            borderBottom: '1px solid #262b36',
          }}
        >
          <div style={{ fontWeight: 600, fontSize: 16 }}>
            Personal AI Command Center
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <Badge status={ready ? 'success' : 'error'} />
            <Tag color={ready ? 'green' : 'red'}>{ready ? '后端在线' : '后端离线'}</Tag>
          </div>
        </Header>
        <Content style={{ overflow: 'auto', padding: 20, background: '#0f1115' }}>
          <Outlet />
        </Content>
      </Layout>
      <ConfirmDialog />
    </Layout>
  )
}
