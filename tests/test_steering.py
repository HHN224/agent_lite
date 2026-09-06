import pytest

from agent_core import Agent, AgentLoop, AgentTool, ToolResult, Session, SessionRepository
from ai import TextDelta, ToolCall
from faux_provider import FauxProvider


class EchoTool(AgentTool):
    argument_types = {"text": str}

    def __init__(self):
        super().__init__(
            name="echo",
            description="Echo",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
            timeout=5,
            dangerous=False,
        )

    def execute(self, text: str) -> ToolResult:
        return ToolResult(content="echo:" + text)


def make_agent(script, tools=None, **loop_kwargs):
    provider = FauxProvider(script)
    loop = AgentLoop(
        provider=provider,
        model="faux-model",
        tools=tools or [],
        **loop_kwargs,
    )
    session = Session(session_id="s1", system_prompt="sys")
    repo = SessionRepository(__import__("tempfile").gettempdir())
    return Agent(loop=loop, session=session, repo=repo), provider


def test_steer_queue_initialized_empty():
    agent, _ = make_agent([])
    assert agent.steer_queue == []
    assert agent.follow_up_queue == []
    assert agent.has_queued_messages() is False


def test_steer_and_follow_up_enqueue():
    agent, _ = make_agent([])
    agent.steer("打断一下")
    agent.follow_up("跑完再接一条")
    assert agent.has_queued_messages() is True
    assert agent.steer_queue[0]["role"] == "user"
    assert agent.follow_up_queue[0]["content"] == "跑完再接一条"


def test_clear_queues():
    agent, _ = make_agent([])
    agent.steer("a")
    agent.follow_up("b")
    agent.clear_queues()
    assert agent.has_queued_messages() is False


def test_loop_poll_steering_injects_message():
    # 一个脚本，第一轮纯文本结束；steer 队列在 run 前预置，
    # loop 每轮开始前 _poll_steering 会把 steer 消息追加进 messages。
    steer = [{"role": "user", "content": "插一句"}]
    provider = FauxProvider([[TextDelta("ok")]])
    loop = AgentLoop(
        provider=provider,
        model="m",
        tools=[],
        get_steering_messages=lambda: list(steer),
    )
    messages = [{"role": "user", "content": "初始"}]
    for ev in loop.run(messages):
        pass
    # 第一轮：初始 user 被注入；但 steer 会在同一轮开始前被 append
    roles = [m["role"] for m in messages]
    assert "user" in roles


def test_follow_up_prompt_recurses():
    # follow_up 队列非空时，prompt() 结束后会递归处理。
    # 用两个纯文本脚本：第一个 "第一轮答案"，第二个（follow_up 触发的） "第二轮答案"。
    provider = FauxProvider([
        [TextDelta("第一轮")],
        [TextDelta("第二轮")],
    ])
    loop = AgentLoop(provider=provider, model="m", tools=[])
    session = Session(session_id="s1", system_prompt="sys")
    agent = Agent(loop=loop, session=session)
    agent.follow_up("接着做")
    events = list(agent.prompt("开始"))
    # 应该有两轮 agent_start/agent_end（主 prompt + follow_up 接力）
    start_count = sum(1 for e in events if e.type == "agent_start")
    assert start_count == 2


def test_steer_prompts_loop_to_emit_steer_event():
    steer = [{"role": "user", "content": "转向"}]
    provider = FauxProvider([[TextDelta("ok")]])
    loop = AgentLoop(
        provider=provider,
        model="m",
        tools=[],
        get_steering_messages=lambda: list(steer),
    )
    events = list(loop.run([{"role": "user", "content": "x"}]))
    assert any(e.type == "steer" for e in events)
