import os
from openai import OpenAI

from agent_core import AgentLoop
from coding_agent import tools, tool_map


def to_schema() -> list[dict]:
    """把工具定义转换成 OpenAI function calling 的 schema。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com",
)


SYSTEM_PROMPT = "You are a helpful AI agent with file and shell tools. Use them when needed, then answer concisely in the user's language."


def main():
    loop = AgentLoop(
        client=client,
        model="deepseek-v4-flash",
        system_prompt=SYSTEM_PROMPT,
        tools=to_schema(),
        tool_map=tool_map,
    )

    while True:
        user_input = input("> ")
        print(loop.run(user_input))

if __name__ == "__main__":
    main()