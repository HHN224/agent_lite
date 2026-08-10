import os
import sys

from openai import OpenAI

from agent_core import AgentLoop
from coding_agent.tools import tools


SYSTEM_PROMPT = "You are a helpful AI agent with file and shell tools. Use them when needed, then answer concisely in the user's language."


def main():
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit(
            "错误：未检测到环境变量 DEEPSEEK_API_KEY。\n"
            '请先设置后再运行，例如：$env:DEEPSEEK_API_KEY = "sk-xxxx"'
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
