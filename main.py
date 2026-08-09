from pathlib import Path
import json
import os
import subprocess
from openai import OpenAI


WORKSPACE = Path(__file__).resolve().parent


def safe_path(path: str) -> Path:
    """把路径解析到工作目录内，越界则抛 PermissionError。"""
    file = (WORKSPACE / path).resolve()

    if file != WORKSPACE and WORKSPACE not in file.parents:
        raise PermissionError(f"不能访问工作目录之外的文件: {path}")

    return file


tools = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a text file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "description": "Write content to a text file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                    },
                    "content": {
                        "type": "string",
                    }
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command and return its stdout/stderr",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit",
            "description": "Replace an exact substring in a text file with new content",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                    },
                    "old_string": {
                        "type": "string",
                    },
                    "new_string": {
                        "type": "string",
                    }
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    }
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


def agent(prompt: str):
    messages = [
        {"role": "system", "content": "You are a helpful AI agent with file and shell tools. Use them when needed, then answer concisely in the user's language."},
        {"role": "user", "content": prompt},
    ]

    while True:
        print(f"\n>>> 调用 API ...")
        response = client.chat.completions.create(
            model="deepseek-v4-flash",
            messages=messages,
            tools=tools,
        )

        message = response.choices[0].message
        messages.append(message)

        # 没有工具调用，说明模型已经回答完了
        if not message.tool_calls:
            print(f">>> 最终回复: {message.content}")
            return message.content

        # 执行工具
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            args = json.loads(tool_call.function.arguments)

            print(f">>> 正在使用工具: {name} | 参数: {args}")
            result = tool_map[name](**args)
            print(f">>> 工具返回: {str(result)[:200]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result,
            })




def main():
    while True:
        user_input = input("> ")
        print(agent(user_input))

if __name__ == "__main__":
    main()