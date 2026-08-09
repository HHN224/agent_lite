import os

from openai import OpenAI

from agent_core import AgentLoop
from coding_agent.tools import tools


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
        tools=tools,
    )

    while True:
        user_input = input("> ")
        print(loop.run(user_input))


if __name__ == "__main__":
    main()
