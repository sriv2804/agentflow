import asyncio
from unittest.mock import MagicMock

from src.core.memory import ScratchPad
from src.tools.common import ToolContext
from src.tools.skill_retriever import skill_retriever


def make_ctx(skill_names=(), query_result=None):
    store = MagicMock()
    store.list_all_skills.return_value = list(skill_names)
    store.query.return_value = query_result
    return ToolContext(skill_store=store, scratchpad=ScratchPad())


def call(ctx, **kwargs):
    return asyncio.run(skill_retriever(_ctx=ctx, **kwargs))


def test_view_with_no_skills_says_so():
    ctx = make_ctx()
    assert call(ctx, mode="view") == "No skills saved yet."
    assert ctx.scratchpad.active_skill == ""


def test_view_lists_names_without_touching_active_skill():
    ctx = make_ctx(skill_names=["skill_a", "skill_b"])
    assert call(ctx, mode="view") == "skill_a\nskill_b"
    assert ctx.scratchpad.active_skill == ""


def test_get_sets_active_skill_and_returns_confirmation():
    ctx = make_ctx(query_result="# Skill: skill_a\n## Steps\n1. do x")
    result = call(ctx, mode="get", query_str="compare two tools")
    assert "[ACTIVE SKILL]" in result
    assert ctx.scratchpad.active_skill.startswith("# Skill: skill_a")


def test_get_with_no_match_leaves_active_skill_empty():
    ctx = make_ctx(query_result=None)
    assert call(ctx, mode="get", query_str="anything") == "No matching skill found."
    assert ctx.scratchpad.active_skill == ""
