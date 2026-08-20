import argparse
import os
import re
import sys
import traceback
from pathlib import Path

# 支持直接运行本文件（如 VS Code 运行按钮）：把项目根目录加入 sys.path，
# 否则找不到 agent_core、coding_agent 等同级包。推荐入口仍是 python -m coding_agent。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from ai import OpenAIProvider
from agent_core import Agent, AgentEvent, AgentLoop, SessionStore
from coding_agent.tools import tools


SYSTEM_PROMPT = "You are a helpful AI agent with file and shell tools. Use them when needed, then answer concisely in the user's language."


def cli_listener(event: AgentEvent):
    """把 AgentLoop 的事件流渲染成终端输出（复刻原来的 >>> 提示格式）。"""
    if event.type == "api_start":
        print("\n>>> 调用 API ...")
    elif event.type == "text_delta":
        print(event.data["content"], end="", flush=True)
    elif event.type == "text_end":
        print()
    elif event.type == "tool_start":
        print(f">>> 正在使用工具: {event.data['name']} | 参数: {event.data['arguments']}")
    elif event.type == "tool_end":
        print(f">>> 工具返回: {event.data['content'][:200]}")
    elif event.type == "error":
        print(f"\n>>> 模型服务出错: {event.data['message']}")

# 项目根目录下的 .env 文件（无论从哪里运行都能加载到）
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# 会话存档目录：sessions/<会话名>.json
SESSIONS_DIR = Path(__file__).resolve().parent.parent / "sessions"

# 会话名白名单，防止 --session ../../xxx 之类的路径越界
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Agent Lite 交互式 REPL")
    parser.add_argument(
        "--session",
        default="default",
        help="会话名，对应 sessions/<name>.json（默认 default）",
    )
    parser.add_argument(
        "--new",
        action="store_true",
        help="不恢复历史，从新会话开始（仍会存档到同一文件）",
    )
    args = parser.parse_args(argv)

    if not SESSION_NAME_RE.match(args.session):
        parser.error("会话名只能包含字母、数字、下划线和连字符，长度 1-64")

    return args


def main():
    args = parse_args()

    load_dotenv(ENV_FILE)

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit(
            "错误：未检测到 DEEPSEEK_API_KEY。\n"
            "请在项目根目录的 .env 文件中写入 DEEPSEEK_API_KEY=sk-xxxx（可参考 .env.example），\n"
            '或设置环境变量：$env:DEEPSEEK_API_KEY = "sk-xxxx"'
        )

    # 自底向上组装：ai 层 Provider → agent_core 循环 → Agent 对话状态 + 会话存档
    provider = OpenAIProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )

    loop = AgentLoop(
        provider=provider,
        model="deepseek-v4-flash",
        tools=tools,
    )
    loop.add_listener(cli_listener)

    store = SessionStore(SESSIONS_DIR / f"{args.session}.json")

    print(f">>> 会话: {args.session}（存档: {store.path}）")

    agent = Agent(
        loop=loop,
        system_prompt=SYSTEM_PROMPT,
        store=store,
        resume=not args.new,
    )

    while True:
        try:
            user_input = input("> ")
            if user_input.strip() == "/clear":
                agent.clear_history()
                print(">>> 已清空对话历史")
                continue
            agent.prompt(user_input)  # 回复已在循环内流式打印，这里只负责结束行
            print()
        except (KeyboardInterrupt, EOFError):
            print("\n再见！")
            break
        except Exception:
            # 任何未预期错误都只跳过本轮，不退出会话
            print("\n发生未预期错误，已跳过本轮：")
            traceback.print_exc()


if __name__ == "__main__":
    main()
