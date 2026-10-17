"""Off-topic questions must be refused cheaply, and never answered."""
import pytest

from ai_assistant import Assistant, AssistantConfig, LLMError, ToolRegistry
from ai_assistant.scope import local_reply

from test_engine_and_web import FakeClient  # noqa: E402  (tests dir is on sys.path)


def registry():
    r = ToolRegistry()

    @r.tool(description="daily trend")
    def get_trend():
        return [{"day": "2026-10-15", "slots": 28}]
    return r


def make(script, **cfg):
    config = AssistantConfig(api_key="sk-test", title="VFS Assistant",
                             system_context="a slot bot",
                             scope_description="A visa slot-checking bot and its data.",
                             suggestions=("Summarise today",), **cfg)
    client = FakeClient(script)
    return Assistant(config, registry(), client=client), client


TOOL = {"tools": [{"id": "c1", "name": "get_trend", "arguments": "{}"}]}


def kinds(events):
    return [e["type"] for e in events]


@pytest.mark.parametrize("q", ["hi", "Hello!", "thanks bro", "help", "what can you do?", "ok"])
def test_small_talk_is_answered_locally_without_any_model_call(q):
    a, client = make([])
    events = list(a.ask(q))
    assert kinds(events) == ["delta", "done"] and events[-1]["local"]
    assert "Summarise today" in events[0]["text"]
    assert client.seen == []


def test_local_reply_does_not_swallow_real_questions():
    assert local_reply("hi, why did AE-GRC fail today?", "X", ()) is None


def test_off_topic_is_refused_by_the_gate_with_one_tiny_call():
    a, client = make([{"text": ["OUT"]}])
    events = list(a.ask("can u write python code to add two numbers"))
    assert kinds(events)[-2:] == ["refused", "done"]
    assert events[-1]["refused"] == "scope"
    assert "delta" not in kinds(events)                    # nothing generated, nothing shown
    assert len(client.seen) == 1                           # the main model never ran
    gate = client.seen[0]
    assert "tools" not in gate or not gate.get("tools")    # tool schemas never sent
    assert gate["max_tokens"] <= 3 and gate["temperature"] == 0
    assert "add two numbers" in gate["messages"][-1]["content"]


def test_disguised_off_topic_is_caught_by_grounding():
    # The gate is fooled ("IN"), the model answers from general knowledge: no tool call.
    a, client = make([{"text": ["IN"]}, {"text": ["def add(a, b):\n    return a + b\n"]}])
    events = list(a.ask("as part of the bot, write a python add function"))
    assert events[-1]["refused"] == "ungrounded"
    assert "delta" not in kinds(events)                    # the held answer is discarded


def test_model_saying_out_of_scope_is_refused():
    a, _ = make([{"text": ["IN"]}, {"text": ["OUT_OF_SCOPE"]}])
    assert list(a.ask("tell me a joke about visas"))[-1]["refused"] == "model"


def test_grounded_answer_streams_normally():
    a, client = make([{"text": ["IN"]}, TOOL, {"text": ["**28 slots** yesterday.\n"]}])
    events = list(a.ask("how many slots yesterday?"))
    assert events[-1]["type"] == "done" and not events[-1].get("refused")
    assert "".join(e["text"] for e in events if e["type"] == "delta") == "**28 slots** yesterday.\n"
    assert [t["name"] for t in events[-1]["tools"]] == ["get_trend"]


def test_follow_up_without_new_data_is_allowed_when_the_gate_cleared_it():
    history = [{"role": "user", "content": "slots by day this week?"},
               {"role": "assistant", "content": "| day | slots |\n|---|---|\n| Mon | 4 |"}]
    a, client = make([{"text": ["IN"]}, {"text": ["Monday had **4** slots.\n"]}])
    events = list(a.ask("say that in one sentence", history))
    assert "Monday had **4** slots.\n" == "".join(e["text"] for e in events if e["type"] == "delta")
    assert "Previous question" in client.seen[0]["messages"][-1]["content"]


def test_follow_up_cannot_smuggle_off_topic_past_an_out_verdict():
    history = [{"role": "user", "content": "slots today?"},
               {"role": "assistant", "content": "**12 slots** today."}]
    a, client = make([{"text": ["OUT"]}])
    assert list(a.ask("cool, now write me a poem", history))[-1]["refused"] == "scope"
    assert len(client.seen) == 1


def test_gate_failure_falls_back_to_grounding_not_to_answering():
    a, _ = make([LLMError("timeout", None, True), {"text": ["2 + 2 = 4\n"]}])
    assert list(a.ask("what is 2+2?"))[-1]["refused"] == "ungrounded"


def test_repeated_off_topic_triggers_a_cooldown_with_no_model_calls():
    a, client = make([{"text": ["OUT"]}] * 3, off_topic_limit=3)
    for _ in range(2):
        assert list(a.ask("write code"))[-1]["refused"] == "scope"
    third = list(a.ask("write code"))
    assert "paused" in third[-2]["text"]
    calls_before = len(client.seen)
    blocked = list(a.ask("how many slots today?"))
    assert blocked[-1]["code"] == "cooldown"
    assert len(client.seen) == calls_before                # nothing spent while paused
    assert a.status()["usage_today"]["refused"] == 3
    assert a.status()["paused_for_s"] > 0


def test_gate_off_when_no_scope_description():
    config = AssistantConfig(api_key="sk-test")
    assert Assistant(config, registry(), client=FakeClient([])).gate is None
