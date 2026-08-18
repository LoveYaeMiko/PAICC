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
  value: number | null
  threshold: number | null
  detail: string
}

export interface RedLineStatus {
  timestamp: number
  overall: RedLineLevel
  red_lines: RedLine[]
  source: string
}

export interface QuantProcessInfo {
  pid: number
  cmdline: string
  cpu_percent: number
  memory_percent: number
  running_time: number
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
