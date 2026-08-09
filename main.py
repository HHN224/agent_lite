from pathlib import Path
import os
import subprocess
from openai import OpenAI

from agent_core import AgentLoop
from ai import tools


WORKSPACE = Path(__file__).resolve().parent


def safe_path(path: str) -> Path:
    """把路径解析到工作目录内，越界则抛 PermissionError。"""
    file = (WORKSPACE / path).resolve()

    if file != WORKSPACE and WORKSPACE not in file.parents:
        raise PermissionError(f"不能访问工作目录之外的文件: {path}")

    return file


def to_schema() -> list[dict]:
    """把 ai 层工具定义转换成 OpenAI function calling 的 schema。"""
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


def read(path: str) -> str:
    """读取文本文件内容。"""
    try:
        file = safe_path(path)

        if not file.exists():
            return f"Error: file not found: {path}"

        if not file.is_file():
            return f"Error: not a file: {path}"

        return file.read_text(encoding="utf-8")

    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


def write(path: str, content: str) -> str:
    """写入文本文件内容。"""
    try:
        file = safe_path(path)
        file.write_text(content, encoding="utf-8")
        return f"Successfully wrote to {path}"
    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


def bash(command: str) -> str:
    """运行 shell 命令并返回 stdout/stderr。"""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr
        return output.strip() or f"(exit code {result.returncode}, no output)"
    except Exception as e:
        return f"Error: {e}"


def edit(path: str, old_string: str, new_string: str) -> str:
    """将文本文件中精确匹配的子串替换为新内容。"""
    try:
        file = safe_path(path)

        if not file.exists():
            return f"Error: file not found: {path}"

        if not file.is_file():
            return f"Error: not a file: {path}"

        content = file.read_text(encoding="utf-8")

        if content.count(old_string) == 0:
            return f"Error: old_string not found in {path}"

        if content.count(old_string) > 1:
            return f"Error: old_string matches {content.count(old_string)} times in {path}; it must be unique"

        file.write_text(content.replace(old_string, new_string), encoding="utf-8")
        return f"Successfully edited {path}"
    except PermissionError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {e}"


tool_map = {
    "read": read,
    "write": write,
    "bash": bash,
    "edit": edit,
}

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