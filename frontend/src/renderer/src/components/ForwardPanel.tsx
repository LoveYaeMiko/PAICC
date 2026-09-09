import { useCallback, useEffect, useState } from 'react'
import { Alert, Card, Col, Descriptions, Row, Space, Spin, Table, Tag, Tooltip, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { api } from '@/services/api'
import type { ForwardGateCheck, ForwardState } from '@/types'

/**
 * Forward-period panel (``/quant/forward``).
 *
 * The forward window does NOT test alpha — with ``SE(Sharpe)=√(252/N)`` a
 * Sharpe of 1.0 needs ~6.3 years to reach t=2.5 (25 years for 0.5) while regime
 * matching decays in 1-3 years (FQA ``docs/FORWARD_PROTOCOL.md`` §1.1). It tests
 * the PIPELINE: tracking error, cost calibration, legal violations, availability,
 * data freshness and symbol coverage. A gate that could not be measured FAILS —
 * "we did not measure it" must never read as "it is fine".
 */

/** id → (中文名, 单位, 阈值展示). */
const GATE_META: Record<string, { label: string; unit?: string; hint: string }> = {
  tracking_error_daily_pp: {
    label: '跟踪误差',
    unit: 'pp/日',
    hint: '逐日 |实录 − 重放|：复制生产账本到窗口前状态，用当前数据重跑同一段规则',
  },
  tracking_error_sign_bias: {
    label: '跟踪误差符号偏差',
    unit: 'p',
    hint: '差值正负的精确二项检验；系统性单边说明管线有偏',
  },
  cost_fee_deviation: { label: '费用偏差', unit: '%', hint: '账本计费 vs 预注册成本规格' },
  cost_price_integrity: {
    label: '成交价完整性',
    unit: 'bp',
    hint: '成交价 vs 市场参考价（收盘/拍卖单用当日收盘价，实时单用同分钟成交价）',
  },
  violations: { label: '违规计数', unit: '笔', hint: 'T+1 / 整手 / 最小变动价位 / 涨跌停 / 停牌' },
  availability: { label: '实时层可用率', hint: '决策窗口内心跳覆盖（间隔 ≤5 分钟）' },
  data_freshness: { label: '数据新鲜度', unit: '天', hint: '行情截止日 vs 账本最后交易日' },
  symbol_minute_coverage: { label: '符号级分钟覆盖', hint: '按每个符号自身可交易日为分母' },
}

const SOFT_LABEL: Record<string, string> = {
  sharpe: 'Sharpe',
  sharpe_standard_error: 'Sharpe 标准误',
  max_drawdown: '最大回撤',
  total_return: '累计收益',
  excess_return: '超额',
  hit_rate: '胜率',
  n_fills: '成交笔数',
  turnover: '换手',
}

function verdictTag(v: string | null | undefined): JSX.Element {
  if (v === 'pass') return <Tag color="green">通过</Tag>
  if (v === 'fail') return <Tag color="red">未通过</Tag>
  return <Tag>未评估</Tag>
}

function fmt(v: unknown, unit?: string): string {
  if (v == null) return '—'
  if (typeof v === 'number') {
    const s = Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(2)
    return unit ? `${s}${unit}` : s
  }
  if (Array.isArray(v)) return v.length ? v.join('、') : '无'
  return String(v)
}

function checkValue(key: string, c: ForwardGateCheck): string {
  switch (key) {
    case 'tracking_error_daily_pp':
      return fmt(c.value, 'pp/日') + (c.n_days != null ? `（${c.n_days} 日）` : '')
    case 'tracking_error_sign_bias':
      return fmt(c.value, '') + (c.n_pos != null ? `（+${c.n_pos}/−${c.n_neg}）` : '')
    case 'cost_fee_deviation':
      return fmt(c.value_pct, '%')
    case 'cost_price_integrity':
      return fmt(c.mean_abs_bps, 'bp') + (c.n_price_checked != null ? `（${c.n_price_checked} 笔）` : '')
    case 'violations':
      return fmt(c.value, '笔')
    case 'availability':
      return c.value == null ? '未测量' : `${(Number(c.value) * 100).toFixed(2)}%`
    case 'data_freshness':
      return fmt(c.value_days, ' 天')
    case 'symbol_minute_coverage':
      return c.value == null ? '未测量' : `${(Number(c.value) * 100).toFixed(1)}%`
    default:
      return fmt(c.value ?? c.value_pct ?? c.mean_abs_bps, '')
  }
}

function checkThreshold(key: string, c: ForwardGateCheck): string {
  switch (key) {
    case 'tracking_error_daily_pp':
      return `≤ ${c.max} pp/日，且 ≥ ${c.min_days} 日`
    case 'tracking_error_sign_bias':
      return `p ≥ ${c.min}`
    case 'cost_fee_deviation':
      return `|偏差| ≤ ${c.max_pct}%`
    case 'cost_price_integrity':
      return `|偏差| ≤ ${c.limit_bps} bp`
    case 'violations':
      return `= ${c.max}`
    case 'availability':
      return `≥ ${((c.min ?? 0) * 100).toFixed(0)}%`
    case 'data_freshness':
      return `≤ ${c.max_days} 天`
    case 'symbol_minute_coverage':
      return `≥ ${((c.min ?? 0) * 100).toFixed(0)}%`
    default:
      return '—'
  }
}

const gateColumns: TableColumnsType<[string, ForwardGateCheck]> = [
  {
    title: '硬闸门',
    key: 'label',
    width: 150,
    render: (_, [key]) => {
      const meta = GATE_META[key]
      return meta ? (
        <Tooltip title={meta.hint}>
          <span>{meta.label}</span>
        </Tooltip>
      ) : (
        <span>{key}</span>
      )
    },
  },
  { title: '实测', key: 'value', width: 150, render: (_, [k, c]) => checkValue(k, c) },
  { title: '阈值', key: 'threshold', width: 140, render: (_, [k, c]) => checkThreshold(k, c) },
  {
    title: '状态',
    key: 'ok',
    width: 90,
    render: (_, [, c]) =>
      c.ok ? <Tag color="green">PASS</Tag> : <Tag color="red">FAIL</Tag>,
  },
]

export default function ForwardPanel(): JSX.Element {
  const [state, setState] = useState<ForwardState | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<ForwardState>('/quant/forward')
      setState(data)
      setError(null)
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail
      setError(detail || (err as Error).message || '读取前向期工件失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
    const timer = window.setInterval(() => void refresh(), 60000)
    return () => window.clearInterval(timer)
  }, [refresh])

  const health = state?.health ?? null
  const paired = state?.paired?.paired ?? null
  const hard = health?.gate?.hard ?? {}
  const gateRows = Object.entries(hard).filter(
    (entry): entry is [string, ForwardGateCheck] => typeof entry[1] === 'object' && entry[1] !== null,
  )
  const soft = health?.gate?.soft

  return (
    <Card
      title={
        <Space size={8}>
          <span>前向期风险闸门</span>
          {verdictTag(state?.gate_verdict)}
          {health?.window ? (
            <Typography.Text type="secondary" style={{ fontWeight: 400 }}>
              {health.window.start} → {health.window.end}
            </Typography.Text>
          ) : null}
        </Space>
      }
      size="small"
      extra={
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          前向期不检验 alpha（无统计功效）· 只检验管线
        </Typography.Text>
      }
    >
      {loading ? (
        <Spin />
      ) : error ? (
        <Alert type="warning" showIcon message={error} />
      ) : !health ? (
        <Alert
          type="info"
          showIcon
          message="风险闸门尚未评估"
          description="运行 python scripts/forward_health.py（PAICC 每周六 18:30 自动执行）"
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {health.gate.failed.length ? (
            <Alert
              type="error"
              showIcon
              message={`硬闸门未通过：${health.gate.failed.join('、')}`}
              description="硬闸门失败的含义是「管线不可信」，不是「策略不行」——未测量的闸门同样判失败。"
            />
          ) : (
            <Alert type="success" showIcon message="全部硬闸门通过" />
          )}

          <Table<[string, ForwardGateCheck]>
            rowKey={([k]) => k}
            columns={gateColumns}
            dataSource={gateRows}
            size="small"
            pagination={false}
            tableLayout="fixed"
          />

          <Row gutter={16}>
            <Col span={12}>
              <Descriptions
                size="small"
                column={1}
                title="候选配对（只记录，不自动切换）"
                bordered
              >
                <Descriptions.Item label="候选">
                  {paired?.rule_id ?? '—'}
                  {paired?.candidate_params
                    ? `（ATR ${paired.candidate_params.pb_atr_mult} / [${paired.candidate_params.pb_stop_lo}, ${paired.candidate_params.pb_stop_hi}]）`
                    : ''}
                </Descriptions.Item>
                <Descriptions.Item label="配对日数">
                  {paired ? `${paired.n_days} / ${paired.window_days}` : '—'}
                </Descriptions.Item>
                <Descriptions.Item label="日相关性">{fmt(paired?.corr)}</Descriptions.Item>
                <Descriptions.Item label="日差均值">
                  {fmt(paired?.mean_diff_pp, 'pp/日')}
                </Descriptions.Item>
                <Descriptions.Item label="配对 t">{fmt(paired?.t_stat)}</Descriptions.Item>
                <Descriptions.Item label="分离所需日数">
                  {paired?.days_needed_for_t ? `${paired.days_needed_for_t} 日` : '—'}
                </Descriptions.Item>
                <Descriptions.Item label="切换规则">
                  {paired
                    ? `${paired.window_days} 个配对日：日差均值 > ${paired.diff_gt} 且 t > ${paired.t_min} → 切换`
                    : '—'}
                </Descriptions.Item>
                <Descriptions.Item label="结论">
                  {paired ? (
                    <Tag color={paired.switch ? 'orange' : 'blue'}>
                      {paired.switch ? '切换（须重新预注册）' : '保持现役'}
                    </Tag>
                  ) : (
                    '—'
                  )}
                </Descriptions.Item>
              </Descriptions>
            </Col>
            <Col span={12}>
              <Descriptions size="small" column={1} title="软指标（仅记录）" bordered>
                {Object.entries(soft?.values ?? {}).map(([k, v]) => (
                  <Descriptions.Item key={k} label={SOFT_LABEL[k] ?? k}>
                    {k === 'max_drawdown' || k === 'total_return' || k === 'excess_return'
                      ? v == null
                        ? '—'
                        : `${(Number(v) * 100).toFixed(2)}%`
                      : fmt(v)}
                  </Descriptions.Item>
                ))}
                {(state?.prereg ?? []).map((rec) => (
                  <Descriptions.Item key={rec.rule_id} label={`预注册 · ${rec.trials?.family ?? ''}`}>
                    {rec.rule_id} v{rec.version}（{rec.frozen_at?.slice(0, 10)}，
                    trial {rec.trials?.this_trial ?? '—'}）
                  </Descriptions.Item>
                ))}
              </Descriptions>
            </Col>
          </Row>

          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            工件：{health.artifact ?? '—'} · 成交 {health.n_fills} 笔 · 数据截止{' '}
            {String(health.provenance?.data_as_of ?? '—')} · commit{' '}
            {String(health.provenance?.code_commit ?? '—').slice(0, 12)}
          </Typography.Text>
        </div>
      )}
    </Card>
  )
}
