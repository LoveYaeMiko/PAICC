import {
  Button,
  Card,
  Empty,
  Input,
  List,
  Progress,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  notification,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/services/api'
import { confirmOperation } from '@/services/confirm'
import { useWsEvent } from '@/services/ws'
import type { DuplicateGroup, FileSearchResult, FolderNode, LargeFile } from '@/types'

interface ContentHit {
  path: string
  snippet: string
}

const FILE_TYPES = [
  { value: '', label: '全部类型' },
  { value: 'image', label: '图片' },
  { value: 'video', label: '视频' },
  { value: 'audio', label: '音频' },
  { value: 'document', label: '文档' },
  { value: 'archive', label: '压缩包' },
  { value: 'executable', label: '可执行文件' },
]

const SIZE_OPTIONS = [
  { value: 0, label: '任意大小' },
  { value: 1, label: '> 1 MB' },
  { value: 10, label: '> 10 MB' },
  { value: 100, label: '> 100 MB' },
  { value: 1024, label: '> 1 GB' },
]

const THRESHOLD_OPTIONS = [
  { value: 10, label: '> 10 MB' },
  { value: 50, label: '> 50 MB' },
  { value: 100, label: '> 100 MB' },
  { value: 500, label: '> 500 MB' },
  { value: 1024, label: '> 1 GB' },
]

const TIME_OPTIONS = [
  { value: '', label: '任意时间' },
  { value: 'today', label: '今天' },
  { value: 'week', label: '近一周' },
  { value: 'month', label: '近一月' },
  { value: 'year', label: '今年' },
]

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  let value = bytes
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`
}

function formatDate(ts: number): string {
  if (!ts) return '—'
  const ms = ts < 1e12 ? ts * 1000 : ts
  const d = new Date(ms)
  if (Number.isNaN(d.getTime())) return '—'
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function errMsg(e: unknown): string {
  const err = e as { response?: { data?: { detail?: string; message?: string } }; message?: string }
  return err.response?.data?.detail ?? err.response?.data?.message ?? err.message ?? '未知错误'
}

function FolderTreeView(props: {
  node: FolderNode
  rootSize: number
  depth?: number
}): JSX.Element {
  const { node, rootSize, depth = 0 } = props
  const pct = rootSize > 0 ? (node.size / rootSize) * 100 : 0
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, paddingLeft: depth * 18 }}>
        <span className="mono" style={{ minWidth: 200, flexShrink: 0, wordBreak: 'break-all' }}>
          {node.name}
        </span>
        <div
          style={{
            flex: 1,
            background: 'var(--paicc-border, rgba(0,0,0,0.12))',
            height: 10,
            borderRadius: 5,
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              width: `${pct.toFixed(2)}%`,
              height: '100%',
              background: 'var(--paicc-accent)',
              transition: 'width 0.2s ease',
            }}
          />
        </div>
        <span
          style={{
            minWidth: 90,
            textAlign: 'right',
            color: 'var(--paicc-muted)',
            flexShrink: 0,
          }}
        >
          {formatBytes(node.size)}
        </span>
      </div>
      {node.children && node.children.length > 0 && (
        <div>
          {node.children.map((child) => (
            <FolderTreeView key={child.path} node={child} rootSize={rootSize} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  )
}

export default function FilesPage(): JSX.Element {
  const [searchLoading, setSearchLoading] = useState(false)
  const [searchResults, setSearchResults] = useState<FileSearchResult[]>([])
  const [fileType, setFileType] = useState<string>('')
  const [fileTime, setFileTime] = useState<string>('')
  const [sizeMin, setSizeMin] = useState<number>(0)

  const [contentLoading, setContentLoading] = useState(false)
  const [contentHits, setContentHits] = useState<ContentHit[]>([])

  const [largeDir, setLargeDir] = useState('')
  const [largeThreshold, setLargeThreshold] = useState<number>(100)
  const [largeLoading, setLargeLoading] = useState(false)
  const [largeFiles, setLargeFiles] = useState<LargeFile[]>([])

  const [dupDir, setDupDir] = useState('')
  const [dupLoading, setDupLoading] = useState(false)
  const [dupGroups, setDupGroups] = useState<DuplicateGroup[]>([])

  const [folderDir, setFolderDir] = useState('')
  const [folderLoading, setFolderLoading] = useState(false)
  const [folderSize, setFolderSize] = useState<number | null>(null)
  const [folderTree, setFolderTree] = useState<FolderNode | null>(null)
  const [folderTreeLoading, setFolderTreeLoading] = useState(false)

  const [taskProgress, setTaskProgress] = useState<{ kind: 'large' | 'dup'; value: number } | null>(
    null,
  )

  const pollRef = useRef<number | null>(null)
  const currentTaskRef = useRef<string | null>(null)
  const currentKindRef = useRef<'large' | 'dup' | null>(null)

  useEffect(
    () => () => {
      if (pollRef.current !== null) window.clearInterval(pollRef.current)
    },
    [],
  )

  const stopPoll = (): void => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current)
      pollRef.current = null
    }
  }

  const pollTask = (
    kind: 'large' | 'dup',
    taskId: string,
    onDone: (result: unknown) => void,
    onFail: (msg: string) => void,
  ): void => {
    if (pollRef.current !== null) window.clearInterval(pollRef.current)
    currentTaskRef.current = taskId
    currentKindRef.current = kind
    setTaskProgress({ kind, value: 0 })
    pollRef.current = window.setInterval(async () => {
      try {
        const res = await api.get<unknown>(`/files/task/${taskId}`)
        const data = res.data as {
          status?: string
          state?: string
          done?: boolean
          completed?: boolean
          result?: unknown
          files?: unknown
          message?: string
          error?: string
        }
        const status = String(data.status ?? data.state ?? '')
        const isDone =
          data.done === true ||
          data.completed === true ||
          ['done', 'completed', 'finished', 'success'].includes(status)
        const isFail = ['failed', 'error', 'cancelled'].includes(status)
        if (isDone) {
          stopPoll()
          setTaskProgress({ kind, value: 1 })
          onDone(data.result ?? data.files ?? [])
        } else if (isFail) {
          stopPoll()
          setTaskProgress(null)
          onFail(String(data.message ?? data.error ?? '任务失败'))
        }
      } catch (e) {
        stopPoll()
        setTaskProgress(null)
        onFail(errMsg(e))
      }
    }, 1500)
  }

  const onTaskProgress = useCallback((data: unknown) => {
    const t = data as { id?: string; status?: string; progress?: number }
    if (!t || typeof t.id !== 'string') return
    if (currentTaskRef.current !== t.id) return
    const kind = currentKindRef.current
    if (!kind) return
    const p = Number(t.progress)
    if (Number.isFinite(p)) {
      setTaskProgress({ kind, value: Math.min(1, Math.max(0, p)) })
    }
  }, [])

  useWsEvent('task_progress', onTaskProgress)

  const handleFileSearch = async (q: string): Promise<void> => {
    if (!q.trim()) {
      notification.warning({ message: '请输入文件名关键字' })
      return
    }
    setSearchLoading(true)
    try {
      const res = await api.get<unknown>('/files/search', {
        params: {
          query: q,
          ...(fileType ? { type: fileType } : {}),
          ...(sizeMin ? { size_min: sizeMin } : {}),
          ...(fileTime ? { time: fileTime } : {}),
        },
      })
      const data = res.data as { ok?: boolean; results?: FileSearchResult[]; error?: string }
      if (data && Array.isArray(data.results)) {
        setSearchResults(data.results)
      } else if (data && data.error) {
        setSearchResults([])
        notification.error({ message: '文件搜索失败', description: data.error })
      } else {
        setSearchResults([])
      }
    } catch (e) {
      notification.error({ message: '文件搜索失败', description: errMsg(e) })
    } finally {
      setSearchLoading(false)
    }
  }

  const handleContentSearch = async (q: string): Promise<void> => {
    if (!q.trim()) {
      notification.warning({ message: '请输入内容关键字' })
      return
    }
    setContentLoading(true)
    try {
      const res = await api.get<unknown>('/files/content-search', { params: { query: q } })
      const data = res.data as { ok?: boolean; results?: ContentHit[]; error?: string }
      if (data && Array.isArray(data.results)) {
        setContentHits(data.results)
      } else if (data && data.error) {
        setContentHits([])
        notification.error({ message: '内容搜索失败', description: data.error })
      } else {
        setContentHits([])
      }
    } catch (e) {
      notification.error({ message: '内容搜索失败', description: errMsg(e) })
    } finally {
      setContentLoading(false)
    }
  }

  const startLargeScan = async (): Promise<void> => {
    if (!largeDir.trim()) {
      notification.warning({ message: '请输入要扫描的目录' })
      return
    }
    setLargeLoading(true)
    setLargeFiles([])
    try {
      const res = await api.post<unknown>('/files/scan-large', {
        directory: largeDir.trim(),
        threshold_mb: largeThreshold,
      })
      const data = res.data as { task_id?: string; id?: string }
      const taskId = data.task_id ?? data.id
      if (!taskId) throw new Error('后端未返回任务 ID')
      pollTask(
        'large',
        taskId,
        (result) => {
          const list = (Array.isArray(result) ? result : []) as LargeFile[]
          list.sort((a, b) => b.size - a.size)
          setLargeFiles(list)
          setLargeLoading(false)
        },
        (msg) => {
          setLargeLoading(false)
          notification.error({ message: '大文件扫描失败', description: msg })
        },
      )
    } catch (e) {
      setLargeLoading(false)
      notification.error({ message: '启动大文件扫描失败', description: errMsg(e) })
    }
  }

  const startDupScan = async (): Promise<void> => {
    if (!dupDir.trim()) {
      notification.warning({ message: '请输入要扫描的目录' })
      return
    }
    setDupLoading(true)
    setDupGroups([])
    try {
      const res = await api.post<unknown>('/files/find-duplicates', { directory: dupDir.trim() })
      const data = res.data as { task_id?: string; id?: string }
      const taskId = data.task_id ?? data.id
      if (!taskId) throw new Error('后端未返回任务 ID')
      pollTask(
        'dup',
        taskId,
        (result) => {
          setDupGroups((Array.isArray(result) ? result : []) as DuplicateGroup[])
          setDupLoading(false)
        },
        (msg) => {
          setDupLoading(false)
          notification.error({ message: '重复文件扫描失败', description: msg })
        },
      )
    } catch (e) {
      setDupLoading(false)
      notification.error({ message: '启动重复文件扫描失败', description: errMsg(e) })
    }
  }

  const computeFolderSize = async (): Promise<void> => {
    if (!folderDir.trim()) {
      notification.warning({ message: '请输入文件夹路径' })
      return
    }
    setFolderLoading(true)
    setFolderSize(null)
    try {
      const res = await api.get<unknown>('/files/folder-size', {
        params: { directory: folderDir.trim() },
      })
      const data = res.data as number | { size?: number; total_size?: number }
      const size = typeof data === 'number' ? data : (data.size ?? data.total_size ?? 0)
      setFolderSize(Number(size))
    } catch (e) {
      notification.error({ message: '计算文件夹大小失败', description: errMsg(e) })
    } finally {
      setFolderLoading(false)
    }
  }

  const computeFolderTree = async (): Promise<void> => {
    if (!folderDir.trim()) {
      notification.warning({ message: '请输入文件夹路径' })
      return
    }
    setFolderTreeLoading(true)
    setFolderTree(null)
    try {
      const res = await api.get<unknown>('/files/folder-tree', {
        params: { path: folderDir.trim(), depth: 3 },
      })
      setFolderTree(res.data as FolderNode)
    } catch (e) {
      notification.error({ message: '生成目录树失败', description: errMsg(e) })
    } finally {
      setFolderTreeLoading(false)
    }
  }

  const deleteDuplicateGroup = async (group: DuplicateGroup): Promise<void> => {
    const confirmationId = await confirmOperation('delete_duplicates', '删除重复文件', {
      hash: group.hash,
      count: group.count,
      keep: group.files[0] ?? '',
    })
    if (!confirmationId) return
    try {
      const res = await api.post<unknown>('/files/delete-duplicates', {
        hash: group.hash,
        keep_index: 0,
        confirmation_id: confirmationId,
      })
      const data = res.data as { deleted?: number; kept?: string }
      notification.success({
        message: '重复文件已删除',
        description: `已删除 ${data.deleted ?? group.count - 1} 个重复文件，保留 ${data.kept ?? group.files[0] ?? ''}`,
      })
      setDupGroups((prev) => prev.filter((g) => g.hash !== group.hash))
    } catch (e) {
      notification.error({ message: '删除重复文件失败', description: errMsg(e) })
    }
  }

  const openLocation = async (path: string): Promise<void> => {
    try {
      await api.post('/files/open', { path })
    } catch (e) {
      notification.error({ message: '打开位置失败', description: errMsg(e) })
    }
  }

  const fileSearchColumns: ColumnsType<FileSearchResult> = [
    { title: '名称', dataIndex: 'name', key: 'name', ellipsis: true },
    {
      title: '路径',
      dataIndex: 'path',
      key: 'path',
      ellipsis: true,
      render: (v: string) => <span className="mono">{v}</span>,
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 110,
      sorter: (a, b) => a.size - b.size,
      render: (v: number, record: FileSearchResult) => (record.is_dir ? '—' : formatBytes(v)),
    },
    {
      title: '修改时间',
      dataIndex: 'modified',
      key: 'modified',
      width: 170,
      render: (v: number) => formatDate(v),
    },
    {
      title: '操作',
      key: 'action',
      width: 110,
      render: (_: unknown, record: FileSearchResult) => (
        <Button size="small" onClick={() => void openLocation(record.path)}>
          打开位置
        </Button>
      ),
    },
  ]

  const largeFileColumns: ColumnsType<LargeFile> = [
    {
      title: '路径',
      dataIndex: 'path',
      key: 'path',
      ellipsis: true,
      render: (v: string) => <span className="mono">{v}</span>,
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 120,
      sorter: (a, b) => a.size - b.size,
      render: (v: number) => formatBytes(v),
    },
    {
      title: '修改时间',
      dataIndex: 'modified',
      key: 'modified',
      width: 170,
      render: (v: number) => formatDate(v),
    },
  ]

  const dupColumns: ColumnsType<DuplicateGroup> = [
    {
      title: '文件哈希',
      dataIndex: 'hash',
      key: 'hash',
      ellipsis: true,
      render: (v: string) => <span className="mono">{v}</span>,
    },
    {
      title: '大小',
      dataIndex: 'size',
      key: 'size',
      width: 120,
      sorter: (a, b) => a.size - b.size,
      render: (v: number) => formatBytes(v),
    },
    {
      title: '重复数量',
      dataIndex: 'count',
      key: 'count',
      width: 110,
      sorter: (a, b) => a.count - b.count,
    },
    {
      title: '操作',
      key: 'action',
      width: 120,
      render: (_: unknown, record: DuplicateGroup) => (
        <Button size="small" danger onClick={() => void deleteDuplicateGroup(record)}>
          删除重复
        </Button>
      ),
    },
  ]

  const items = [
    {
      key: 'name',
      label: '文件名搜索',
      children: (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Space wrap>
            <Input.Search
              placeholder="输入文件名关键字"
              allowClear
              enterButton="搜索"
              onSearch={(v: string) => void handleFileSearch(v)}
              style={{ width: 320 }}
            />
            <Select
              value={fileType}
              onChange={(v: string) => setFileType(v)}
              options={FILE_TYPES}
              style={{ width: 140 }}
            />
            <Select
              value={sizeMin}
              onChange={(v: number) => setSizeMin(v)}
              options={SIZE_OPTIONS}
              style={{ width: 150 }}
            />
            <Select
              value={fileTime}
              onChange={(v: string) => setFileTime(v)}
              options={TIME_OPTIONS}
              style={{ width: 130 }}
            />
          </Space>
          <Table
            rowKey="path"
            columns={fileSearchColumns}
            dataSource={searchResults}
            loading={searchLoading}
            size="small"
            pagination={{ pageSize: 10, showSizeChanger: true }}
            scroll={{ x: 800 }}
            locale={{ emptyText: <Empty description="输入关键字开始搜索" /> }}
          />
        </Space>
      ),
    },
    {
      key: 'content',
      label: '内容搜索',
      children: (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Input.Search
            placeholder="输入内容关键字"
            allowClear
            enterButton="搜索"
            onSearch={(v: string) => void handleContentSearch(v)}
            style={{ width: 360 }}
          />
          <Spin spinning={contentLoading}>
            {contentHits.length === 0 ? (
              <Empty description="输入关键字搜索文件内容" />
            ) : (
              <List
                bordered
                dataSource={contentHits}
                renderItem={(item: ContentHit) => (
                  <List.Item>
                    <div style={{ width: '100%' }}>
                      <div className="mono" style={{ wordBreak: 'break-all' }}>
                        {item.path}
                      </div>
                      <div
                        style={{
                          color: 'var(--paicc-muted)',
                          marginTop: 4,
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-all',
                        }}
                      >
                        {item.snippet}
                      </div>
                    </div>
                  </List.Item>
                )}
              />
            )}
          </Spin>
        </Space>
      ),
    },
    {
      key: 'large',
      label: '大文件',
      children: (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Space wrap>
            <Input
              placeholder="目录路径，如 C:/Users"
              value={largeDir}
              onChange={(e) => setLargeDir(e.target.value)}
              style={{ width: 320 }}
              allowClear
            />
            <Select
              value={largeThreshold}
              onChange={(v: number) => setLargeThreshold(v)}
              options={THRESHOLD_OPTIONS}
              style={{ width: 150 }}
            />
            <Button type="primary" loading={largeLoading} onClick={() => void startLargeScan()}>
              开始扫描
            </Button>
          </Space>
          {taskProgress !== null && taskProgress.kind === 'large' && (
            <Progress percent={Math.round(taskProgress.value * 100)} size="small" status="active" />
          )}
          <Table
            rowKey="path"
            columns={largeFileColumns}
            dataSource={largeFiles}
            loading={largeLoading}
            size="small"
            pagination={{ pageSize: 10 }}
            scroll={{ x: 600 }}
            locale={{ emptyText: <Empty description="输入目录并点击扫描" /> }}
          />
        </Space>
      ),
    },
    {
      key: 'dup',
      label: '重复文件',
      children: (
        <Space direction="vertical" style={{ width: '100%' }} size="middle">
          <Space wrap>
            <Input
              placeholder="目录路径，如 C:/Users"
              value={dupDir}
              onChange={(e) => setDupDir(e.target.value)}
              style={{ width: 320 }}
              allowClear
            />
            <Button type="primary" loading={dupLoading} onClick={() => void startDupScan()}>
              查找重复文件
            </Button>
          </Space>
          {taskProgress !== null && taskProgress.kind === 'dup' && (
            <Progress percent={Math.round(taskProgress.value * 100)} size="small" status="active" />
          )}
          <Table
            rowKey="hash"
            columns={dupColumns}
            dataSource={dupGroups}
            loading={dupLoading}
            size="small"
            pagination={{ pageSize: 10 }}
            expandable={{
              expandedRowRender: (record: DuplicateGroup) => (
                <List
                  size="small"
                  dataSource={record.files}
                  renderItem={(f: string) => (
                    <List.Item>
                      <span className="mono" style={{ wordBreak: 'break-all' }}>
                        {f}
                      </span>
                    </List.Item>
                  )}
                />
              ),
            }}
            locale={{ emptyText: <Empty description="输入目录并点击查找" /> }}
          />
        </Space>
      ),
    },
    {
      key: 'foldersize',
      label: '文件夹大小',
      children: (
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Space wrap>
            <Input
              placeholder="文件夹路径，如 C:/Users"
              value={folderDir}
              onChange={(e) => setFolderDir(e.target.value)}
              style={{ width: 360 }}
              allowClear
            />
            <Button type="primary" loading={folderLoading} onClick={() => void computeFolderSize()}>
              计算大小
            </Button>
            <Button loading={folderTreeLoading} onClick={() => void computeFolderTree()}>
              生成目录树
            </Button>
          </Space>
          {folderSize !== null && (
            <Card size="small" style={{ maxWidth: 360 }}>
              <Statistic
                title="文件夹总大小"
                value={formatBytes(folderSize)}
                valueStyle={{ color: 'var(--paicc-accent)' }}
              />
            </Card>
          )}
          {folderTree !== null && (
            <Card
              size="small"
              title={
                <span className="mono" style={{ fontSize: 13 }}>
                  {folderTree.path}
                </span>
              }
            >
              <div style={{ maxHeight: 480, overflow: 'auto' }}>
                <FolderTreeView node={folderTree} rootSize={folderTree.size} />
              </div>
            </Card>
          )}
        </Space>
      ),
    },
  ]

  return <Tabs defaultActiveKey="name" items={items} />
}
