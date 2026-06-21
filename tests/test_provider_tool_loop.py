"""
test_provider_tool_loop.py — ModelRouter.complete_with_tools now runs a REAL OpenAI-compatible
tool loop for non-Anthropic models (it used to degrade them to a text-only completion, so the
domestic report-team agents could never call the deliverables file builders).

Network-free: the openai SDK is monkeypatched. Run from the project root:

    .venv/bin/python -m pytest tests/test_provider_tool_loop.py -v
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent_team import providers  # noqa: E402


def _fake_openai_tool_then_text(captured, tool_name="create_excel_workbook"):
    """A fake openai.OpenAI: returns a tool_call on the first create(), a final text on the
    second (a minimal 2-step function-calling exchange)."""
    state = {"calls": 0}

    class _Completions:
        def create(self, **kw):
            captured.setdefault("tools", kw.get("tools"))
            state["calls"] += 1
            if state["calls"] == 1:
                tc = type("TC", (), {"id": "t1", "type": "function",
                                     "function": type("F", (), {"name": tool_name,
                                                                "arguments": json.dumps({"title": "X"})})()})()
                msg = type("M", (), {"content": None, "tool_calls": [tc]})()
            else:
                msg = type("M", (), {"content": "done", "tool_calls": None})()
            return type("R", (), {"choices": [type("C", (), {"message": msg})()],
                                  "usage": type("U", (), {"prompt_tokens": 7, "completion_tokens": 3})()})()

    class _Client:
        def __init__(self, **kw):
            self.chat = type("Chat", (), {"completions": _Completions()})()
    return _Client


def test_model_router_runs_openai_tool_loop(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    import openai
    captured = {}
    monkeypatch.setattr(openai, "OpenAI", _fake_openai_tool_then_text(captured))
    ran = {}

    def executor(name, args):
        ran.update(name=name, args=args)
        return "ok, file saved."

    specs = [{"name": "create_excel_workbook", "description": "make a workbook",
              "input_schema": {"type": "object", "properties": {"title": {"type": "string"}}}}]
    text = providers.ModelRouter().complete_with_tools(
        "sys", "做个表", specs, executor, model="deepseek-v4-flash", max_tokens=500)
    assert text == "done"                              # the final assistant text
    assert ran["name"] == "create_excel_workbook" and ran["args"] == {"title": "X"}  # tool ran
    # the deliverables (Anthropic-format) specs were translated to OpenAI's function shape
    assert captured["tools"][0]["type"] == "function"
    assert captured["tools"][0]["function"]["name"] == "create_excel_workbook"


def test_anthropic_path_unchanged(monkeypatch):
    """A Claude model still routes to the native Anthropic tool loop (not the OpenAI one)."""
    called = {}
    monkeypatch.setattr(providers.ModelRouter, "_anthropic_client",
                        lambda self: type("C", (), {
                            "complete_with_tools": lambda *a, **k: (called.update(native=True), "native-text")[1],
                            "last_stop_reason": None})())
    text = providers.ModelRouter().complete_with_tools(
        "sys", "hi", [], lambda n, a: "", model="claude-haiku-4-5")
    assert text == "native-text" and called.get("native")
