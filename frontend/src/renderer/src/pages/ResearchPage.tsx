import { DeleteOutlined, ImportOutlined, ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import {
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  List,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
  notification,
} from 'antd'
import type { TableColumnsType } from 'antd'
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/services/api'
import { confirmOperation } from '@/services/confirm'
import type { ResearchDoc } from '@/types'

interface SearchResult {
  title: string
  snippet: string
  score: number
}

function describeError(err: unknown): string {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as { response?: { data?: unknown } }).response
    const data = resp?.data
    if (data && typeof data === 'object' && 'detail' in data) {
      return String((data as { detail: unknown }).detail)
    }
    if (typeof data === 'string') return data
  }
  if (err instanceof Error) return err.message
  return String(err)
}

function formatTime(ts: number | null | undefined): string {
  if (ts == null) return '—'
  const ms = ts > 1e12 ? ts : ts * 1000
  return new Date(ms).toLocaleString()
}

function formatScore(score: number | null | undefined): string {
  if (score == null) return '—'
  return score.toFixed(4)
}

export default function ResearchPage(): JSX.Element {
  const [docs, setDocs] = useState<ResearchDoc[]>([])
  const [docsLoading, setDocsLoading] = useState(true)

  const [filePath, setFilePath] = useState('')
  const [rawText, setRawText] = useState('')
  const [ingesting, setIngesting] = useState(false)

  const [query, setQuery] = useState('')
  const [topK, setTopK] = useState(5)
  const [searching, setSearching] = useState(false)
  const [searched, setSearched] = useState(false)
  const [results, setResults] = useState<SearchResult[]>([])

  const [topic, setTopic] = useState('')
  const [reportContent, setReportContent] = useState('')
  const [generating, setGenerating] = useState(false)
  const [savingToKb, setSavingToKb] = useState(false)

  const refreshDocs = useCallback(async (): Promise<void> => {
    try {
      const { data } = await api.get<ResearchDoc[]>('/research/documents')
      setDocs(Array.isArray(data) ? data : [])
    } catch {
      setDocs([])
    } finally {
      setDocsLoading(false)
    }
  }, [])

  useEffect(() => {
    void refreshDocs()
  }, [refreshDocs])

  const ingest = async (source: string): Promise<void> => {
    setIngesting(true)
    try {
      await api.post('/research/ingest', { source })
      notification.success({ message: '导入成功' })
      setFilePath('')
      setRawText('')
      await refreshDocs()
    } catch (err) {
      notification.error({ message: '导入失败', description: describeError(err) })
    } finally {
      setIngesting(false)
    }
  }

  const handleIngestPath = async (): Promise<void> => {
    const p = filePath.trim()
    if (!p) return
    await ingest(p)
  }

  const handleIngestText = async (): Promise<void> => {
    const t = rawText.trim()
    if (!t) return
    await ingest(t)
  }

  const handleDelete = async (doc: ResearchDoc): Promise<void> => {
    const confirmationId = await confirmOperation('delete_research_document', '删除研究文档', {
      id: doc.id,
      title: doc.title,
      file_path: doc.file_path,
    })
    if (!confirmationId) return
    try {
      await api.delete(`/research/documents/${doc.id}`, {
        params: { confirmation_id: confirmationId },
      })
      notification.success({ message: '已删除', description: doc.title })
      await refreshDocs()
    } catch (err) {
      notification.error({ message: '删除失败', description: describeError(err) })
    }
  }

  const handleSearch = async (value: string): Promise<void> => {
    const q = value.trim()
    if (!q) return
    setSearching(true)
    setSearched(true)
    try {
      const { data } = await api.get<unknown>('/research/search', {
        params: { query: q, top_k: topK },
      })
      const list = (data as { results?: SearchResult[] } | null)?.results
      setResults(Array.isArray(list) ? list : [])
    } catch (err) {
      notification.error({ message: '检索失败', description: describeError(err) })
      setResults([])
    } finally {
      setSearching(false)
    }
  }

  const handleGenerate = async (): Promise<void> => {
    const t = topic.trim()
    if (!t) return
    setGenerating(true)
    try {
      const { data } = await api.post<unknown>('/research/report', { topic: t })
      setReportContent(extractContent(data))
    } catch (err) {
      notification.error({ message: '生成失败', description: describeError(err) })
    } finally {
      setGenerating(false)
    }
  }

  const handleSaveToKb = async (): Promise<void> => {
    if (!reportContent) return
    const title = topic.trim() || '研究报告'
    setSavingToKb(true)
    try {
      await api.post('/quant/save-report', { title, content: reportContent })
      notification.success({ message: '已保存到知识库', description: title })
    } catch (err) {
      notification.error({ message: '保存失败', description: describeError(err) })
    } finally {
      setSavingToKb(false)
    }
  }

  const docColumns: TableColumnsType<ResearchDoc> = [
    { title: '标题', dataIndex: 'title', key: 'title', ellipsis: true },
    {
      title: '文件路径',
      dataIndex: 'file_path',
      key: 'file_path',
      className: 'mono',
      ellipsis: true,
      render: (v: string) => v ?? '—',
    },
    { title: '分块数', dataIndex: 'chunk_count', key: 'chunk_count', width: 90 },
    {
      title: '导入时间',
      dataIndex: 'ingested_at',
      key: 'ingested_at',
      width: 180,
      render: (v: number) => formatTime(v),
    },
    {
      title: '操作',
      key: 'action',
      width: 90,
      render: (_, record) => (
        <Button
          size="small"
          danger
          icon={<DeleteOutlined />}
          onClick={() => void handleDelete(record)}
        >
          删除
        </Button>
      ),
    },
  ]

  const docsTab = (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Card title="导入文档" size="small">
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <Space.Compact style={{ width: '100%' }}>
            <Input
              placeholder="输入文档文件路径或网页 URL（http/https）"
              value={filePath}
              onChange={(e) => setFilePath(e.target.value)}
              onPressEnter={() => void handleIngestPath()}
            />
            <Button
              type="primary"
              icon={<ImportOutlined />}
              loading={ingesting}
              onClick={() => void handleIngestPath()}
            >
              导入
            </Button>
          </Space.Compact>
          <Input.TextArea
            placeholder="或粘贴原始文本内容或网页 URL 后导入"
            value={rawText}
            autoSize={{ minRows: 3, maxRows: 6 }}
            onChange={(e) => setRawText(e.target.value)}
          />
          <div>
            <Button
              type="primary"
              icon={<ImportOutlined />}
              loading={ingesting}
              disabled={!rawText.trim()}
              onClick={() => void handleIngestText()}
            >
              导入文本
            </Button>
          </div>
        </Space>
      </Card>

      <Card
        title="已导入文档"
        size="small"
        extra={
          <Button size="small" icon={<ReloadOutlined />} onClick={() => void refreshDocs()}>
            刷新
          </Button>
        }
      >
        {docsLoading ? (
          <Spin />
        ) : (
          <Table<ResearchDoc>
            rowKey="id"
            columns={docColumns}
            dataSource={docs}
            size="small"
            pagination={false}
          />
        )}
      </Card>
    </Space>
  )

  const searchTab = (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Card size="small">
        <Space direction="vertical" style={{ width: '100%' }} size={8}>
          <Input.Search
            placeholder="输入查询关键词"
            enterButton="检索"
            allowClear
            loading={searching}
            onSearch={(v) => void handleSearch(v)}
          />
          <Space size={8}>
            <Typography.Text type="secondary">top_k：</Typography.Text>
            <InputNumber min={1} max={50} value={topK} onChange={(v) => setTopK(v ?? 5)} />
          </Space>
        </Space>
      </Card>
      {searching ? (
        <Spin />
      ) : !searched ? (
        <Empty description="输入关键词开始检索" />
      ) : results.length === 0 ? (
        <Empty description="无匹配结果" />
      ) : (
        <List<SearchResult>
          itemLayout="vertical"
          dataSource={results}
          renderItem={(item, index) => (
            <List.Item>
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  gap: 8,
                }}
              >
                <Typography.Text strong>
                  {index + 1}. {item.title || '（无标题）'}
                </Typography.Text>
                <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                  score {formatScore(item.score)}
                </Tag>
              </div>
              <Typography.Paragraph
                style={{ margin: '8px 0 0', color: '#8a93a6', whiteSpace: 'pre-wrap' }}
              >
                {item.snippet}
              </Typography.Paragraph>
            </List.Item>
          )}
        />
      )}
    </Space>
  )

  const reportTab = (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Space.Compact style={{ width: '100%' }}>
        <Input
          placeholder="输入报告主题"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          onPressEnter={() => void handleGenerate()}
        />
        <Button type="primary" loading={generating} onClick={() => void handleGenerate()}>
          生成
        </Button>
      </Space.Compact>
      {reportContent ? (
        <>
          <div>
            <Button
              type="primary"
              icon={<SaveOutlined />}
              loading={savingToKb}
              onClick={() => void handleSaveToKb()}
            >
              保存到知识库
            </Button>
          </div>
          <pre
            className="mono"
            style={{
              margin: 0,
              padding: 16,
              background: '#0b0d11',
              border: '1px solid #262b36',
              borderRadius: 8,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
              maxHeight: 520,
              overflow: 'auto',
              lineHeight: 1.6,
            }}
          >
            {reportContent}
          </pre>
        </>
      ) : (
        <Empty description="输入主题后生成研究报告" />
      )}
    </Space>
  )

  return (
    <Tabs
      defaultActiveKey="docs"
      items={[
        { key: 'docs', label: '文档', children: docsTab },
        { key: 'search', label: '检索', children: searchTab },
        { key: 'report', label: '报告', children: reportTab },
      ]}
    />
  )
}

function extractContent(data: unknown): string {
  if (typeof data === 'string') return data
  if (data && typeof data === 'object') {
    const obj = data as Record<string, unknown>
    const candidate = obj.content ?? obj.report ?? obj.markdown ?? obj.text
    if (typeof candidate === 'string') return candidate
  }
  return JSON.stringify(data, null, 2)
}
