import pytest
from src.core.memory import MemoryManager


def test_append_msg_adds_message():
    mm = MemoryManager()
    mm.append_msg(role="user", content="hello")
    assert mm.conversation_history == [{"role": "user", "content": "hello"}]


def test_append_msg_stores_role_and_content():
    mm = MemoryManager()
    mm.append_msg(role="assistant", content="hi there")
    msg = mm.conversation_history[0]
    assert msg["role"] == "assistant"
    assert msg["content"] == "hi there"


def test_eviction_drops_oldest_when_over_limit():
    mm = MemoryManager(max_short_term=3)
    mm.append_msg("user", "msg1")
    mm.append_msg("user", "msg2")
    mm.append_msg("user", "msg3")
    mm.append_msg("user", "msg4")  # triggers eviction
    assert len(mm.conversation_history) == 3
    assert mm.conversation_history[0]["content"] == "msg2"


def test_get_messages_as_string_contains_role_and_content():
    mm = MemoryManager()
    mm.append_msg("user", "hello")
    result = mm.get_messages(as_string=True)
    assert isinstance(result, str)
    assert "user" in result
    assert "hello" in result


def test_get_messages_as_list_returns_dicts():
    mm = MemoryManager()
    mm.append_msg("assistant", "world")
    result = mm.get_messages(as_string=False)
    assert isinstance(result, list)
    assert any(m["role"] == "assistant" and m["content"] == "world" for m in result)


def test_summary_prepended_as_system_message():
    mm = MemoryManager()
    mm.summary = "This is a summary."
    mm.append_msg("user", "question")
    result = mm.get_messages(as_string=False)
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "This is a summary."


def test_no_system_message_when_summary_empty():
    mm = MemoryManager()
    mm.append_msg("user", "hi")
    result = mm.get_messages(as_string=False)
    assert result[0]["role"] == "user"
