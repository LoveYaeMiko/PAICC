import { useEffect, useState } from 'react'
import { Card, Col, Descriptions, Empty, Row, Space, Spin, Statistic, Table, Tabs, Tag, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { api } from '@/services/api'

/** One dual-capital shadow account (FQA shadow.accounts). */
interface AccountStatus {
  name: string
  status?: {
    equity?: {
      latest?: number
      final_cash?: number
      total_return?: number
      annualized_return?: number
      sharpe?: number
      max_drawdown?: number
      n_days?: number
      n_fills?: number
      total_commission?: number
    }
    last_trading_date?: string
    red_lines?: { name: string; label: string; level: string; value: unknown; detail: string }[]
  }
  autopilot?: { mode?: string; gross_scale?: number; factor_decayed?: boolean }
}

interface TradeRecord {
  seq: number
  date: string
  symbol: string
  side: string
  shares: number
  price: number
  commission: number
  notional: number
}

const tradeColumns: TableColumnsType<TradeRecord> = [
  { title: '日期', dataIndex: 'date', key: 'date', width: 110 },
  { title: '代码', dataIndex: 'symbol', key: 'symbol', className: 'mono' },
  {
    title: '方向',
    dataIndex: 'side',
    key: 'side',
    width: 70,
    render: (v: string) => <Tag color={v === 'buy' ? 'red' : 'green'}>{v === 'buy' ? '买' : '卖'}</Tag>,
  },
  {
    title: '股数',
    dataIndex: 'shares',
    key: 'shares',
    align: 'right',
    render: (v: number) => (v == null ? '—' : v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })),
  },
  { title: '价格', dataIndex: 'price', key: 'price', align: 'right', render: (v: number) => v?.toFixed(2) },
  { title: '佣金', dataIndex: 'commission', key: 'commission', align: 'right', render: (v: number) => v?.toFixed(2) },
  {
    title: '金额',
    dataIndex: 'notional',
    key: 'notional',
    align: 'right',
    render: (v: number) => (v == null ? '—' : v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })),
  },
]

function AccountTab({ account }: { account: AccountStatus }): JSX.Element {
  const [trades, setTrades] = useState<TradeRecord[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    api
      .get<{ fills: TradeRecord[] }>('/quant/trades', { params: { account: account.name, limit: 50 } })
      .then((r) => setTrades(r.data.fills ?? []))
      .catch(() => setTrades([]))
      .finally(() => setLoading(false))
  }, [account.name])

  const eq = account.status?.equity ?? {}
  const mode = account.autopilot?.mode ?? 'normal'
  const modeColor = mode === 'normal' ? 'green' : mode === 'de_risk' ? 'orange' : 'red'

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Space size={12} wrap>
        <Tag color={modeColor}>档位 {mode}</Tag>
        <Tag>总敞口 ×{account.autopilot?.gross_scale ?? 1}</Tag>
        {account.autopilot?.factor_decayed ? <Tag color="warning">因子衰减</Tag> : null}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          数据截至 {account.status?.last_trading_date ?? '—'}
        </Typography.Text>
      </Space>
      <Row gutter={[12, 12]}>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="最新净值" value={eq.latest ?? 0} precision={2} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="累计收益" value={eq.total_return ?? 0} precision={4} suffix="%" formatter={(v) => `${(Number(v) * 100).toFixed(2)}%`} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="Sharpe" value={eq.sharpe ?? 0} precision={2} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="最大回撤" value={eq.max_drawdown ?? 0} precision={4} suffix="%" formatter={(v) => `${(Number(v) * 100).toFixed(2)}%`} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="累计成本" value={eq.total_commission ?? 0} precision={2} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="成交笔数" value={eq.n_fills ?? 0} />
        </Col>
      </Row>
      {(account.status?.red_lines ?? []).length > 0 ? (
        <Descriptions size="small" column={2} bordered>
          {account.status?.red_lines?.map((rl) => (
            <Descriptions.Item key={rl.name} label={rl.label}>
              <Tag color={rl.level === 'ok' ? 'green' : rl.level === 'warning' ? 'orange' : 'red'}>
                {String(rl.value ?? '—')}
              </Tag>
            </Descriptions.Item>
          ))}
        </Descriptions>
      ) : null}
      <Typography.Text strong>最近成交（详细交易记录）</Typography.Text>
      {loading ? (
        <Spin />
      ) : trades.length === 0 ? (
        <Empty description="暂无成交记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Table<TradeRecord>
          rowKey={(r) => r.seq}
          columns={tradeColumns}
          dataSource={trades}
          size="small"
          pagination={false}
          scroll={{ y: 240 }}
        />
      )}
    </Space>
  )
}

/** Dual-capital shadow panel — one tab per account, each with full status + trades. */
export default function DualShadowPanel(): JSX.Element {
  const [accounts, setAccounts] = useState<AccountStatus[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = (): void => {
    setLoading(true)
    api
      .get<Record<string, AccountStatus>>('/quant/accounts')
      .then((r) => {
        const list = Object.values(r.data ?? {})
        setAccounts(list.filter((a) => a.status || a.autopilot))
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  if (loading) return <Spin />
  if (error) return <Typography.Text type="danger">双轨状态加载失败: {error}</Typography.Text>
  if (accounts.length === 0) {
    return (
      <Card size="small" title="双资金轨影子盘">
        <Empty description="暂无双轨状态 — 运行 `python cli.py shadow` 后显示" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </Card>
    )
  }
  return (
    <Card size="small" title={`双资金轨影子盘（${accounts.length} 账户）`}>
      <Tabs
        items={accounts.map((a) => ({
          key: a.name,
          label: a.name,
          children: <AccountTab account={a} />,
        }))}
      />
    </Card>
  )
}
