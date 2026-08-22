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
from coding_agent.tools import build_tools


SYSTEM_PROMPT = "You are a helpful AI agent with file and shell tools. Use them when needed, then answer concisely in the user's language."


def safe_print(s: str, **kwargs):
    """按终端编码打印，无法编码的字符用替换符兜底，避免 GBK 终端上 print 抛 UnicodeEncodeError。"""
    try:
        print(s, **kwargs)
    except UnicodeEncodeError:
        print(s.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8"), **kwargs)


def cli_listener(event: AgentEvent):
    """把 Agent 的事件流渲染成终端输出（复刻原来的 >>> 提示格式）。"""
    if event.type == "turn_start":
        print("\n>>> 调用 API ...")
    elif event.type == "message_update":
        safe_print(event.data["content"], end="", flush=True)
    elif event.type == "message_end":
        print()
    elif event.type == "tool_execution_start":
        safe_print(f">>> 正在使用工具: {event.data['name']} | 参数: {event.data['arguments']}")
    elif event.type == "tool_execution_end":
        safe_print(f">>> 工具返回: {event.data['content'][:200]}")
    elif event.type == "error":
        safe_print(f"\n>>> 模型服务出错: {event.data['message']}")

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
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="工作目录：文件工具与 bash 沙箱的活动范围（默认当前目录）",
    )
    parser.add_argument(
        "--model",
        default="deepseek-v4-flash",
        help="模型名（默认 deepseek-v4-flash）",
    )
    parser.add_argument(
        "--base-url",
        default="https://api.deepseek.com",
        help="OpenAI 兼容 API 的 base_url（默认 DeepSeek）",
    )
    parser.add_argument(
        "--permission-policy",
        choices=["ask", "deny", "auto"],
        default="ask",
        help="危险工具的权限策略：ask 每次确认 / deny 直接拒绝 / auto 自动放行（默认 ask）",
    )
    parser.add_argument(
        "--bash-image",
        default="python:3.12-slim",
        help="bash 工具使用的 Docker 运行镜像（默认 python:3.12-slim）",
    )
    args = parser.parse_args(argv)

    if not SESSION_NAME_RE.match(args.session):
        parser.error("会话名只能包含字母、数字、下划线和连字符，长度 1-64")

    args.workspace = args.workspace.resolve()
    if not args.workspace.is_dir():
        parser.error(f"工作目录不存在: {args.workspace}")

    return args


def make_confirm():
    """生成权限确认函数：展示工具调用的人类可读描述，等待用户 y/N。
    非交互环境（EOF / Ctrl+C）一律视为拒绝，保证不产生副作用。"""

    def confirm(description: str) -> bool:
        try:
            answer = input(f">>> {description}\n>>> 是否允许执行？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return answer in ("y", "yes")

    return confirm


def main():
    args = parse_args()

    # 优先加载项目根目录 .env（源码直跑场景），再兜底加载当前目录 .env（全局安装后任意目录启动）
    load_dotenv(ENV_FILE)
    load_dotenv()

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
        base_url=args.base_url,
    )

    tools = build_tools(args.workspace, bash_image=args.bash_image)

    loop = AgentLoop(
        provider=provider,
        model=args.model,
        tools=tools,
        permission_policy=args.permission_policy,
        confirm=make_confirm(),
    )

    store = SessionStore(SESSIONS_DIR / f"{args.session}.json")

    print(f">>> 会话: {args.session}（存档: {store.path}）")
    print(f">>> 工作目录: {args.workspace}")
    print(f">>> 权限策略: {args.permission_policy}（bash 镜像: {args.bash_image}）")

    agent = Agent(
        loop=loop,
        system_prompt=SYSTEM_PROMPT,
        store=store,
        resume=not args.new,
    )

    while True:
        try:
            user_input = input("> ")
        except (KeyboardInterrupt, EOFError):
            print("\n再见！")
            break

        if user_input.strip() == "/clear":
            agent.clear_history()
            print(">>> 已清空对话历史")
            continue

        # 执行阶段：Ctrl+C 触发 abort（中止本轮），而非退出 REPL
        gen = agent.prompt(user_input)
        try:
            for event in gen:
                cli_listener(event)
        except KeyboardInterrupt:
            print("\n>>> 已中止")
            loop.abort()
            # 排空生成器剩余事件，让 loop 在下个检查点安全退出
            try:
                for event in gen:
                    cli_listener(event)
            except KeyboardInterrupt:
                pass
        except Exception:
            # 任何未预期错误都只跳过本轮，不退出会话
            print("\n发生未预期错误，已跳过本轮：")
            traceback.print_exc()


if __name__ == "__main__":
    main()
