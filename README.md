# Personal AI Command Center (PAICC)

高度个性化的 Windows 桌面应用 —— 将**系统控制、文件管理、应用管理、量化研究监控、深度研究工作台和 AI 助手**融为一体。用户通过一个可拖动的悬浮球交互，AI 助手可执行自然语言指令、调用各类系统工具，并与本地 Claude Code 集成实现代码级操作。

> 项目蓝图见 [blueprint.md](blueprint.md)。

---

## ✨ 核心特性

- **统一入口**：悬浮球 + 常驻输入框，随时对话和操作。
- **AI 驱动**：LLM 作为大脑，通过 Function Calling 调用所有工具。
- **量化专属**：与本地量化项目（`C:\Users\wyxwi\Desktop\FQA`）深度集成，监控四项模拟盘红线、管理进程。
- **研究台**：本地知识库（Chroma）+ 联网检索，生成研究报告。
- **Claude Code 集成**：内嵌终端，直接调用 `claude` CLI（`--output-format stream-json`）。
- **安全确认**：危险操作必须用户确认（30 秒超时自动拒绝），全量操作日志。

## 🧱 技术栈

| 层次 | 技术 |
|------|------|
| 桌面框架 | Electron |
| 前端 | React 18 + TypeScript + Ant Design 5 + Zustand + electron-vite |
| 后端 | Python 3.12 + FastAPI + Pydantic v2 |
| 系统监控 | psutil（GPU 可选 GPUtil） |
| 文件搜索 | Everything CLI（`es.exe`） / Windows Search |
| 任务调度 | APScheduler（定时存储报告） |
| 数据库 | SQLite（配置 / 日志 / 缓存） |
| 向量库 | Chroma（可选）+ sentence-transformers（可选） |
| LLM | DeepSeek / OpenAI / Claude / Ollama（OpenAI 兼容协议，可配置切换） |
| Claude Code | `claude` CLI |

## 📁 目录结构

```
paicc/
├── blueprint.md              # 项目蓝图
├── frontend/                 # Electron + React 前端
│   └── src/
│       ├── main/             # Electron 主进程（窗口/托盘/后端进程管理）
│       ├── preload/          # 预加载桥接
│       └── renderer/src/
│           ├── components/   # 悬浮球、终端、确认弹窗
│           ├── pages/        # 系统/文件/应用/存储/量化/研究/对话/Claude/设置
│           ├── services/     # API + WebSocket 客户端
│           ├── store/        # Zustand 状态
│           └── types/        # 共享类型
└── backend/                  # Python FastAPI 后端
    ├── app/
    │   ├── routers/          # REST API（system/files/apps/storage/quant/research/ai/claude_code）
    │   ├── services/         # 业务逻辑
    │   ├── tools/            # AI Function Calling 工具注册
    │   ├── models/           # Pydantic 模型
    │   ├── db.py             # SQLite
    │   ├── ws.py             # WebSocket 事件总线
    │   └── main.py           # 入口
    └── requirements.txt
```

## 🚀 快速开始

### 后端

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
# 可选：知识库/GPU/WMI 集成
pip install -r requirements-optional.txt

cp .env.example .env          # 填入 LLM API Key 等
python run.py                 # 默认 http://127.0.0.1:8000
```

健康检查：`GET http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev                   # 开发模式（自动拉起悬浮球 + 主界面）
```

> 前端主进程会自动拉起 Python 后端（可用 `PAICC_MANAGE_BACKEND=0` 关闭自动管理，改为手动启动后端）。
> 全局快捷键 `Alt+Space` 呼出/隐藏输入框。

### 打包安装包

```bash
cd frontend
npm run build:win             # 生成 NSIS 安装包到 frontend/release/
```

## ⚙️ 配置

所有配置可通过**设置页**（运行时，写入 SQLite）或环境变量 / `.env` 覆盖。关键项：

| 键 | 说明 | 默认 |
|----|------|------|
| `llm_provider` | `deepseek` / `openai` / `claude` / `ollama` / `custom` | `deepseek` |
| `llm_model` | 模型名 | `deepseek-chat` |
| `llm_base_url` | API 地址 | `https://api.deepseek.com` |
| `llm_api_key` | API Key | 空 |
| `quant_root` | 量化项目根目录 | `C:\Users\wyxwi\Desktop\FQA` |
| `everything_path` | Everything CLI 路径 | `es.exe` |
| `smtp_*` | 报告邮件 SMTP | 空（未配置则跳过） |

## 🔐 安全模型

- 所有**修改类操作**（删除、清理、卸载、执行未知脚本、结束进程）必须经用户确认弹窗批准。
- 确认默认 **30 秒超时自动拒绝**。
- AI 默认只读；修改需用户批准（Function Calling 层拦截，返回确认请求）。
- 全部操作记录到 SQLite `operation_logs` 表。

## 🗺️ 开发阶段

蓝图共 8 个阶段（约 18 周）。当前仓库实现了阶段 1–7 的核心功能骨架与阶段 8 的打包配置，量化红线、研究台、Claude Code 等模块对缺失的外部依赖（Everything、Chroma、GPU、claude CLI 等）做了**优雅降级**。

## 📄 License

MIT
