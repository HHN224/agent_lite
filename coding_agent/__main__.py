import os
import sys
from pathlib import Path

# 支持直接运行本文件（如 VS Code 运行按钮）：把项目根目录加入 sys.path，
# 否则找不到 agent_core、coding_agent 等同级包。推荐入口仍是 python -m coding_agent。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from ai import OpenAIProvider
from agent_core import Agent, AgentLoop
from coding_agent.tools import tools


SYSTEM_PROMPT = "You are a helpful AI agent with file and shell tools. Use them when needed, then answer concisely in the user's language."

# 项目根目录下的 .env 文件（无论从哪里运行都能加载到）
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def main():
    load_dotenv(ENV_FILE)

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit(
            "错误：未检测到 DEEPSEEK_API_KEY。\n"
            "请在项目根目录的 .env 文件中写入 DEEPSEEK_API_KEY=sk-xxxx（可参考 .env.example），\n"
            '或设置环境变量：$env:DEEPSEEK_API_KEY = "sk-xxxx"'
        )

    # 自底向上组装：ai 层 Provider → agent_core 循环 → Agent 对话状态
    provider = OpenAIProvider(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )

    loop = AgentLoop(
        provider=provider,
        model="deepseek-v4-flash",
        tools=tools,
    )

    agent = Agent(
        loop=loop,
        system_prompt=SYSTEM_PROMPT,
    )

    while True:
        user_input = input("> ")
        agent.prompt(user_input)  # 回复已在循环内流式打印，这里只负责结束行
        print()


if __name__ == "__main__":
    main()
