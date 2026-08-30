# Agent Lite

一个轻量级、可交互的 AI Agent 示例项目。它通过 **DeepSeek 的 Function Calling 能力**，让大语言模型能够自主调用本机的文件读写、命令执行等工具，在终端中以对话的方式完成各类任务。

> 本项目是一个教学型 / 入门级 Agent 实现，代码精简（约 150 行），非常适合用来理解「大模型 + 工具调用 = Agent」的核心原理。

---

## ✨ 功能特性

- 🤖 **真正的 Agent 循环**：模型可自主决定是否调用工具，并在工具返回结果后继续推理，直到给出最终回答
- ⚡ **流式输出**：回复按 token 实时打印，工具调用过程同步可见
- 🛡️ **循环健壮性**：工具调用轮数上限、工具错误回填模型、API 故障不崩溃（统一 `ProviderError`）、Ctrl+C 优雅退出
- 💾 **会话持久化**：每轮对话自动存档为 JSON，重启自动恢复历史，支持多会话与 `/clear` 清空
- 📂 **文件工具**：`read` 读取文件、`write` 写入文件、`edit` 精确替换文件内容
- 🖥️ **Shell 工具**：`bash` 在一次性 Docker 沙箱容器中执行命令（无网络、只读根文件系统、512MB 内存 / 100 进程限额），工作目录通过 bind mount 挂载到容器 `/workspace`
- 💬 **交互式 REPL**：终端中输入提示词即可与 Agent 持续对话
- 🔧 **接口兼容 OpenAI**：使用 `openai` SDK 接入 DeepSeek API，替换 `base_url` 即可迁移到其他兼容服务

---

## 🧩 工作原理

```
用户输入 ──► OpenAI/DeepSeek API（携带工具定义）
                 │
                 ▼
           模型返回两种结果之一
        ┌────────┴────────┐
        ▼                 ▼
   有工具调用           无工具调用
        │                 │
  执行对应工具           输出最终回答
        │                 │
  结果回传给模型 ◄────────┘
        └── 循环，直到模型不再调用工具
```

核心依赖 `tools`（JSON Schema 工具定义）与 `tool_map`（工具名 → Python 函数）的映射关系，让模型能以结构化参数安全地触发本地函数。

---

## 🏗️ 分层架构（来自 pi-agent）

本项目严格遵循 **自底向上分层，底层不能调用上层**，上层只能依赖紧邻的下层：

```
┌─────────────────────────────────────────────┐
│  coding_agent  应用层（最上层）                │
│  · 具体工具：read / write / bash / edit      │
│  · CLI 入口：main 里组装 AgentLoop + Agent   │
├─────────────────────────────────────────────┤
│  agent_core    核心层                        │
│  · Agent      管理对话状态（messages / system│
│               prompt）与会话持久化，对外只   │
│               暴露 prompt()                   │
│  · AgentLoop  只管"接收 messages → 经 Provider │
│               流式调模型 → 执行工具 → 回填结果"  │
│               的机械循环，不接触具体 API SDK     │
│  · AgentTool  具体工具必须实现的 execute 契约  │
├─────────────────────────────────────────────┤
│  ai           最底层                          │
│  · Tool       工具数据类（name/description/  │
│               parameters → OpenAI schema）   │
│  · Provider   LLMProvider 流式调用契约 +     │
│               OpenAI 兼容实现                │
└─────────────────────────────────────────────┘
```

- **最上层是 coding_agent，往下一层是 agent_core，最下面一层是 ai**
- 上下层只通过 `from agent_core import ...` / `from ai import ...` 这类导入建立依赖，方向始终向下
- 将来做消息压缩、改写、持久化记忆时，都放在 **agent_core 的 Agent 里**，不要让 AgentLoop 关心上下文管理

---

## 📁 项目结构

```
agent lite/
├── ai/               # 最底层：Tool 数据类与 Provider 流式调用契约
│   ├── tools.py      # Tool（name / description / parameters → schema）
│   └── providers.py  # LLMProvider 抽象 + OpenAI 兼容流式实现
├── agent_core/       # 核心层：Agent（对话状态 + 会话持久化）、AgentLoop（模型循环）、ToolExecutor（统一工具执行器）、AgentTool 抽象接口
│   ├── agent.py
│   ├── loop.py
│   ├── tool_executor.py  # 参数校验 → 权限策略（ask/deny/auto）→ 超时 → 审计 → 结果规范化
│   ├── session.py    # SessionStore：会话状态 ↔ JSON 存档
│   └── agent_tools.py
├── coding_agent/     # 应用层（最上层）：四个具体工具 + CLI 入口
│   ├── tools.py      # read / write / bash / edit（build_tools 工厂按工作目录组装）
│   └── __main__.py   # 入口：agent-lite 命令 / python -m coding_agent
├── tests/            # pytest 测试：FauxProvider 确定性驱动 AgentLoop + 工具/会话单测
├── sessions/         # 会话存档（每轮自动保存，已 gitignore）
├── pyproject.toml    # 打包配置 + agent-lite console script
└── README.md         # 项目说明文档
```

---

## 🚀 快速开始

### 1. 环境要求

- Python 3.10+
- Docker Desktop（`bash` 工具的沙箱运行环境，需启动守护进程并拉取 `python:3.12-slim` 镜像）
- 一个 [DeepSeek 开放平台](https://platform.deepseek.com/) 的 API Key

### 2. 安装依赖

```bash
# 推荐：可编辑安装（含 console script `agent-lite`，任意目录可启动）
pip install -e .[dev]

# 或仅安装运行依赖
pip install -r requirements.txt
```

### 3. 配置 API Key（必需）

**方式一：`.env` 文件（推荐）** —— 复制 `.env.example` 为 `.env`，填入真实 Key 即可，程序启动时自动加载（`.env` 已被 `.gitignore` 忽略，不会泄露到仓库）：

```bash
DEEPSEEK_API_KEY=sk-xxxx
```

**方式二：环境变量** —— 代码最终仍从环境变量 `DEEPSEEK_API_KEY` 读取，直接设置也同样有效：

```bash
# Windows (PowerShell)
$env:DEEPSEEK_API_KEY = "sk-xxxx"

# Linux / macOS
export DEEPSEEK_API_KEY="sk-xxxx"
```

### 4. 运行

```bash
agent-lite
# 或从源码直接运行
python -m coding_agent
```

在 `>` 提示符后输入任务即可，例如：

```text
> 读取当前目录下的 README.md，并把项目说明翻译成英文写进 notes.md
```

每轮对话自动存档到 `sessions/default.json`，重启后自动恢复历史。可选参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--session NAME` | `default` | 使用不同会话（存档为 `sessions/NAME.json`） |
| `--new` | — | 忽略已有历史，从新会话开始 |
| `--workspace PATH` | 当前目录 | 工作目录：文件工具与 bash 沙箱的活动范围 |
| `--model NAME` | `deepseek-v4-flash` | 模型名 |
| `--base-url URL` | `https://api.deepseek.com` | OpenAI 兼容 API 的 base_url（可切换到其他兼容服务） |
| `--permission-policy MODE` | `ask` | 危险工具（写文件 / 编辑 / bash）的权限策略：`ask` 每次询问、`deny` 直接拒绝、`auto` 自动放行 |
| `--bash-image IMAGE` | `python:3.12-slim` | `bash` 工具使用的 Docker 运行镜像 |

对话中输入 `/clear` 清空当前会话历史。

### 5. 运行测试

```bash
pip install -e .[dev]
pytest
```

---

## ⚠️ 安全须知

- `bash` 工具在 Docker 沙箱中运行（断网 + 资源限额），但工作目录以可写方式挂载进容器，AI 仍可通过命令修改项目内文件
- **权限策略**（`--permission-policy`，默认 `ask`）：写文件、精确编辑与 bash 属危险工具，每次调用都会展示「路径 / 变更摘要 / 命令」并等你确认；`deny` 直接拒绝、`auto` 自动放行。被拒绝的操作以错误结果回传模型，不会产生副作用
- 请勿将 API Key 硬编码进代码并提交到 Git 仓库（当前代码已从环境变量 `DEEPSEEK_API_KEY` 读取密钥）
- 文件工具通过 `safe_path` 限制在工作目录内；`auto` 模式下无人工把关，仅适合完全可信的本地实验场景

---

## 🔭 扩展思路

- 增加更多工具：网络请求（`requests`）、数据库查询、图片处理等
- 为工具加入命令黑名单 / 白名单等更细粒度的安全校验
- 增加上下文压缩（超长对话自动总结），见 pi 的 compaction 设计

---

## 📄 License

仅供学习交流使用，请遵守 [DeepSeek 服务条款](https://platform.deepseek.com/terms)。


<!-- 111今日打卡今日打卡111111又来打卡了1112222-->