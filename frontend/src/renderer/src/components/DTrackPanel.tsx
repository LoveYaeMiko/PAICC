import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button, Card, Col, Empty, Row, Space, Spin, Statistic, Table, Tag, Typography } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import type { TableColumnsType } from 'antd'
import type { EChartsOption } from 'echarts'
import EChart from '@/components/EChart'
import { api } from '@/services/api'
import type { ShadowAccountConfig, ShadowStatus } from '@/types'

/**
 * Single-track D panel (FQA ``shadow.accounts`` only carries ``D_5W`` since
 * A/B/C retired on 2026-09-08). Everything here is scoped to one account:
 * strategy tags, the real-time intraday trader card, the 14:50 pre-close order
 * list, headline metrics, equity / drawdown charts, target positions and the
 * full fill history.
 */

interface AccountStatus {
  name: string
  status?: ShadowStatus
  autopilot?: { mode?: string; gross_scale?: number; reason?: string }
}

/** Real-time intraday trader status (``outputs/live_<account>.json``). */
interface LivePosition {
  symbol: string
  shares: number
  last: number
  entry?: number | null
  stop?: number | null
  pnl?: number | null
  pnl_pct?: number | null
}

interface LiveStatus {
  account?: string
  ts?: string
  equity_live?: number
  cash?: number
  invested_pct?: number
  positions?: LivePosition[]
}

/** 14:50 pre-close order list (``outputs/preclose_orders_<account>.json``). */
interface PrecloseOrder {
  symbol: string
  side: string
  shares: number
}

interface PreclosePayload {
  date?: string
  ts?: string
  orders?: PrecloseOrder[]
  note?: string
  stale?: boolean
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
  /** 卖出笔的移动加权买入价（FQA 成本口径）；买入笔为 null */
  entry_price?: number | null
}

interface PositionRow {
  symbol: string
  shares: number
  weight: number
  side: string
  last_price?: number | null
  entry_price?: number | null
  pnl?: number | null
  pnl_pct?: number | null
  days_held?: number | null
}

const MODE_COLOR: Record<string, string> = { normal: 'green', de_risk: 'orange', halt: 'red' }
const MODE_LABEL: Record<string, string> = { normal: '正常', de_risk: '收缩', halt: '停止' }

/** Live payload is stale when it was not refreshed within the last 10 minutes. */
const LIVE_STALE_MS = 10 * 60 * 1000

function formatRatio(v: number | null | undefined): string {
  if (v == null) return '—'
  return `${(v * 100).toFixed(2)}%`
}

function formatShares(v: number | null | undefined): string {
  if (v == null) return '—'
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

function formatPrice(v: number | null | undefined): string {
  if (v == null) return '—'
  return v.toFixed(2)
}

function pnlColor(v: number | null | undefined): string | undefined {
  if (v == null) return undefined
  return v >= 0 ? '#ff4d4f' : '#52c41a'
}

/** ``time`` is ``HH:MM:SS`` for intraday fills and ``''`` for auction fills. */
function formatFillTime(v: string | null | undefined): string {
  if (!v) return '收盘'
  return v.length > 5 ? v.slice(0, 5) : v
}

function parseTs(v: string | null | undefined): number {
  if (!v) return NaN
  return new Date(v.replace(' ', 'T')).getTime()
}

function pullbackParams(cfg?: ShadowAccountConfig): Record<string, unknown> {
  const pb = cfg?.pullback
  return pb && typeof pb === 'object' ? (pb as Record<string, unknown>) : {}
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

const livePositionColumns: TableColumnsType<LivePosition> = [
  { title: '标的', dataIndex: 'symbol', key: 'symbol', className: 'mono' },
  {
    title: '股数',
    dataIndex: 'shares',
    key: 'shares',
    align: 'right',
    render: (v: number) => formatShares(v),
  },
  {
    title: '买入价',
    dataIndex: 'entry',
    key: 'entry',
    align: 'right',
    render: (v: number | null) => formatPrice(v),
  },
  {
    title: '现价',
    dataIndex: 'last',
    key: 'last',
    align: 'right',
    render: (v: number) => formatPrice(v),
  },
  {
    title: '止损价',
    dataIndex: 'stop',
    key: 'stop',
    align: 'right',
    render: (v: number | null) => formatPrice(v),
  },
  {
    title: '盈亏',
    dataIndex: 'pnl',
    key: 'pnl',
    align: 'right',
    render: (v: number | null) =>
      v == null ? '—' : <span style={{ color: pnlColor(v) }}>{v.toFixed(2)}</span>,
  },
  {
    title: '盈亏%',
    dataIndex: 'pnl_pct',
    key: 'pnl_pct',
    align: 'right',
    render: (v: number | null) =>
      v == null ? '—' : <span style={{ color: pnlColor(v) }}>{`${v.toFixed(2)}%`}</span>,
  },
]

const precloseColumns: TableColumnsType<PrecloseOrder> = [
  { title: '代码', dataIndex: 'symbol', key: 'symbol', className: 'mono' },
  {
    title: '方向',
    dataIndex: 'side',
    key: 'side',
    width: 80,
    render: (v: string) => (
      <Tag color={String(v).toLowerCase() === 'sell' ? 'green' : 'red'}>
        {String(v).toLowerCase() === 'sell' ? '卖' : '买'}
      </Tag>
    ),
  },
  {
    title: '股数',
    dataIndex: 'shares',
    key: 'shares',
    align: 'right',
    render: (v: number) => formatShares(v),
  },
]

const positionColumns: TableColumnsType<PositionRow> = [
  { title: '标的', dataIndex: 'symbol', key: 'symbol', className: 'mono' },
  {
    title: '股数',
    dataIndex: 'shares',
    key: 'shares',
    align: 'right',
    render: (v: number) => formatShares(v),
  },
  {
    title: '买入价',
    dataIndex: 'entry_price',
    key: 'entry_price',
    align: 'right',
    render: (v: number | null) => formatPrice(v),
  },
  {
    title: '现价',
    dataIndex: 'last_price',
    key: 'last_price',
    align: 'right',
    render: (v: number | null) => formatPrice(v),
  },
  {
    title: '权重',
    dataIndex: 'weight',
    key: 'weight',
    align: 'right',
    render: (v: number) => formatRatio(v),
  },
  {
    title: '盈亏%',
    dataIndex: 'pnl_pct',
    key: 'pnl_pct',
    align: 'right',
    render: (v: number | null) =>
      v == null ? '—' : <span style={{ color: pnlColor(v) }}>{formatRatio(v)}</span>,
  },
  {
    title: '持有天数',
    dataIndex: 'days_held',
    key: 'days_held',
    align: 'right',
    width: 90,
    render: (v: number | null) => (v == null ? '—' : String(v)),
  },
]

const tradeColumns: TableColumnsType<TradeRecord> = [
  { title: '日期', dataIndex: 'date', key: 'date', width: 100 },
  {
    title: '时间',
    dataIndex: 'time',
    key: 'time',
    width: 70,
    className: 'mono',
    render: (v: string | null) => formatFillTime(v),
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
    title: '买入价',
    dataIndex: 'entry_price',
    key: 'entry_price',
    align: 'right',
    render: (v: number | null, r) => (r.side === 'sell' && v != null ? v.toFixed(2) : '—'),
  },
  {
    title: '股数',
    dataIndex: 'shares',
    key: 'shares',
    align: 'right',
    render: (v: number) => formatShares(v),
  },
  {
    title: '成交价',
    dataIndex: 'price',
    key: 'price',
    align: 'right',
    render: (v: number) => formatPrice(v),
  },
  {
    title: '佣金',
    dataIndex: 'commission',
    key: 'commission',
    align: 'right',
    render: (v: number) => formatPrice(v),
  },
  {
    title: '金额',
    dataIndex: 'notional',
    key: 'notional',
    align: 'right',
    render: (v: number) => formatShares(v),
  },
]

interface Props {
  /** Bump to re-fetch everything (e.g. after a manual daily-close run). */
  refreshKey?: number
}

/** D-track (single account) panel: strategy tags, live P&L, pre-close orders, ledger. */
export default function DTrackPanel({ refreshKey = 0 }: Props): JSX.Element {
  const [accounts, setAccounts] = useState<AccountStatus[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [trades, setTrades] = useState<TradeRecord[]>([])
  const [tradesLoading, setTradesLoading] = useState(true)

  const [live, setLive] = useState<LiveStatus | null>(null)
  const [preclose, setPreclose] = useState<PreclosePayload | null>(null)
  const [precloseError, setPrecloseError] = useState(false)

  // Kept in state so the 实时/非实时 tag re-evaluates even when the live
  // payload itself does not change between polls.
  const [now, setNow] = useState(() => Date.now())

  const account = useMemo<AccountStatus | null>(() => {
    const list = accounts.filter((a) => a.status || a.autopilot)
    if (!list.length) return null
    return list.find((a) => a.name === 'D_5W') ?? list[0]
  }, [accounts])
  const accountName = account?.name ?? ''

  const load = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<Record<string, AccountStatus>>('/quant/accounts')
      setAccounts(Object.values(data ?? {}))
      setError(null)
    } catch (err) {
      setAccounts([])
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load, refreshKey])

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(timer)
  }, [])

  // D track: real-time intraday trader (``cli.py live``) — minute precision.
  useEffect(() => {
    if (!accountName) {
      setLive(null)
      return undefined
    }
    let cancelled = false
    const poll = (): void => {
      api
        .get<LiveStatus | null>('/quant/live', { params: { account: accountName } })
        .then((r) => {
          if (!cancelled) setLive(r.data ?? null)
        })
        .catch(() => {
          if (!cancelled) setLive(null)
        })
    }
    poll()
    const timer = window.setInterval(poll, 15_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [accountName])

  // 14:50 pre-close order list; degrades silently when the endpoint is absent.
  useEffect(() => {
    if (!accountName) {
      setPreclose(null)
      setPrecloseError(false)
      return undefined
    }
    let cancelled = false
    const poll = (): void => {
      api
        .get<PreclosePayload>('/quant/preclose', { params: { account: accountName } })
        .then((r) => {
          if (cancelled) return
          setPreclose(r.data ?? null)
          setPrecloseError(false)
        })
        .catch(() => {
          if (cancelled) return
          setPreclose(null)
          setPrecloseError(true)
        })
    }
    poll()
    const timer = window.setInterval(poll, 30_000)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [accountName])

  useEffect(() => {
    if (!accountName) {
      setTrades([])
      setTradesLoading(false)
      return undefined
    }
    let cancelled = false
    setTradesLoading(true)
    api
      .get<{ fills: TradeRecord[] }>('/quant/trades', {
        params: { account: accountName, limit: 200 },
      })
      .then((r) => {
        if (!cancelled) setTrades(r.data?.fills ?? [])
      })
      .catch(() => {
        if (!cancelled) setTrades([])
      })
      .finally(() => {
        if (!cancelled) setTradesLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [accountName, refreshKey])

  const refreshButton = (
    <Button size="small" icon={<ReloadOutlined />} onClick={() => void load()}>
      刷新
    </Button>
  )

  if (loading) {
    return (
      <Card size="small" title="D 轨">
        <Spin />
      </Card>
    )
  }

  if (error != null) {
    return (
      <Card size="small" title="D 轨" extra={refreshButton}>
        <Typography.Text type="danger">D 轨状态加载失败：{error}</Typography.Text>
      </Card>
    )
  }

  if (!account) {
    return (
      <Card size="small" title="D 轨" extra={refreshButton}>
        <Empty description="暂无 D 轨状态 — 运行影子模式后显示" image={Empty.PRESENTED_IMAGE_SIMPLE} />
      </Card>
    )
  }

  const st = account.status
  const eq = st?.equity ?? ({} as ShadowStatus['equity'])
  const cfg = st?.account_config
  const pb = pullbackParams(cfg)
  const rankSource = String(pb.rank_source ?? 'ml')
  const slots = pb.k == null ? '—' : String(pb.k)
  const mode = account.autopilot?.mode ?? 'normal'
  const gross = account.autopilot?.gross_scale ?? 1
  const deployment = st?.deployment

  const liveTs = parseTs(live?.ts)
  const liveStale = Number.isNaN(liveTs) || now - liveTs > LIVE_STALE_MS

  const equitySeries = st?.equity_curve ?? []
  const positions = st?.positions ?? []

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Card
        size="small"
        title={`D 轨 · ${account.name}`}
        extra={
          <Space size={8}>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              数据截至 {st?.last_trading_date ?? '—'}
            </Typography.Text>
            {refreshButton}
          </Space>
        }
      >
        <Space size={8} wrap>
          <Tag color="volcano">信号 回撤策略·{rankSource === 'ml' ? 'ML 扫描' : '动量扫描'}</Tag>
          <Tag color="geekblue">截面 {cfg?.universe ?? '—'}</Tag>
          <Tag color="purple">槽位 {slots}</Tag>
          <Tag color="cyan">触发约定 分钟收盘确认</Tag>
          <Tag color="blue">开盘 30 分钟豁免</Tag>
          <Tag color="gold">满仓化</Tag>
          <Tag color={MODE_COLOR[mode] ?? 'default'}>风险闸门 {MODE_LABEL[mode] ?? mode}</Tag>
          <Tag>总敞口 ×{gross}</Tag>
          {deployment && deployment.simulated_only !== false ? (
            <Tag color="default">模拟盘（未接入实盘资金）</Tag>
          ) : null}
          {account.autopilot?.reason ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {account.autopilot.reason}
            </Typography.Text>
          ) : null}
        </Space>
      </Card>

      <Card
        size="small"
        title={
          <Space size={8} wrap>
            <span>实时盘中</span>
            <Tag color={liveStale ? 'default' : 'processing'}>{liveStale ? '非实时（休市/离线）' : '实时'}</Tag>
            <Typography.Text type="secondary" style={{ fontSize: 11 }}>
              更新于 {live?.ts ?? '—'}
            </Typography.Text>
          </Space>
        }
      >
        {live == null ? (
          <Empty description="今日无实时数据（休市或实时交易未启动）" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Row gutter={[12, 12]}>
              <Col xs={12} sm={6} md={4}>
                <Statistic title="实时权益" value={live.equity_live ?? 0} precision={2} />
              </Col>
              <Col xs={12} sm={6} md={4}>
                <Statistic title="实时现金" value={live.cash ?? 0} precision={2} />
              </Col>
              <Col xs={12} sm={6} md={4}>
                <Statistic title="仓位占比" value={live.invested_pct ?? 0} precision={1} suffix="%" />
              </Col>
              <Col xs={12} sm={6} md={4}>
                <Statistic title="实时持仓数" value={live.positions?.length ?? 0} />
              </Col>
            </Row>
            <Table<LivePosition>
              rowKey={(r) => r.symbol}
              columns={livePositionColumns}
              dataSource={live.positions ?? []}
              size="small"
              pagination={false}
              scroll={{ y: 220 }}
              locale={{ emptyText: '暂无持仓' }}
            />
          </Space>
        )}
      </Card>

      <Card
        size="small"
        title={
          <Space size={8} wrap>
            <span>今日收盘竞价委托</span>
            {preclose?.date ? <Tag color="blue">{preclose.date}</Tag> : null}
            {preclose?.ts ? (
              <Typography.Text type="secondary" style={{ fontSize: 11 }}>
                决定时刻 {preclose.ts}
              </Typography.Text>
            ) : null}
            {preclose?.stale ? <Tag color="orange">非今日</Tag> : null}
          </Space>
        }
      >
        {precloseError ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            委托接口不可用
          </Typography.Text>
        ) : preclose == null ? (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            委托接口不可用
          </Typography.Text>
        ) : (preclose.orders ?? []).length === 0 ? (
          <Empty description="今日无委托" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Space direction="vertical" size={8} style={{ width: '100%' }}>
            <Table<PrecloseOrder>
              rowKey={(r) => `${r.symbol}-${r.side}`}
              columns={precloseColumns}
              dataSource={preclose.orders ?? []}
              size="small"
              pagination={false}
              scroll={{ y: 240 }}
            />
            {preclose.note ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {preclose.note}
              </Typography.Text>
            ) : null}
          </Space>
        )}
      </Card>

      <Card size="small" title="关键指标">
        <Row gutter={[12, 12]}>
          <Col xs={12} sm={8} md={4}>
            <Statistic title="净值" value={eq.latest ?? 0} precision={2} />
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
      </Card>

      {equitySeries.length ? (
        <Row gutter={[12, 12]}>
          <Col xs={24} lg={12}>
            <Card size="small" title="净值 vs HS300 基准">
              <EChart option={buildEquityOption(st as ShadowStatus)} height={260} />
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card size="small" title="回撤 + 超额收益">
              <EChart option={buildDrawdownExcessOption(st as ShadowStatus)} height={260} />
            </Card>
          </Col>
        </Row>
      ) : null}

      <Card size="small" title="当前持仓（目标持仓）">
        {positions.length === 0 ? (
          <Empty description="暂无持仓" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Table<PositionRow>
            rowKey={(r) => r.symbol}
            columns={positionColumns}
            dataSource={positions}
            size="small"
            pagination={false}
            scroll={{ y: 240 }}
          />
        )}
      </Card>

      <Card size="small" title="成交记录">
        {tradesLoading ? (
          <Spin />
        ) : trades.length === 0 ? (
          <Empty description="暂无成交记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />
        ) : (
          <Table<TradeRecord>
            rowKey={(r) => r.seq}
            columns={tradeColumns}
            dataSource={trades}
            size="small"
            pagination={{ pageSize: 20, size: 'small', showSizeChanger: false }}
          />
        )}
      </Card>
    </Space>
  )
}
