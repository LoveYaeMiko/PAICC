# Personal AI Command Center (PAICC) 项目蓝图

**版本**: 1.0  
**最后更新**: 2026-08-14  
**状态**: 待开发  
**目标用户**: 技术爱好者（个人使用）  
**平台**: Windows 10/11 桌面应用  

---

## 1. 项目概述

Personal AI Command Center（PAICC）是一个高度个性化的 Windows 桌面应用程序，将系统控制、文件管理、应用管理、量化研究监控、深度研究工作台和 AI 助手融为一体。用户通过一个可拖动的悬浮球进行交互，AI 助手可执行自然语言指令，调用各种系统工具，并与本地 Claude Code 集成实现代码级操作。

核心特色：
- **统一入口**：悬浮球 + 常驻输入框，随时对话和操作。
- **AI 驱动**：LLM 作为大脑，通过 Function Calling 调用所有工具。
- **量化专属**：与本地量化项目深度集成，监控模拟盘红线、管理回测进程。
- **研究台**：本地知识库 + 联网检索，生成研究报告。
- **Claude Code 集成**：内嵌终端，直接调用 CLI，类似 VSCode 插件体验。
- **安全确认**：危险操作必须用户确认，全量日志。

---

## 2. 功能需求

### 2.1 系统监控
- 实时显示 CPU、内存、磁盘 I/O、网络速度、GPU 温度/负载（可选）。
- 进程列表：查看、排序（CPU/内存/磁盘/网络）、结束进程、定位文件。
- 电源计划切换（节能/平衡/高性能/卓越性能）。
- 开机启动项管理（启用/禁用/延迟）。

### 2.2 文件管理
- **文件名搜索**：集成 Everything，毫秒级返回，支持类型、大小、时间过滤。
- **内容搜索**：使用 Windows Search，支持 PDF、Word、Excel、PPT、Markdown、CSV 等。
- **大文件扫描**：指定目录、阈值，列出结果。
- **重复文件检测**：基于哈希，提供预览和删除（需确认）。
- **临时文件清理**：系统临时、浏览器缓存、缩略图等，需确认。
- **文件夹大小分析**：递归计算，树状图展示。

### 2.3 应用管理
- **扫描应用**：开始菜单、桌面快捷方式、注册表卸载项。
- **启动应用**：支持 exe、快捷方式、命令行脚本（脚本执行通过 Claude Code）。
- **卸载应用**：调用系统卸载程序（弹出 GUI），不静默。
- **常用应用**：用户标记或自动推荐，提取图标并缓存，显示在悬浮球快速启动。

### 2.4 存储分析报告
- 手动或定时生成 Markdown 报告，包含磁盘使用、大文件 Top10、重复文件统计、清理建议。
- 定时任务（APScheduler）可设置每周/每月，并通过邮箱发送（SMTP 配置后续提供）。

### 2.5 AI 智能助手
- 自然语言对话，可调用所有工具（Function Calling）。
- 主动提醒：温度过高、磁盘不足、量化红线触发。
- 多模型支持：云端 API（DeepSeek/OpenAI/Claude），本地 Ollama 备用。

### 2.6 量化项目监控与操作
- 量化项目位于`C:\Users\wyxwi\Desktop\FQA`目录下。
- 找到量化项目目录，自动识别关键文件。
- 实时监控量化进程：CPU/内存、运行时长、日志流（高亮错误）。
- **红线仪表盘**：展示 Phase 10 模拟盘的四项红线（成本偏离、空腿偏差、Regime 切换、PEAD 异常）。
- 一键启动/停止预设命令（如模拟盘运行、回测）。
- 可视化编辑 `simulation.yaml`，保存后自动重启相关进程。
- 红线触发时 AI 主动弹窗通知，并引导处理。

### 2.7 深度研究工作台
- 本地知识库：导入 PDF、Markdown、网页等，向量化存储（Chroma）。
- 检索增强生成（RAG）：本地优先，相似度不足时自动联网搜索。
- 研究报告生成：结合本地资料和联网内容，输出结构化报告。
- 与量化模块联动：可将回测结果、报告自动入库。

### 2.8 Claude Code 集成
- 内嵌 xterm.js 终端，直接调用 `claude` CLI（`--output-format stream-json`）。
- 解析流式输出，展示消息气泡或原始终端。
- 支持向 Claude 发送指令，执行代码修改、运行脚本等。
- 文件变更监控：Claude 修改文件后显示 diff。
- 上下文注入：将量化监控状态、系统信息作为上下文。

### 2.9 安全与确认机制
- 所有修改类操作（删除、清理、卸载、执行未知脚本）必须通过 UI 弹窗确认。
- 操作日志记录到 SQLite（时间、操作、参数、结果）。
- AI 默认只读权限，修改需用户批准。

---

## 3. 系统架构

### 3.1 总体架构图（文字版）

```
┌──────────────────────────────────────────────┐
│              Electron Main Process           │
│  - 悬浮球窗口管理                             │
│  - 系统托盘、开机启动、全局快捷键             │
│  - 启动/管理 Python 后端子进程               │
└───────────────┬──────────────────────────────┘
                │ IPC / HTTP
┌───────────────▼──────────────────────────────┐
│        Electron Renderer (React)             │
│  - 悬浮球 UI（圆形辐射菜单）                 │
│  - 主界面（多标签页）                        │
│  - 常驻输入框                                │
│  - xterm.js 终端                             │
└───────────────┬──────────────────────────────┘
                │ HTTP / WebSocket
┌───────────────▼──────────────────────────────┐
│           Python FastAPI Backend             │
│  - 系统监控模块 (psutil, wmi)                │
│  - 文件管理模块 (Everything, Windows Search) │
│  - 应用管理模块 (注册表/快捷方式)            │
│  - 存储分析模块 (APScheduler, SMTP)          │
│  - 量化监控模块 (subprocess, watchdog, yaml) │
│  - 知识库服务 (Chroma, embeddings)           │
│  - AI 工具集 (Function Calling)              │
│  - Claude Code 调度器 (subprocess, stream)   │
│  - SQLite 数据库                             │
└──────────────────────────────────────────────┘
```

### 3.2 技术栈

| 层次 | 技术 | 说明 |
|------|------|------|
| 桌面框架 | Electron | 跨平台，适合悬浮球、系统托盘 |
| 前端 | React + TypeScript | 组件化开发 |
| UI 库 | Ant Design 或 Tailwind CSS | 由实施 agent 决定 |
| 后端 | Python + FastAPI | 复用量化代码，AI 生态 |
| 系统监控 | psutil, GPUtil, wmi | 跨平台资源监控 |
| 文件搜索 | Everything SDK/CLI (`es.exe`) | 极速文件名搜索 |
| 内容搜索 | Windows Search API (pywin32) | 文档内容检索 |
| 进程管理 | subprocess, psutil | 启动/监控进程 |
| 任务调度 | APScheduler | 定时报告 |
| 邮件发送 | smtplib / yagmail | 发送报告 |
| 数据库 | SQLite | 配置、日志、缓存 |
| 向量数据库 | Chroma | 本地知识库 |
| Embeddings | sentence-transformers | 中文友好模型 |
| LLM API | DeepSeek / OpenAI / Claude | 可由配置切换 |
| 本地 LLM | Ollama (可选) | 离线备用 |
| Claude Code | claude CLI | 直接调用 |
| 终端 | xterm.js | 内嵌终端 |
| 打包 | electron-builder | 生成安装包 |

---

## 4. 目录结构建议

```
paicc/
├── frontend/                   # Electron + React 前端
│   ├── src/
│   │   ├── components/         # 悬浮球、主界面、输入框等
│   │   ├── pages/              # 各功能页面
│   │   ├── services/           # API 调用
│   │   ├── store/              # 状态管理
│   │   └── utils/
│   ├── main.ts                 # Electron 主进程
│   ├── preload.ts              # 预加载脚本
│   └── package.json
├── backend/                    # Python FastAPI 后端
│   ├── app/
│   │   ├── main.py             # FastAPI 入口
│   │   ├── routers/            # API 路由
│   │   │   ├── system.py
│   │   │   ├── files.py
│   │   │   ├── apps.py
│   │   │   ├── storage.py
│   │   │   ├── quant.py
│   │   │   ├── research.py
│   │   │   ├── ai.py
│   │   │   └── claude_code.py
│   │   ├── services/           # 业务逻辑
│   │   │   ├── system_monitor.py
│   │   │   ├── everything.py
│   │   │   ├── windows_search.py
│   │   │   ├── file_ops.py
│   │   │   ├── app_manager.py
│   │   │   ├── storage_analysis.py
│   │   │   ├── quant_manager.py
│   │   │   ├── knowledge_base.py
│   │   │   ├── llm_client.py
│   │   │   └── claude_code.py
│   │   ├── tools/              # AI Function Calling 工具定义
│   │   ├── models/             # Pydantic 模型
│   │   ├── db.py               # SQLite 初始化
│   │   └── config.py
│   ├── scripts/                # PowerShell 脚本（索引添加等）
│   ├── requirements.txt
│   └── .env.example
├── data/                       # 数据库、索引、缓存
├── logs/
└── README.md
```

---

## 5. 核心模块设计

### 5.1 悬浮球交互

- **窗口属性**：透明、无边框、置顶、跳过任务栏、可拖动。
- **样式**：圆形，默认半透明，悬停高亮，支持简单动画（呼吸/旋转）。
- **菜单**：悬停 500ms 展开辐射状菜单，包含图标：系统监控、文件搜索、应用启动、存储分析、量化面板、研究台、设置。
- **快速启动**：可拖入常用应用快捷方式，显示在辐射菜单内层。
- **输入框**：悬浮球旁固定输入框（可折叠），支持文字输入，Enter 发送，Ctrl+Enter 换行。
- **全局快捷键**：`Alt+Space` 呼出/隐藏输入框。
- **开机启动**：通过 `app.setLoginItemSettings` 实现。

### 5.2 AI 助手

- **模型配置**：在设置中配置 API Key、模型名称、Base URL。
- **工具调用**：使用 OpenAI Function Calling 格式定义工具。
- **系统提示词**：包含用户偏好、量化项目信息、安全规则。
- **对话历史**：保存在内存或 SQLite。
- **主动通知**：通过 WebSocket 推送，在悬浮球旁弹出通知卡片。

### 5.3 文件搜索

- **Everything 集成**：后端维护 `es.exe` 路径，子进程调用。
- **API**：`/api/files/search?query=xx&type=pdf&size_min=1MB&limit=50`
- **内容搜索**：使用 Windows Search OLE DB 或 `pywin32` 的 `ISearchQueryHelper`。
- **索引管理**：提供 PowerShell 脚本添加索引目录（见附件）。

### 5.4 应用管理

- **扫描**：遍历开始菜单、桌面、注册表卸载项。
- **解析快捷方式**：使用 `win32com.client` 或 `pylnk3` 读取 `.lnk`。
- **图标提取**：使用 `win32ui` 提取 exe 图标，缓存为 PNG，仅对常用应用执行。
- **卸载**：从注册表读取 `UninstallString`，`subprocess.Popen` 执行。

### 5.5 量化监控

- **项目注册**：设置量化项目根目录，后端扫描子目录识别关键文件。
- **进程匹配**：通过命令行或可执行文件路径匹配量化相关进程（如 `python -m src.simulation.run`）。
- **红线状态读取**：运行 `python -m src.simulation.dashboard --sim_id phase10_sim` 或解析其输出 JSON。
- **日志流**：使用 `watchdog` 监控日志文件变化，推送新行到前端。
- **预设命令**：在配置文件中定义常用命令（启动模拟盘、回测、测试），一键执行。

### 5.6 深度研究工作台

- **知识库**：Chroma 持久化存储，文档导入支持 PDF（PyPDF2）、Markdown、TXT。
- **检索**：本地向量相似度检索，阈值可调；不足时自动调用搜索引擎 API（如 Bing/SerpAPI）。
- **生成**：使用 LLM 生成回答或报告。
- **自动入库**：量化报告生成后可调用知识库 API 自动保存。

### 5.7 Claude Code 集成

- **启动**：`subprocess.Popen(["claude", "--output-format", "stream-json"], stdin=PIPE, stdout=PIPE, stderr=PIPE)`。
- **事件解析**：逐行解析 JSON，识别 `assistant`、`tool_use`、`tool_result`、`error`。
- **前端展示**：可选终端模式（原始输出）或聊天模式（解析后气泡）。
- **命令执行**：用户输入可直接作为 Claude 指令，或 AI 助手调用 Claude 执行具体任务（如“运行回测脚本”）。
- **文件变更**：使用 `watchdog` 监控项目目录，Claude 修改后展示 diff（可集成 diff2html）。

### 5.8 安全确认

- **危险操作清单**：删除文件、清空回收站、卸载应用、执行未知脚本、修改系统设置。
- **确认流程**：AI 助手调用工具前，前端弹出确认对话框，列出操作详情；用户批准后后端执行。
- **超时自动拒绝**：30 秒无响应则取消。
- **操作日志**：所有 AI 操作和用户操作记录到 SQLite `operation_logs` 表。

---

## 6. API 设计（REST + WebSocket）

### 6.1 REST API 端点（部分）

| 方法 | 路径 | 描述 | 权限 |
|------|------|------|------|
| GET | /api/health | 健康检查 | 公开 |
| GET | /api/system/status | CPU/内存/磁盘/网络 | 查询 |
| GET | /api/system/processes | 进程列表 | 查询 |
| POST | /api/system/process/kill | 结束进程 | 修改（确认） |
| GET | /api/files/search | 文件名搜索 | 查询 |
| GET | /api/files/content-search | 内容搜索 | 查询 |
| POST | /api/files/scan-large | 大文件扫描（异步） | 查询 |
| POST | /api/files/find-duplicates | 重复文件检测（异步） | 查询 |
| GET | /api/storage/cleanable-items | 可清理项列表 | 查询 |
| POST | /api/storage/clean | 执行清理 | 修改（确认） |
| GET | /api/apps/list | 应用列表 | 查询 |
| POST | /api/apps/start | 启动应用 | 修改 |
| POST | /api/apps/uninstall | 卸载应用 | 修改（确认） |
| POST | /api/storage/analysis | 生成存储报告 | 查询 |
| GET | /api/storage/reports | 历史报告列表 | 查询 |
| POST | /api/storage/analysis/schedule | 设置定时报告 | 修改 |
| GET | /api/quant/status | 量化红线状态 | 查询 |
| POST | /api/quant/command | 执行量化命令 | 修改（确认） |
| GET | /api/research/search | 知识库检索 | 查询 |
| POST | /api/research/ingest | 导入文档 | 修改 |
| POST | /api/claude/start | 启动 Claude 会话 | 修改 |
| POST | /api/claude/send | 发送消息到 Claude | 修改 |
| GET | /api/claude/status | Claude 进程状态 | 查询 |
| GET | /api/logs/operations | 操作日志 | 查询 |

### 6.2 WebSocket 事件

| 事件 | 方向 | 描述 |
|------|------|------|
| `system_stats` | 后端→前端 | 实时系统状态（每 2 秒） |
| `log_line` | 后端→前端 | 量化日志新行 |
| `red_line_alert` | 后端→前端 | 红线触发通知 |
| `claude_event` | 后端→前端 | Claude Code 输出事件 |
| `task_progress` | 后端→前端 | 大文件扫描/重复检测进度 |

---

## 7. 数据库设计（SQLite）

### 表结构

- **settings**: key-value 配置（API keys、邮箱、常用目录等）
- **apps**: 应用缓存（id, name, path, icon_path, is_favorite）
- **operation_logs**: 操作日志（id, timestamp, user, action, params, result）
- **quant_projects**: 量化项目注册（id, name, root_path, config_file, dashboard_script）
- **quant_commands**: 预设命令（id, project_id, name, command, description）
- **research_documents**: 知识库文档元数据（id, title, file_path, ingested_at, chunk_count）
- **reports**: 存储分析报告历史（id, generated_at, content, sent_to_email）

---

## 8. 开发阶段计划

### 阶段 1：基础框架（2 周）
- 初始化前后端项目。
- 实现悬浮球窗口（拖动、悬停、辐射菜单）。
- 实现常驻输入框（折叠、发送消息）。
- 后端健康检查和基本系统状态 API。
- 接入一个 LLM API，实现简单对话。
- SQLite 初始化和配置读取。

**验收**：悬浮球可运行，输入框可与 LLM 对话，系统状态显示正常。

### 阶段 2：系统监控与文件管理（3 周）
- 完善系统监控 API（进程列表、结束进程）。
- 集成 Everything，实现文件名搜索。
- 实现大文件扫描、重复文件检测、临时文件清理。
- 集成 Windows Search，实现内容搜索。
- 前端实现系统监控页面、文件搜索页面、存储管理页面。

**验收**：可搜索文件、扫描大文件、查看系统状态。

### 阶段 3：应用管理与存储报告（2 周）
- 应用扫描、启动、卸载。
- 常用应用图标提取与缓存。
- 存储分析报告生成。
- 定时任务（APScheduler）与邮件发送（预留配置界面）。

**验收**：可启动/卸载应用，生成存储报告。

### 阶段 4：AI 工具集成与安全确认（2 周）
- 定义所有 Function Calling 工具。
- 实现危险操作确认机制。
- 操作日志记录。
- 主动通知（磁盘不足、温度过高等）。

**验收**：AI 可调用工具执行查询和修改操作，危险操作有确认弹窗。

### 阶段 5：量化项目联动（3 周）
- 量化项目注册与关键文件识别。
- 量化进程监控与日志流。
- 红线仪表盘读取与展示。
- 预设命令一键执行。
- 与 AI 助手集成（自然语言操作）。

**验收**：能监控模拟盘红线，启动/停止量化命令。

### 阶段 6：深度研究工作台（3 周）
- 知识库搭建（Chroma + embeddings）。
- 文档导入与解析。
- 本地检索 + 联网补充。
- 研究报告生成。
- 与量化模块联动（报告自动入库）。

**验收**：能导入文档并提问，生成简单报告。

### 阶段 7：Claude Code 集成（2 周）
- 内嵌 xterm.js 终端。
- 启动 claude CLI 并解析事件。
- 文件变更监控与 diff 展示。
- 与量化模块联动（通过 Claude 执行脚本）。

**验收**：可在控制中心内使用 Claude Code，类似 VSCode 插件。

### 阶段 8：优化与打包（1 周）
- 性能优化、内存占用优化。
- 打包为安装包（electron-builder）。
- 编写用户文档。

**验收**：可安装使用，功能完整。

**总计约 18 周**。

---

## 9. 关键附件

### 9.1 Windows 索引添加脚本（PowerShell）

```powershell
# add_search_index.ps1
# 以管理员权限运行
param(
    [string[]]$Directories = @("D:\Research", "D:\Quant", "D:\Documents")
)

$crawlManager = New-Object -ComObject "SearchAPI.CrawlScopeManager"
foreach ($dir in $Directories) {
    if (Test-Path $dir) {
        Write-Host "Adding to index: $dir"
        $crawlManager.AddRoot($dir, 0, 0)  # 0=include, 1=exclude
    } else {
        Write-Host "Directory not found: $dir"
    }
}
$crawlManager.SaveAll()
Write-Host "Index directories updated. Please rebuild index manually or restart Windows Search service."
```

### 9.2 量化项目配置模板（`quant_project.yaml`）

```yaml
project_name: "quant_research"
root_path: "D:/Quant"
config_file: "configs/simulation.yaml"
dashboard_script: "src/simulation/dashboard.py"
log_dir: "logs"
commands:
  start_simulation: "python -m src.simulation.run --config configs/simulation.yaml"
  stop_simulation: "python -m src.simulation.stop"
  run_backtest: "python -m src.backtest.run --config configs/backtest.yaml"
```

---

## 10. 验收标准

1. 悬浮球可拖动，悬停显示辐射菜单，输入框可对话。
2. 系统状态、进程管理、文件搜索、应用启动等功能正常。
3. AI 助手能调用所有工具，危险操作需确认。
4. 量化模块能监控红线、启动命令、查看日志。
5. 研究台能导入文档并回答问题。
6. Claude Code 集成可用，能执行代码相关任务。
7. 存储报告可定时生成并发送邮件（后续配置）。
8. 应用打包为可安装程序，开机自启。

---

## 11. 未来扩展

- 虚拟形象（Live2D/Spine）。
- 多语言支持。
- 移动端远程控制。
- 更多量化因子集成。
- 插件系统。

---

**本蓝图将作为实施 agent 的指导文档。如有任何歧义，请联系项目所有者澄清。**