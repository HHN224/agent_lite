from agent_core import (
    text_block,
    image_block,
    thinking_block,
    tool_result_block,
    content_to_text,
    content_length,
    content_to_llm,
    is_content_blocks,
)


def test_text_block():
    b = text_block("hello")
    assert b == {"type": "text", "text": "hello"}
    assert is_content_blocks([b])


def test_image_block_encodes_data_uri():
    b = image_block("abc")
    assert b["type"] == "image_url"
    assert b["image_url"]["url"] == "data:image/png;base64,abc"


def test_image_block_preserves_existing_url():
    b = image_block("data:image/png;base64,xyz")
    assert b["image_url"]["url"] == "data:image/png;base64,xyz"


def test_thinking_block():
    b = thinking_block("thinking...")
    assert b == {"type": "thinking", "text": "thinking..."}


def test_tool_result_block():
    b = tool_result_block("out", is_error=True, exit_code=1, stdout="o", stderr="e")
    assert b["type"] == "tool_result"
    assert b["is_error"] is True
    assert b["exit_code"] == 1
    assert b["stdout"] == "o"
    assert b["stderr"] == "e"


def test_content_to_text_plain_str():
    assert content_to_text("hello") == "hello"


def test_content_to_text_concats_text_blocks():
    blocks = [text_block("a"), text_block("b")]
    assert content_to_text(blocks) == "ab"


def test_content_to_text_ignores_thinking():
    blocks = [thinking_block("think"), text_block("ans")]
    assert content_to_text(blocks) == "ans"


def test_content_length_plain_str():
    assert content_length("hello") == 5


def test_content_length_counts_image_equivalent():
    # 纯文本块：累加字符；图片块：给固定当量 4800
    blocks = [text_block("ab"), image_block("data")]
    assert content_length(blocks) == 2 + 4800


def test_content_to_llm_passthrough():
    assert content_to_llm("str") == "str"
    # 单个纯 text 块会被简化为字符串（更省、更广泛支持）
    assert content_to_llm([text_block("x")]) == "x"


def test_content_to_llm_filters_thinking():
    # thinking 块会被过滤掉，只剩 text（若只剩一个 text 则简化为字符串）
    blocks = [{"type": "thinking", "text": "think"}, text_block("ans")]
    assert content_to_llm(blocks) == "ans"


def test_content_to_llm_single_text_returns_string():
    assert content_to_llm([text_block("only")]) == "only"


def test_content_to_llm_keeps_image():
    blocks = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}}, text_block("看图")]
    out = content_to_llm(blocks)
    assert isinstance(out, list)
    assert out[0]["type"] == "image_url"