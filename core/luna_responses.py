"""Speak the Responses API while the rest of Ted speaks Chat Completions.

The Ted Code Book — Chapter 37. Start at §37.3 if a tool call arrived with
empty arguments, §37.5 if Ted forgot mid-task what he was doing.

WHY THIS FILE EXISTS
────────────────────
GPT-5.6 Luna refuses function tools and reasoning together on
``/v1/chat/completions``. The 400 says so outright:

    Function tools with reasoning_effort are not supported for gpt-5.6-luna
    in /v1/chat/completions. To use function tools, use /v1/responses or
    set reasoning_effort to 'none'.

That is not the model being incapable. It is the endpoint having nowhere to
put the model's thinking. A reasoning model emits two things per round: the
visible output (text, or a tool call) and an internal reasoning trace. On a
multi-step task the trace from round 1 has to come back in round 2, or the
model re-derives its plan from scratch at every step.

``/v1/chat/completions`` takes a flat ``messages`` array with four fixed roles.
There is no slot for a reasoning item, so the trace would be silently dropped
between rounds — paid for, then thrown away. OpenAI errors rather than
charging for reasoning it knows it cannot keep.

``/v1/responses`` takes a list of TYPED items — ``message``, ``function_call``,
``function_call_output``, ``reasoning`` — so the trace round-trips like
anything else.

Ted took the other exit: ``core/providers.py`` forced ``reasoning_effort`` to
"none" whenever tools were present. That made the request legal. It did not
make Ted think. It is why one sentence — "open a terminal, run claude, tell it
to write a calculator" — arrived as four unrelated single-step plans in
``data/ted_launch.log``, each one a cold guess at the next move.

WHAT THIS FILE DOES
───────────────────
Everything above ``providers.chat_create`` is duck-typed against the Chat
Completions stream shape. ``llm._stream_turn`` needs exactly this and nothing
more::

    chunk.usage.prompt_tokens / .completion_tokens
    chunk.choices[0].delta.content
    chunk.choices[0].delta.reasoning
    chunk.choices[0].delta.tool_calls[i].index / .id
    chunk.choices[0].delta.tool_calls[i].function.name / .arguments
    resp.close()

So this module calls ``/v1/responses`` and yields objects of that shape. No
caller changes. If you find yourself editing ``_stream_turn`` to accommodate
this file, the shim is wrong — fix the shim.

THE REASONING CACHE (§37.5)
───────────────────────────
Ted's tool loop rebuilds the whole ``messages`` list every round and hands it
back to a stateless ``chat_create``. The reasoning items are not in that list,
so by default they would be lost anyway and this file would buy nothing.

``previous_response_id`` is the easy fix, but it makes the provider carry
per-turn state and it breaks the moment ``chat_create`` falls back to Groq or
Ollama mid-turn — Ted's normal, expected behaviour on a rate limit.

So instead: when a round finishes, the reasoning items are stashed under the
``call_id`` of every function call in that same round. Next round, the
incoming ``messages`` carry those ids in their ``tool_call_id`` fields, the
reasoning is looked up and re-inserted ahead of the matching ``function_call``
items, and the model resumes its own train of thought. Stateless from Ted's
side, and a mid-turn handover to another provider simply misses the cache
instead of corrupting anything.

The items are opaque. Ted never reads them, never renders them, and never
stores them past the turn. They are a token passed along, not data we author —
which is also why ``store=False``: the trace lives in this process's memory
for the length of one request chain and nowhere else.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict

# ── the reasoning cache ──────────────────────────────────────────────────────
# Bounded on purpose. A leak here is a slow memory leak in a process Charlie
# leaves running for days. 64 entries is several concurrent turns' worth; the
# oldest fall off and the only cost of a miss is one round without carry-over.
_CACHE_MAX = 64
_cache_lock = threading.Lock()
_reasoning_by_call_id: "OrderedDict[str, list]" = OrderedDict()


def remember_reasoning(call_ids, items):
    """Stash this round's reasoning under every call id it produced."""
    if not call_ids or not items:
        return
    with _cache_lock:
        for cid in call_ids:
            if not cid:
                continue
            _reasoning_by_call_id[cid] = items
            _reasoning_by_call_id.move_to_end(cid)
        while len(_reasoning_by_call_id) > _CACHE_MAX:
            _reasoning_by_call_id.popitem(last=False)


def recall_reasoning(call_ids):
    """Reasoning items for the first id still held, or [] on a miss.

    A miss is not an error. It means the previous round went to Groq or
    Ollama, or the cache rolled over. The turn continues without carry-over.
    """
    with _cache_lock:
        for cid in call_ids or ():
            items = _reasoning_by_call_id.get(cid)
            if items:
                return list(items)
    return []


def forget_reasoning():
    """Drop everything. For tests, and for a hard provider reset."""
    with _cache_lock:
        _reasoning_by_call_id.clear()


# A reasoning item is not symmetrical: the API returns bookkeeping fields it
# will not accept back. Echoing model_dump() verbatim 400s on input[n].status
# and kills every round after the first — Ted opens Terminal and stops.
#
# Allowlist, not a blocklist. A new output-only field appearing later would
# reintroduce exactly this bug, and the failure mode is the whole tool loop.
_REASONING_INPUT_FIELDS = ("type", "id", "encrypted_content", "summary",
                           "content")


def _sanitize_reasoning(item):
    """Keep only the fields /v1/responses accepts back as input."""
    clean = {k: v for k, v in item.items()
             if k in _REASONING_INPUT_FIELDS and v is not None}
    clean["type"] = "reasoning"
    return clean


def _as_dict(obj):
    """SDK model -> plain dict, whatever SDK version is installed."""
    if isinstance(obj, dict):
        return obj
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                continue
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")}


# ── request translation ──────────────────────────────────────────────────────

def to_responses_tools(schemas):
    """Chat Completions tool schemas -> Responses tool schemas.

    Chat Completions nests the definition under "function". Responses puts
    the same fields at the top level. That is the entire difference.
    """
    out = []
    for sc in schemas or ():
        if not isinstance(sc, dict):
            continue
        fn = sc.get("function")
        if not isinstance(fn, dict):
            # Already flat, or something we do not recognise. Pass it through
            # rather than dropping a capability on the floor.
            if sc.get("name"):
                out.append(dict(sc))
            continue
        flat = {"type": "function", "name": fn.get("name") or ""}
        if fn.get("description"):
            flat["description"] = fn["description"]
        if fn.get("parameters") is not None:
            flat["parameters"] = fn["parameters"]
        out.append(flat)
    return out


def _convert_content_parts(parts):
    """Multimodal content parts differ between the two APIs.

    Chat Completions: {"type": "text"} / {"type": "image_url", "image_url":
    {"url": ...}}.  Responses: {"type": "input_text"} / {"type":
    "input_image", "image_url": "<url>"}.  Ted's vision turns can carry tools,
    so this path is reachable and must not silently drop the image.
    """
    converted = []
    for part in parts:
        if not isinstance(part, dict):
            converted.append({"type": "input_text", "text": str(part)})
            continue
        kind = part.get("type")
        if kind in ("text", "input_text"):
            converted.append({"type": "input_text",
                              "text": part.get("text") or ""})
        elif kind in ("image_url", "input_image"):
            url = part.get("image_url")
            if isinstance(url, dict):
                url = url.get("url") or ""
            converted.append({"type": "input_image", "image_url": url or ""})
        else:
            converted.append(part)
    return converted


def to_input_items(messages):
    """Chat Completions messages -> Responses input items.

    Three shapes change:

      role "tool"                  -> {"type": "function_call_output"}
      assistant with tool_calls    -> one {"type": "function_call"} each,
                                      preceded by the cached reasoning items
      everything else              -> passes through as a role/content message

    ``tool_call_id`` becomes ``call_id``. That rename is what lets the cache
    above find the previous round's thinking.
    """
    items = []
    for m in messages or ():
        if not isinstance(m, dict):
            continue
        role = m.get("role")

        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": m.get("tool_call_id") or m.get("call_id") or "",
                "output": str(m.get("content") or ""),
            })
            continue

        tool_calls = m.get("tool_calls")
        if role == "assistant" and tool_calls:
            ids = [t.get("id") for t in tool_calls if isinstance(t, dict)]
            # The thinking goes back in FIRST — it is what produced the calls
            # below it, and order is how the model reads that relationship.
            for item in recall_reasoning(ids):
                items.append(item)
            text = str(m.get("content") or "").strip()
            if text:
                items.append({"role": "assistant", "content": text})
            for t in tool_calls:
                if not isinstance(t, dict):
                    continue
                fn = t.get("function") or {}
                items.append({
                    "type": "function_call",
                    "call_id": t.get("id") or "",
                    "name": fn.get("name") or "",
                    "arguments": fn.get("arguments") or "{}",
                })
            continue

        content = m.get("content")
        if isinstance(content, list):
            items.append({"role": role or "user",
                          "content": _convert_content_parts(content)})
        else:
            items.append({"role": role or "user",
                          "content": str(content or "")})
    return items


def build_params(*, model, messages, tools=None, tool_choice=None,
                 reasoning_effort=None, max_tokens=None, include_encrypted=True):
    """Assemble the /v1/responses request body."""
    params = {"model": model, "input": to_input_items(messages)}
    flat_tools = to_responses_tools(tools)
    if flat_tools:
        params["tools"] = flat_tools
        if tool_choice in ("auto", "required", "none"):
            params["tool_choice"] = tool_choice
    if reasoning_effort and reasoning_effort not in ("none", "default"):
        params["reasoning"] = {"effort": reasoning_effort}
    elif reasoning_effort == "default":
        params["reasoning"] = {"effort": "medium"}
    if max_tokens:
        params["max_output_tokens"] = max_tokens
    # Ted keeps his own history. Asking OpenAI to retain a copy buys nothing
    # and quietly puts Charlie's turns somewhere he did not choose.
    params["store"] = False
    if include_encrypted and params.get("reasoning"):
        # With store=False the trace only comes back if we ask for it. This is
        # the field the cache above stashes and echoes.
        params["include"] = ["reasoning.encrypted_content"]
    return params


# ── the chunk shim ───────────────────────────────────────────────────────────
# Deliberately dumb objects. They exist only so `_stream_turn`'s getattr calls
# find what they expect. Nothing else in Ted should ever import these.

class _Function:
    __slots__ = ("name", "arguments")

    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolCallDelta:
    __slots__ = ("index", "id", "type", "function")

    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.type = "function"
        self.function = _Function(name, arguments)


class _Delta:
    __slots__ = ("role", "content", "tool_calls", "reasoning",
                 "reasoning_content")

    def __init__(self, content=None, tool_calls=None, reasoning=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning = reasoning
        self.reasoning_content = None


class _Choice:
    __slots__ = ("index", "delta", "message", "finish_reason")

    def __init__(self, delta=None, message=None, finish_reason=None):
        self.index = 0
        self.delta = delta
        self.message = message
        self.finish_reason = finish_reason


class _Usage:
    __slots__ = ("prompt_tokens", "completion_tokens", "total_tokens")

    def __init__(self, prompt=0, completion=0):
        self.prompt_tokens = prompt or 0
        self.completion_tokens = completion or 0
        self.total_tokens = (prompt or 0) + (completion or 0)


class _Chunk:
    __slots__ = ("choices", "usage", "id", "model")

    def __init__(self, choices=None, usage=None, id="", model=""):
        self.choices = choices or []
        self.usage = usage
        self.id = id
        self.model = model


class _Message:
    """Non-streamed shape: resp.choices[0].message.content / .tool_calls."""
    __slots__ = ("role", "content", "tool_calls")

    def __init__(self, content=None, tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls or []


class _ToolCall:
    __slots__ = ("id", "type", "function")

    def __init__(self, id, name, arguments):
        self.id = id
        self.type = "function"
        self.function = _Function(name, arguments)


class _Completion:
    __slots__ = ("choices", "usage", "id", "model")

    def __init__(self, choices=None, usage=None, id="", model=""):
        self.choices = choices or []
        self.usage = usage
        self.id = id
        self.model = model


# ── response translation ─────────────────────────────────────────────────────

def _harvest(output_items):
    """Pull reasoning items and call ids out of a finished response.

    Returns (reasoning_items, call_ids). Both feed remember_reasoning().
    """
    reasoning, call_ids = [], []
    for raw in output_items or ():
        item = _as_dict(raw)
        kind = item.get("type")
        if kind == "reasoning":
            reasoning.append(_sanitize_reasoning(item))
        elif kind == "function_call":
            cid = item.get("call_id") or item.get("id")
            if cid:
                call_ids.append(cid)
    return reasoning, call_ids


def _usage_from(response_obj):
    u = _as_dict(response_obj).get("usage") or {}
    return _Usage(u.get("input_tokens", 0), u.get("output_tokens", 0))


def stream_chunks(events):
    """Responses stream events -> Chat Completions chunks.

    Split out from create() so the tests can drive it with fabricated events
    and no network. Every branch here is keyed off the event's own ``type``
    string; an event shape we do not recognise is ignored rather than raised,
    because a new event type appearing in the API must not take Ted down.
    """
    # Responses numbers its own output items, including reasoning items, so
    # output_index is NOT a usable tool-call index. Ted needs a dense 0..n
    # sequence per tool call, which is what `calls.setdefault(idx, ...)` in
    # _stream_turn accumulates against.
    slot_for_item = {}
    next_slot = 0

    for ev in events:
        etype = getattr(ev, "type", None) or (
            ev.get("type") if isinstance(ev, dict) else "") or ""

        if etype == "response.output_item.added":
            item = _as_dict(getattr(ev, "item", None)
                            or (ev.get("item") if isinstance(ev, dict) else {}))
            if item.get("type") != "function_call":
                continue
            item_id = item.get("id") or item.get("call_id") or ""
            if item_id in slot_for_item:
                continue
            slot_for_item[item_id] = next_slot
            # Emit the name and id immediately. Arguments stream in after, and
            # a tool call whose name arrives late reads as a nameless call to
            # everything downstream.
            yield _Chunk(choices=[_Choice(delta=_Delta(tool_calls=[
                _ToolCallDelta(next_slot,
                               id=item.get("call_id") or item_id,
                               name=item.get("name") or "",
                               arguments=item.get("arguments") or None)]))])
            next_slot += 1
            continue

        if etype == "response.function_call_arguments.delta":
            item_id = getattr(ev, "item_id", None) or (
                ev.get("item_id") if isinstance(ev, dict) else "")
            delta = getattr(ev, "delta", None) or (
                ev.get("delta") if isinstance(ev, dict) else "")
            if not delta:
                continue
            slot = slot_for_item.get(item_id)
            if slot is None:
                # Arguments for a call whose "added" event we never saw. Open
                # a slot rather than dropping the call entirely.
                slot = slot_for_item[item_id] = next_slot
                next_slot += 1
            yield _Chunk(choices=[_Choice(delta=_Delta(tool_calls=[
                _ToolCallDelta(slot, arguments=delta)]))])
            continue

        if etype == "response.output_text.delta":
            delta = getattr(ev, "delta", None) or (
                ev.get("delta") if isinstance(ev, dict) else "")
            if delta:
                yield _Chunk(choices=[_Choice(delta=_Delta(content=delta))])
            continue

        if etype in ("response.reasoning_summary_text.delta",
                     "response.reasoning_text.delta"):
            delta = getattr(ev, "delta", None) or (
                ev.get("delta") if isinstance(ev, dict) else "")
            if delta:
                # _stream_turn counts these to tell "spent its budget
                # thinking" apart from "the connection died". Never shown.
                yield _Chunk(choices=[_Choice(delta=_Delta(reasoning=delta))])
            continue

        if etype == "response.completed":
            resp = getattr(ev, "response", None) or (
                ev.get("response") if isinstance(ev, dict) else {})
            body = _as_dict(resp)
            reasoning, call_ids = _harvest(body.get("output"))
            remember_reasoning(call_ids, reasoning)
            # Empty choices on the usage chunk, exactly like Chat Completions.
            yield _Chunk(choices=[], usage=_usage_from(resp))
            continue

        if etype in ("response.failed", "response.incomplete", "error"):
            body = _as_dict(getattr(ev, "response", None) or ev)
            err = body.get("error") or body.get("incomplete_details") or body
            raise RuntimeError(f"Responses stream ended early: {err}")


def to_completion(response_obj):
    """A finished non-streamed response -> a ChatCompletion-shaped object.

    ``app.py``'s legacy tool probe calls chat_create with stream=False and
    reads .choices[0].message.tool_calls. That path is behind
    TED_LEGACY_LADDER, but a shim that only half-covers its callers is a
    landmine, so it is covered.
    """
    body = _as_dict(response_obj)
    text_parts, tool_calls = [], []
    for raw in body.get("output") or ():
        item = _as_dict(raw)
        kind = item.get("type")
        if kind == "function_call":
            tool_calls.append(_ToolCall(item.get("call_id") or item.get("id") or "",
                                        item.get("name") or "",
                                        item.get("arguments") or "{}"))
        elif kind == "message":
            for part in item.get("content") or ():
                part = _as_dict(part)
                if part.get("type") in ("output_text", "text"):
                    text_parts.append(part.get("text") or "")
    reasoning, call_ids = _harvest(body.get("output"))
    remember_reasoning(call_ids, reasoning)
    message = _Message(content="".join(text_parts) or None,
                       tool_calls=tool_calls)
    return _Completion(
        choices=[_Choice(message=message,
                         finish_reason="tool_calls" if tool_calls else "stop")],
        usage=_usage_from(response_obj),
        id=body.get("id") or "",
        model=body.get("model") or "")


# ── the entry point ──────────────────────────────────────────────────────────

def _is_include_rejection(exc):
    """Did the API refuse specifically the encrypted-content include?

    Worth distinguishing: losing carry-over is a degradation, losing the whole
    call is an outage. If only `include` is unsupported we retry without it
    and Ted still reasons — just one round at a time.
    """
    text = str(exc).lower()
    return "include" in text and ("unsupported" in text or "invalid" in text
                                 or "not supported" in text)


def create(client, *, model, messages, tools=None, tool_choice=None,
           reasoning_effort=None, max_tokens=None, stream=True, timeout=None):
    """Call /v1/responses and return something Ted's loop already understands.

    stream=True  -> a generator of Chat-Completions-shaped chunks (generators
                    have .close(), which is what _stream_turn calls).
    stream=False -> a ChatCompletion-shaped object.
    """
    params = build_params(model=model, messages=messages, tools=tools,
                          tool_choice=tool_choice,
                          reasoning_effort=reasoning_effort,
                          max_tokens=max_tokens)
    kwargs = {"timeout": timeout} if timeout else {}

    def _call(p, streaming):
        try:
            return client.responses.create(stream=streaming, **p, **kwargs)
        except Exception as exc:
            if "include" in p and _is_include_rejection(exc):
                print("[luna] encrypted reasoning unsupported — "
                      "continuing without carry-over")
                p = {k: v for k, v in p.items() if k != "include"}
                return client.responses.create(stream=streaming, **p, **kwargs)
            raise

    if not stream:
        return to_completion(_call(params, False))

    events = _call(params, True)

    def _generate():
        try:
            for chunk in stream_chunks(events):
                yield chunk
        finally:
            closer = getattr(events, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass

    return _generate()
