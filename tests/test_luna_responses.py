"""The Responses adapter, driven by fabricated events. No network.

Stream adapters do not fail on the happy path. They fail on a tool call with
no arguments, two calls in one response, and a stream that stops mid-item —
which are miserable to debug live against a paid API and trivial to pin here.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import luna_responses as lr


PASS = FAIL = 0


def check(label, ok):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


def ev(type_, **kw):
    """Events arrive as SDK objects; dicts exercise the same branches."""
    return dict(type=type_, **kw)


def drain(events):
    """Run the shim the way llm._stream_turn does, and report what it saw."""
    calls, text, reasoned, usage = {}, "", 0, {}
    for chunk in lr.stream_chunks(events):
        u = getattr(chunk, "usage", None)
        if u is not None:
            usage["prompt"] = u.prompt_tokens
            usage["completion"] = u.completion_tokens
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        for t in (getattr(delta, "tool_calls", None) or ()):
            slot = calls.setdefault(t.index, {"id": "", "name": "", "args": ""})
            if t.id:
                slot["id"] = t.id
            if t.function.name:
                slot["name"] = t.function.name
            if t.function.arguments:
                slot["args"] += t.function.arguments
        if getattr(delta, "reasoning", None):
            reasoned += len(delta.reasoning)
        if getattr(delta, "content", None):
            text += delta.content
    return calls, text, reasoned, usage


# ── request translation ──────────────────────────────────────────────────────
print("— tool schemas —")
flat = lr.to_responses_tools([{
    "type": "function",
    "function": {"name": "open_app", "description": "Open an app",
                 "parameters": {"type": "object", "properties": {}}},
}])
check("the function definition is unnested",
      flat == [{"type": "function", "name": "open_app",
                "description": "Open an app",
                "parameters": {"type": "object", "properties": {}}}])
check("an already-flat schema survives a second pass",
      lr.to_responses_tools(flat) == flat)

print("\n— messages become typed input items —")
lr.forget_reasoning()
items = lr.to_input_items([
    {"role": "system", "content": "You are Ted."},
    {"role": "user", "content": "open terminal"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function",
         "function": {"name": "open_app", "arguments": '{"name":"Terminal"}'}}]},
    {"role": "tool", "tool_call_id": "call_1", "name": "open_app",
     "content": "Opened Terminal."},
])
check("the assistant's tool call becomes a function_call item",
      items[2] == {"type": "function_call", "call_id": "call_1",
                   "name": "open_app", "arguments": '{"name":"Terminal"}'})
check("tool_call_id becomes call_id on the output item",
      items[3] == {"type": "function_call_output", "call_id": "call_1",
                   "output": "Opened Terminal."})
check("an empty assistant content line is not sent as a blank message",
      len(items) == 4)

print("\n— multimodal parts are converted, not dropped —")
mm = lr.to_input_items([{"role": "user", "content": [
    {"type": "text", "text": "what is this"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
]}])
check("text and image both cross over",
      mm[0]["content"] == [
          {"type": "input_text", "text": "what is this"},
          {"type": "input_image", "image_url": "data:image/png;base64,AAA"}])

print("\n— request body —")
body = lr.build_params(model="gpt-5.6-luna",
                       messages=[{"role": "user", "content": "hi"}],
                       tools=flat, reasoning_effort="medium", max_tokens=400)
check("reasoning effort is sent as the Responses shape",
      body["reasoning"] == {"effort": "medium"})
check("max_tokens becomes max_output_tokens",
      body["max_output_tokens"] == 400 and "max_tokens" not in body)
check("Ted keeps his own history — nothing is stored server-side",
      body["store"] is False)
check("encrypted reasoning is requested so carry-over can work",
      body["include"] == ["reasoning.encrypted_content"])
check("effort 'none' sends no reasoning block at all",
      "reasoning" not in lr.build_params(
          model="m", messages=[], reasoning_effort="none"))

# ── stream translation ───────────────────────────────────────────────────────
print("\n— one tool call, streamed —")
calls, text, _reasoned, usage = drain([
    ev("response.output_item.added",
       item={"type": "reasoning", "id": "rs_1"}),
    ev("response.reasoning_text.delta", delta="the user wants Terminal"),
    ev("response.output_item.added",
       item={"type": "function_call", "id": "fc_1", "call_id": "call_1",
             "name": "open_app", "arguments": ""}),
    ev("response.function_call_arguments.delta", item_id="fc_1",
       delta='{"name":'),
    ev("response.function_call_arguments.delta", item_id="fc_1",
       delta='"Terminal"}'),
    ev("response.completed", response={
        "output": [{"type": "reasoning", "id": "rs_1",
                    "encrypted_content": "ENC"},
                   {"type": "function_call", "call_id": "call_1",
                    "name": "open_app"}],
        "usage": {"input_tokens": 900, "output_tokens": 40}}),
])
check("the call arrives whole", calls == {0: {
    "id": "call_1", "name": "open_app", "args": '{"name":"Terminal"}'}})
check("a reasoning item never opens a tool-call slot", len(calls) == 1)
check("usage crosses over", usage == {"prompt": 900, "completion": 40})

print("\n— the awkward ones —")
calls, _t, _r, _u = drain([
    ev("response.output_item.added",
       item={"type": "function_call", "id": "fc_1", "call_id": "c1",
             "name": "now_playing", "arguments": ""}),
    ev("response.completed", response={"output": [], "usage": {}}),
])
check("a call with no arguments still names its tool",
      calls == {0: {"id": "c1", "name": "now_playing", "args": ""}})

calls, _t, _r, _u = drain([
    ev("response.output_item.added",
       item={"type": "function_call", "id": "fc_1", "call_id": "c1",
             "name": "close_app", "arguments": ""}),
    ev("response.output_item.added",
       item={"type": "function_call", "id": "fc_2", "call_id": "c2",
             "name": "close_app", "arguments": ""}),
    ev("response.function_call_arguments.delta", item_id="fc_2",
       delta='{"name":"Finder"}'),
    ev("response.function_call_arguments.delta", item_id="fc_1",
       delta='{"name":"Notes"}'),
    ev("response.completed", response={"output": [], "usage": {}}),
])
check("two calls in one response get dense, separate indexes",
      calls == {0: {"id": "c1", "name": "close_app", "args": '{"name":"Notes"}'},
                1: {"id": "c2", "name": "close_app", "args": '{"name":"Finder"}'}})
check("interleaved argument deltas land on the right call",
      calls[0]["args"] == '{"name":"Notes"}')

calls, _t, _r, _u = drain([
    ev("response.function_call_arguments.delta", item_id="orphan",
       delta='{"a":1}'),
    ev("response.completed", response={"output": [], "usage": {}}),
])
check("arguments for a call we never saw open still survive",
      calls == {0: {"id": "", "name": "", "args": '{"a":1}'}})

_c, _t, _r, _u = drain([
    ev("response.output_text.delta", delta="Opening "),
    ev("response.unknown_future_event", delta="???"),
    ev("response.output_text.delta", delta="Terminal."),
    ev("response.completed", response={"output": [], "usage": {}}),
])
check("an unrecognised event is ignored, not fatal", _t == "Opening Terminal.")

failed = False
try:
    drain([ev("response.failed",
              response={"error": {"message": "server had a bad day"}})])
except RuntimeError:
    failed = True
check("a failed stream raises instead of returning an empty answer", failed)

# ── reasoning carry-over: the whole point ────────────────────────────────────
print("\n— reasoning survives the tool round trip —")
lr.forget_reasoning()
drain([
    ev("response.output_item.added",
       item={"type": "function_call", "id": "fc_1", "call_id": "call_9",
             "name": "open_app", "arguments": ""}),
    ev("response.completed", response={
        "output": [{"type": "reasoning", "id": "rs_9",
                    "encrypted_content": "THINKING"},
                   {"type": "function_call", "call_id": "call_9",
                    "name": "open_app"}],
        "usage": {}}),
])
round_two = lr.to_input_items([
    {"role": "user", "content": "open terminal then run claude"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_9", "type": "function",
         "function": {"name": "open_app", "arguments": '{"name":"Terminal"}'}}]},
    {"role": "tool", "tool_call_id": "call_9", "content": "Opened Terminal."},
])
check("the previous round's thinking is put back in",
      round_two[1].get("type") == "reasoning"
      and round_two[1].get("encrypted_content") == "THINKING")
check("it goes back BEFORE the call it produced",
      round_two[2]["type"] == "function_call")

# Regression: the API returns bookkeeping on a reasoning item that it refuses
# to accept back. Echoing it verbatim 400s the SECOND round of every tool turn
# — Ted opened Terminal and stopped dead.
lr.forget_reasoning()
drain([
    ev("response.output_item.added",
       item={"type": "function_call", "id": "fc_x", "call_id": "call_x",
             "name": "open_app", "arguments": ""}),
    ev("response.completed", response={
        "output": [{"type": "reasoning", "id": "rs_x", "status": "completed",
                    "encrypted_content": "ENC", "summary": []},
                   {"type": "function_call", "call_id": "call_x",
                    "name": "open_app", "status": "completed"}],
        "usage": {}}),
])
echoed = lr.recall_reasoning(["call_x"])[0]
check("output-only bookkeeping is stripped before it is echoed",
      "status" not in echoed)
check("the parts the model actually needs survive",
      echoed["type"] == "reasoning" and echoed["id"] == "rs_x"
      and echoed["encrypted_content"] == "ENC")
check("an empty summary is not sent back as a null field",
      "summary" not in echoed or echoed["summary"] == [])

lr.forget_reasoning()
missed = lr.to_input_items([
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_9", "type": "function",
         "function": {"name": "open_app", "arguments": "{}"}}]},
])
check("a cache miss degrades quietly instead of raising",
      len(missed) == 1 and missed[0]["type"] == "function_call")

print("\n— the non-streamed path (legacy ladder tool probe) —")
lr.forget_reasoning()
completion = lr.to_completion({
    "id": "resp_1", "model": "gpt-5.6-luna",
    "output": [
        {"type": "reasoning", "id": "rs_1"},
        {"type": "message", "content": [
            {"type": "output_text", "text": "Sure."}]},
        {"type": "function_call", "call_id": "c1", "name": "open_app",
         "arguments": '{"name":"Notes"}'},
    ],
    "usage": {"input_tokens": 12, "output_tokens": 3},
})
tc = completion.choices[0].message.tool_calls
check("tool calls read the way app.py expects",
      len(tc) == 1 and tc[0].id == "c1"
      and tc[0].function.name == "open_app"
      and tc[0].function.arguments == '{"name":"Notes"}')
check("text and usage come along",
      completion.choices[0].message.content == "Sure."
      and completion.usage.prompt_tokens == 12)

print("\n— the cache stays bounded —")
lr.forget_reasoning()
for n in range(lr._CACHE_MAX + 20):
    lr.remember_reasoning([f"c{n}"], [{"type": "reasoning", "id": f"r{n}"}])
check("an assistant left running for days does not leak",
      len(lr._reasoning_by_call_id) == lr._CACHE_MAX)
check("the oldest entries are the ones dropped",
      not lr.recall_reasoning(["c0"]) and lr.recall_reasoning(["c80"]))

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
