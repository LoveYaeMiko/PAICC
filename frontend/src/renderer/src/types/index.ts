// Shared TypeScript types mirroring the Python backend responses.

export interface HealthResponse {
  status: string
  version: string
  python_version: string
  uptime_seconds: number
}

export interface MemoryInfo {
  total: number
  used: number
  available: number
  percent: number
}

export interface DiskUsage {
  device: string
  mountpoint: string
  total: number
  used: number
  free: number
  percent: number
}

export interface NetInfo {
  bytes_sent: number
  bytes_recv: number
  sent_per_sec: number
  recv_per_sec: number
}

export interface DiskIOInfo {
  read_bytes: number
  write_bytes: number
  read_per_sec: number
  write_per_sec: number
}

export interface GpuInfo {
  name: string
  load: number
  temperature: number | null
  memory_used: number
  memory_total: number
}

export interface SystemStats {
  cpu_percent: number
  cpu_per_core: number[]
  cpu_count: number
  cpu_freq: number | null
  memory: MemoryInfo
  disk: DiskUsage[]
  net: NetInfo
  disk_io: DiskIOInfo
  gpu: GpuInfo[]
  uptime_seconds: number
}

export interface ProcessInfo {
  pid: number
  name: string
  username: string
  cpu_percent: number
  memory_percent: number
  memory_rss: number
  create_time: number
  exe: string
  cmdline: string
  status: string
  disk_read_bytes?: number
  disk_write_bytes?: number
}

export interface StartupItem {
  name: string
  command: string
  location: string
  source: string
  enabled: boolean
}

export interface FileSearchResult {
  name: string
  path: string
  size: number
  modified: number
  is_dir: boolean
}

export interface LargeFile {
  path: string
  size: number
  modified: number
}

export interface DuplicateGroup {
  hash: string
  size: number
  count: number
  files: string[]
}

export interface CleanableItem {
  id: string
  category: string
  path: string
  size: number
  description: string
}

export interface AppEntry {
  id: number
  name: string
  path: string
  icon_path: string
  is_favorite: boolean
  launch_count: number
  last_launched: number
}

export interface StorageReport {
  id: number
  generated_at: number
  content: string
  sent_to_email: number
}

export interface QuantProject {
  id: number
  name: string
  root_path: string
  config_file: string
  dashboard_script: string
  log_dir: string
  is_active: boolean
}

export interface QuantCommand {
  id: number
  project_id: number
  name: string
  command: string
  description: string
}

export type RedLineLevel = 'ok' | 'warning' | 'critical' | 'unknown'

export interface RedLine {
  name: string
  label?: string
  level: RedLineLevel
  value: number | boolean | null
  threshold: number | null
  detail: string
}

export interface RedLineStatus {
  timestamp: number
  overall: RedLineLevel
  red_lines: RedLine[]
  accounts: Record<string, AccountRedLineStatus>
  source: string
  last_run?: string
  as_of?: string
  data_freshness_days?: number
}

/** One account's red-line payload inside the dual-track /quant/status response. */
export interface AccountRedLineStatus {
  overall: RedLineLevel
  red_lines: RedLine[]
  last_trading_date?: string
  data_freshness_days?: number
  equity?: Partial<ShadowEquity>
  last_run?: string
  account_name?: string
}

export interface QuantProcessInfo {
  pid: number
  cmdline: string
  cpu_percent: number
  memory_percent: number
  running_time: number
}

export interface ShadowEquity {
  latest: number
  final_cash: number
  total_return: number
  annualized_return: number
  sharpe: number
  max_drawdown: number
  n_days: number
  n_fills: number
  total_commission: number
  /** Fill counts by provenance (live / replay / close / auction) — D-4. */
  fills_by_source?: Record<string, number>
}

export interface ShadowPosition {
  symbol: string
  shares: number
  weight: number
  side: string
  entry_price?: number | null
  last_price?: number | null
  pnl?: number | null
  pnl_pct?: number | null
  days_held?: number | null
}

export interface S7Params {
  amplitude: number
  zscore_threshold: number
  position_cut: number
  freeze_days: number
  slippage_bps: number
  commission_bps: number
  min_commission: number
  stamp_tax_sell_bps: number
  transfer_fee_bps: number
}

export interface ShadowRedLine {
  name: string
  value: number | boolean | null
  level?: string
  threshold?: number
  critical?: number
  detail?: string
}

export interface EquityPoint {
  date: string
  equity: number
  drawdown: number
}

export interface BenchmarkPoint {
  date: string
  equity: number
  drawdown: number
}

export interface ExcessPoint {
  date: string
  excess: number
}

export interface ShadowStrategy {
  beta_neutralize: boolean
  beta_lookback: number | null
  rebalance_days: number | null
}

export interface ShadowStatus {
  as_of: string
  last_run: string
  last_trading_date: string
  data_freshness_days: number
  equity: ShadowEquity
  positions: ShadowPosition[]
  s7_params: S7Params
  refreshed: Record<string, unknown>
  red_lines: ShadowRedLine[]
  strategy?: ShadowStrategy
  account_name?: string
  account_config?: ShadowAccountConfig
  /** Deployment channel reported by FQA (observe = simulated only, no real money). */
  deployment?: {
    mode: string
    real_money_enabled: boolean
    simulated_only?: boolean
    observe_since?: string
    note?: string
  }
  equity_curve?: EquityPoint[]
  benchmark?: BenchmarkPoint[]
  excess_curve?: ExcessPoint[]
}

/** Per-account book config surfaced by FQA's shadow status (ML dual-track). */
export interface ShadowAccountConfig {
  cash: number
  alpha_source: string
  ml_artifacts: string[]
  universe: string
  long_pct: number
  short_pct: number
  rebalance_days: number
  notional_floor: number
  band_frac: number
  pullback?: Record<string, unknown>
}

export interface RedLineHistoryPoint {
  id: number
  ts: number
  source: string
  name: string
  label: string
  level: string
  value: number | string | null
  detail: string
  account?: string
}

export interface S7AmplitudeResult {
  amplitude: number
  sharpe: number
  max_drawdown: number
  total_return: number
  annualized_return: number
}

export interface S7SentimentResult {
  zscore_threshold: number
  freeze_days: number
  sharpe: number
  max_drawdown: number
  total_return: number
}

export interface S7Amplitude {
  current: number
  grid: number[]
  results: S7AmplitudeResult[]
  best: S7AmplitudeResult | null
  recommended: number
  note?: string
}

export interface S7Sentiment {
  current: { zscore_threshold: number; freeze_days: number }
  zscore_grid: number[]
  freeze_grid: number[]
  results: S7SentimentResult[]
  best: S7SentimentResult | null
  recommended: { zscore_threshold: number; freeze_days: number }
  note?: string
}

export interface S7Calibration {
  as_of: string
  last_run: string
  window: { start: string; end: string }
  cost: Record<string, unknown>
  amplitude: S7Amplitude
  sentiment: S7Sentiment
  recommendations: Record<string, unknown>
  auto_apply: boolean
  applied: { changed: Record<string, { old: string; new: string }>; unchanged: string[] }
}

/** One registered scheduler job (``/quant/schedule`` → ``jobs[]``). */
export interface QuantScheduleJob {
  id: string
  name: string
  cron: string
  next_run: string | null
  last_run: string | null
  last_status: string | null
}

export interface QuantScheduleStatus {
  shadow_daily_time: string
  calibrate_time: string
  weekly_time?: string
  shadow_auto_email: boolean
  autopilot_enabled: boolean
  scheduler_running: boolean
  last_shadow_run: Record<string, unknown> | null
  last_calibration_run: Record<string, unknown> | null
  last_autopilot_run: Record<string, unknown> | null
  last_weekly_run?: Record<string, unknown> | null
  /** Optional: older backends predate the per-job view. */
  jobs?: QuantScheduleJob[]
}

export interface AutopilotState {
  mode: string
  gross_scale: number | null
  reason: string
  since_date: string | null
  last_evaluated: string | null
  last_calibrate: string | null
  last_monitor: string | null
  last_remine?: string | null
  factor_decayed: boolean
  decay_detail?: Record<string, { recent_icir: number | null; decayed: boolean }>
  extra: Record<string, unknown>
}


export interface ResearchDoc {
  id: number
  title: string
  file_path: string
  ingested_at: number
  chunk_count: number
}

export interface Paper {
  id: number
  arxiv_id: string | null
  title: string
  authors: string
  abstract: string
  categories: string
  fields: string
  published_at: string
  url: string
  source: string
  citation_count: number
  venue: string
  score: number
  set_tag: string
  crawl_date: string
}

export interface PaperReport {
  id: number
  report_date: string
  report_type: 'daily' | 'monthly'
  content?: string
  file_path: string
  sent_to_email: number
}

export interface PaperStatus {
  papers_dir: string
  daily_time: string
  monthly_time: string
  arxiv_categories: string
  paper_count: number
  report_count: number
  last_report: { report_date: string; report_type: string; sent_to_email: number } | null
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  tool_calls?: ToolCall[]
}

export interface ToolCall {
  name: string
  arguments: Record<string, unknown>
  result?: string
}

export interface ChatResponse {
  content: string
  tool_calls: ToolCall[]
  needs_confirmation: boolean
  confirmation?: Confirmation
}

export interface Confirmation {
  confirmation_id: string
  action: string
  title: string
  details: Record<string, unknown>
  status: string
  expires_at: number
}

export interface OperationLog {
  id: number
  timestamp: number
  user: string
  action: string
  params: unknown
  result: unknown
}

export interface ClaudeEvent {
  type: string
  [key: string]: unknown
}

export interface FolderNode {
  name: string
  path: string
  size: number
  children?: FolderNode[]
}
