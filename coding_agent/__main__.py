import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from agent_core import AgentLoop
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

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )

    loop = AgentLoop(
        client=client,
        model="deepseek-v4-flash",
        system_prompt=SYSTEM_PROMPT,
        tools=tools,
    )

    while True:
        user_input = input("> ")
        print(loop.run(user_input))


if __name__ == "__main__":
    main()
