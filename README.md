# Agent Lite

一个轻量级、可交互的 AI Agent 示例项目。它通过 **DeepSeek 的 Function Calling 能力**，让大语言模型能够自主调用本机的文件读写、命令执行等工具，在终端中以对话的方式完成各类任务。

> 本项目是一个教学型 / 入门级 Agent 实现，代码精简（约 150 行），非常适合用来理解「大模型 + 工具调用 = Agent」的核心原理。

---

## ✨ 功能特性

- 🤖 **真正的 Agent 循环**：模型可自主决定是否调用工具，并在工具返回结果后继续推理，直到给出最终回答
- 📂 **文件工具**：`read` 读取文件、`write` 写入文件、`edit` 精确替换文件内容
- 🖥️ **Shell 工具**：`bash` 执行任意系统命令并返回 stdout / stderr
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
├── main.py          # 主程序：工具定义、工具实现、Agent 循环、CLI 入口
└── README.md        # 项目说明文档
```

---

## 🚀 快速开始

### 1. 环境要求

- Python 3.10+
- 一个 [DeepSeek 开放平台](https://platform.deepseek.com/) 的 API Key

### 2. 安装依赖

```bash
pip install openai
```

### 3. 配置 API Key（推荐）

**强烈建议**将 API Key 通过环境变量传入，避免硬编码在代码中泄露：

```bash
# Windows (PowerShell)
$env:DEEPSEEK_API_KEY = "sk-xxxx"

# Linux / macOS
export DEEPSEEK_API_KEY="sk-xxxx"
```

然后在 `main.py` 中修改：

```python
import os
client = OpenAI(
    api_key=os.getenv("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com",
)
```

### 4. 运行

```bash
python main.py
```

在 `>` 提示符后输入任务即可，例如：

```text
> 读取当前目录下的 main.py，并把函数说明翻译成英文写进 notes.md
```

---

## ⚠️ 安全须知

- `bash` 工具会以**当前用户权限**执行任意命令，请勿在不受信任的环境中运行，也不要让 AI 接触敏感系统
- 请勿将 API Key 硬编码进代码并提交到 Git 仓库（`main.py` 中目前留有示例 Key，建议立即移除并改用环境变量）
- 本项目的工具没有沙箱、权限校验和路径限制，仅适合本地学习与实验

---

## 🔭 扩展思路

- 增加更多工具：网络请求（`requests`）、数据库查询、图片处理等
- 为工具加入路径白名单 / 命令黑名单等安全校验
- 使用 `stream=True` 实现流式输出，提升交互体验
- 持久化对话历史，支持多轮记忆

---

## 📄 License

仅供学习交流使用，请遵守 [DeepSeek 服务条款](https://platform.deepseek.com/terms)。
