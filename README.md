# Agent Lite

一个轻量级、可交互的 AI Agent 示例项目。它通过 **DeepSeek 的 Function Calling 能力**，让大语言模型能够自主调用本机的文件读写、命令执行等工具，在终端中以对话的方式完成各类任务。

> 本项目是一个教学型 / 入门级 Agent 实现，代码精简（约 150 行），非常适合用来理解「大模型 + 工具调用 = Agent」的核心原理。

---

## ✨ 功能特性

- 🤖 **真正的 Agent 循环**：模型可自主决定是否调用工具，并在工具返回结果后继续推理，直到给出最终回答
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

## 📁 项目结构

```
agent lite/
├── ai/               # 最底层：Tool 数据类（name / description / parameters → schema）
│   └── tools.py
├── agent_core/       # 核心层：AgentLoop（对话循环）与 AgentTool 抽象接口
│   ├── loop.py
│   └── agent_tools.py
├── coding_agent/     # 应用层：四个具体工具 + CLI 入口
│   ├── tools.py      # read / write / bash / edit
│   └── __main__.py   # 入口：python -m coding_agent
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
pip install openai python-dotenv
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
python -m coding_agent
```

在 `>` 提示符后输入任务即可，例如：

```text
> 读取当前目录下的 README.md，并把项目说明翻译成英文写进 notes.md
```

---

## ⚠️ 安全须知

- `bash` 工具在 Docker 沙箱中运行（断网 + 资源限额），但工作目录以可写方式挂载进容器，AI 仍可通过命令修改项目内文件
- 请勿将 API Key 硬编码进代码并提交到 Git 仓库（当前代码已从环境变量 `DEEPSEEK_API_KEY` 读取密钥）
- 文件工具通过 `safe_path` 限制在工作目录内，但本项目的工具仍没有完整的权限校验，仅适合本地学习与实验

---

## 🔭 扩展思路

- 增加更多工具：网络请求（`requests`）、数据库查询、图片处理等
- 为工具加入路径白名单 / 命令黑名单等安全校验
- 使用 `stream=True` 实现流式输出，提升交互体验
- 持久化对话历史，支持多轮记忆

---

## 📄 License

仅供学习交流使用，请遵守 [DeepSeek 服务条款](https://platform.deepseek.com/terms)。
