import { useEffect, useState } from 'react'
import {
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Row,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import { LineChartOutlined, ReloadOutlined } from '@ant-design/icons'
import type { TableColumnsType } from 'antd'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart'
import { api } from '@/services/api'
import type { QuantScheduleStatus, ShadowStatus } from '@/types'

/** One dual-capital shadow account (FQA shadow.accounts). */
interface AccountStatus {
  name: string
  status?: ShadowStatus
  autopilot?: { mode?: string; gross_scale?: number; factor_decayed?: boolean; reason?: string }
}

interface TradeRecord {
  seq: number
  date: string
  time?: string
  symbol: string
  side: string
  shares: number
  price: number
  commission: number
  notional: number
}

const tradeColumns: TableColumnsType<TradeRecord> = [
  { title: '日期', dataIndex: 'date', key: 'date', width: 100 },
  {
    title: '时间',
    dataIndex: 'time',
    key: 'time',
    width: 70,
    className: 'mono',
    render: (v: string) => v || '收盘',
  },
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

function formatRatio(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${(v * 100).toFixed(2)}%`
}

function buildEquityOption(status: ShadowStatus): EChartsOption {
  const eq = status.equity_curve ?? []
  const bench = status.benchmark ?? []
  const series: unknown[] = [
    {
      name: '组合净值',
      type: 'line',
      showSymbol: false,
      smooth: true,
      data: eq.map((p) => [p.date, p.equity]),
      lineStyle: { width: 2 },
    },
  ]
  if (bench.length) {
    series.push({
      name: 'HS300 基准',
      type: 'line',
      showSymbol: false,
      smooth: true,
      data: bench.map((p) => [p.date, p.equity]),
      lineStyle: { width: 1.5, type: 'dashed' },
    })
  }
  return {
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0 },
    grid: { left: 64, right: 20, top: 20, bottom: 44 },
    xAxis: { type: 'time' },
    yAxis: { type: 'value', scale: true, name: '净值' },
    series,
  } as EChartsOption
}

function buildDrawdownExcessOption(status: ShadowStatus): EChartsOption {
  const eq = status.equity_curve ?? []
  const bench = status.benchmark ?? []
  const excess = status.excess_curve ?? []
  const series: unknown[] = []
  if (eq.length) {
    series.push({
      name: '组合回撤',
      type: 'line',
      showSymbol: false,
      data: eq.map((p) => [p.date, +(p.drawdown * 100).toFixed(2)]),
      areaStyle: { opacity: 0.15 },
      yAxisIndex: 0,
    })
  }
  if (bench.length) {
    series.push({
      name: '基准回撤',
      type: 'line',
      showSymbol: false,
      data: bench.map((p) => [p.date, +(p.drawdown * 100).toFixed(2)]),
      lineStyle: { type: 'dashed' },
      yAxisIndex: 0,
    })
  }
  if (excess.length) {
    series.push({
      name: '超额收益',
      type: 'line',
      showSymbol: false,
      data: excess.map((p) => [p.date, +(p.excess * 100).toFixed(2)]),
      yAxisIndex: 1,
    })
  }
  return {
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => `${v}%` },
    legend: { bottom: 0 },
    grid: { left: 52, right: 52, top: 20, bottom: 44 },
    xAxis: { type: 'time' },
    yAxis: [
      { type: 'value', name: '回撤%', axisLabel: { formatter: '{value}%' } },
      { type: 'value', name: '超额%', axisLabel: { formatter: '{value}%' }, splitLine: { show: false } },
    ],
    series,
  } as EChartsOption
}

const positionColumns: TableColumnsType<{
  symbol: string
  shares: number
  weight: number
  side: string
  last_price?: number | null
  entry_price?: number | null
  pnl?: number | null
  pnl_pct?: number | null
}> = [
  { title: '标的', dataIndex: 'symbol', key: 'symbol', className: 'mono' },
  {
    title: '股数',
    dataIndex: 'shares',
    key: 'shares',
    align: 'right',
    render: (v: number) => (v == null ? '—' : v.toLocaleString('zh-CN')),
  },
  {
    title: '权重',
    dataIndex: 'weight',
    key: 'weight',
    align: 'right',
    render: (v: number) => formatRatio(v),
  },
  {
    title: '方向',
    dataIndex: 'side',
    key: 'side',
    width: 80,
    render: (v: string) => <Tag color={v === 'short' ? 'orange' : 'green'}>{v ?? '—'}</Tag>,
  },
  {
    title: '盈亏%',
    dataIndex: 'pnl_pct',
    key: 'pnl_pct',
    align: 'right',
    render: (v: number | null) =>
      v == null ? '—' : <span style={{ color: v >= 0 ? '#ff4d4f' : '#52c41a' }}>{formatRatio(v)}</span>,
  },
]

function AccountTab({ account }: { account: AccountStatus }): JSX.Element {
  const [trades, setTrades] = useState<TradeRecord[]>([])
  const [loading, setLoading] = useState(true)
  const st = account.status

  useEffect(() => {
    setLoading(true)
    api
      .get<{ fills: TradeRecord[] }>('/quant/trades', { params: { account: account.name, limit: 50 } })
      .then((r) => setTrades(r.data.fills ?? []))
      .catch(() => setTrades([]))
      .finally(() => setLoading(false))
  }, [account.name])

  if (!st) {
    return <Empty description="该账户暂无影子状态 — 运行影子模式后显示" image={Empty.PRESENTED_IMAGE_SIMPLE} />
  }

  const eq = st.equity ?? ({} as ShadowStatus['equity'])
  const cfg = st.account_config
  const mode = account.autopilot?.mode ?? 'normal'
  const modeColor = mode === 'normal' ? 'green' : mode === 'de_risk' ? 'orange' : 'red'
  const modeLabel = mode === 'normal' ? '正常' : mode === 'de_risk' ? '收缩' : '停止'

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Space size={12} wrap>
        <Tag color={modeColor}>档位 {modeLabel}</Tag>
        <Tag>总敞口 ×{account.autopilot?.gross_scale ?? 1}</Tag>
        {cfg ? (
          <>
            <Tag color="geekblue">截面 {cfg.universe}</Tag>
            <Tag color="purple">
              簿形 多{cfg.long_pct > 0 ? `${(cfg.long_pct * 100).toFixed(0)}%` : '—'}
              {cfg.short_pct > 0 ? `/空${(cfg.short_pct * 100).toFixed(0)}%` : '（长多）'}
            </Tag>
            <Tag color="cyan">{cfg.rebalance_days} 日调仓</Tag>
            <Tag color={cfg.notional_floor > 0 ? 'gold' : 'default'}>
              治理 {cfg.notional_floor > 0 ? `下限${cfg.notional_floor}元/带宽${(cfg.band_frac * 100).toFixed(1)}%` : '无'}
            </Tag>
            {cfg.alpha_source === 'pullback' ? (
              <Tag color="volcano">
                信号 回撤策略·{cfg.pullback && (cfg.pullback as { rank_source?: string }).rank_source === 'ml'
                  ? 'ML 扫描'
                  : '动量扫描'}
              </Tag>
            ) : (
              <Tag color="magenta">信号 ML·{cfg.ml_artifacts?.join(',') ?? '—'}</Tag>
            )}
          </>
        ) : null}
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          数据截至 {st.last_trading_date ?? '—'}
        </Typography.Text>
      </Space>
      <Row gutter={[12, 12]}>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="最新净值" value={eq.latest ?? 0} precision={2} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="累计收益" value={formatRatio(eq.total_return)} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="Sharpe" value={eq.sharpe ?? 0} precision={2} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="最大回撤" value={formatRatio(eq.max_drawdown)} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="年化收益" value={formatRatio(eq.annualized_return)} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="成交笔数" value={eq.n_fills ?? 0} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="累计成本" value={eq.total_commission ?? 0} precision={2} />
        </Col>
        <Col xs={12} sm={8} md={4}>
          <Statistic title="期末现金" value={eq.final_cash ?? 0} precision={2} />
        </Col>
      </Row>
      {st.equity_curve?.length ? (
        <Row gutter={[12, 12]}>
          <Col xs={24} lg={12}>
            <Card size="small" title="净值 vs HS300 基准">
              <EChart option={buildEquityOption(st)} height={260} />
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card size="small" title="回撤 + 超额收益">
              <EChart option={buildDrawdownExcessOption(st)} height={260} />
            </Card>
          </Col>
        </Row>
      ) : null}
      {(st.red_lines ?? []).length > 0 ? (
        <Descriptions size="small" column={2} bordered title="红线">
          {st.red_lines.map((rl) => (
            <Descriptions.Item key={rl.name} label={rl.detail ? `${rl.detail}` : rl.name}>
              <Tag color={rl.level === 'ok' ? 'green' : rl.level === 'warning' ? 'orange' : 'red'}>
                {rl.level ?? String(rl.value ?? '—')}
              </Tag>
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                {' '}
                {String(rl.value ?? '')}
              </Typography.Text>
            </Descriptions.Item>
          ))}
        </Descriptions>
      ) : null}
      <Typography.Text strong>当日目标持仓（Top 20）</Typography.Text>
      {(st.positions ?? []).length === 0 ? (
        <Empty description="暂无持仓" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Table
          rowKey={(r) => r.symbol}
          columns={positionColumns}
          dataSource={(st.positions ?? []).slice(0, 20)}
          size="small"
          pagination={false}
          scroll={{ y: 240 }}
        />
      )}
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

interface Props {
  schedule: QuantScheduleStatus | null
  runningShadow: boolean
  onRunShadow: () => void
}

/** Dual-capital shadow panel — one tab per account with full status + trades. */
export default function DualShadowPanel({ schedule, runningShadow, onRunShadow }: Props): JSX.Element {
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
        setError(null)
      })
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false))
  }

  useEffect(load, [])

  return (
    <Card
      size="small"
      title={`影子模式（长期测试 · 多资金轨${accounts.length ? ` — ${accounts.length} 账户` : ''}）`}
      extra={
        <Space size={8}>
          {schedule ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              工作日 {schedule.shadow_daily_time} · 校准周六 {schedule.calibrate_time} · 周度周日{' '}
              {schedule.weekly_time ?? '18:00'}
              {schedule.scheduler_running ? '' : '（未启动）'}
            </Typography.Text>
          ) : null}
          <Button
            size="small"
            type="primary"
            icon={<LineChartOutlined />}
            loading={runningShadow}
            onClick={onRunShadow}
          >
            立即运行
          </Button>
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()} />
        </Space>
      }
    >
      {loading ? (
        <Spin />
      ) : error ? (
        <Typography.Text type="danger">双轨状态加载失败: {error}</Typography.Text>
      ) : accounts.length === 0 ? (
        <Empty description="暂无双轨状态 — 运行影子模式后显示" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <Tabs
          items={accounts.map((a) => ({
            key: a.name,
            label: a.name,
            children: <AccountTab account={a} />,
          }))}
        />
      )}
    </Card>
  )
}
