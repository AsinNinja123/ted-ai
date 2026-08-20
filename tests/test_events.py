"""Characterization checks for the shared event stream and SSE endpoint."""

import json
import os
import queue
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import events
from dashboard.app import app


PASS = FAIL = 0


def check(label, ok):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


print("— bounded in-process fan-out —")
bus = events.EventBus(history_size=3)
fresh = bus.subscribe()
first = bus.emit("plan", {"heard": "clean up"})
check("a live subscriber receives the emitted fact", fresh.get(timeout=.1) == first)
fresh.close()
check("closing a subscriber releases it", bus.subscriber_count == 0)

for n in range(4):
    bus.emit("agent_result", {"n": n})
replay = bus.subscribe(after_id=first.id)
got = [replay.get(timeout=.1).payload["n"] for _ in range(3)]
check("reconnect replays only the bounded retained history", got == [1, 2, 3])
try:
    replay.get(timeout=.01)
    empty = False
except queue.Empty:
    empty = True
check("replay does not invent an extra event", empty)
replay.close()

print("\n— wire format —")
frame = events.sse_encode(first)
check("SSE carries an id for reconnect", frame.startswith(f"id: {first.id}\n"))
check("SSE names the event kind", "event: plan\n" in frame)
data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
decoded = json.loads(data_line[6:])
check("SSE data is the same event, not a narrated copy",
      decoded["payload"] == {"heard": "clean up"} and decoded["id"] == first.id)

print("\n— Flask endpoint —")
published = events.emit("test_trace", {"proof": "real"})
client = app.test_client()
response = client.get(
    "/api/events",
    headers={"Last-Event-ID": str(published.id - 1)},
    buffered=False,
)
iterator = iter(response.response)
retry = next(iterator).decode()
wire = next(iterator).decode()
check("the endpoint is an event stream", response.mimetype == "text/event-stream")
check("the browser gets a reconnect interval", retry == "retry: 1500\n\n")
check("the requested missed event is replayed", "event: test_trace" in wire
      and '"proof":"real"' in wire)
check("streaming is explicitly not cached", response.headers.get("Cache-Control") == "no-cache")
response.close()
malformed = client.get(
    "/api/events", headers={"Last-Event-ID": "not-a-number"}, buffered=False)
check("a malformed reconnect id starts a fresh stream instead of a 500",
      malformed.status_code == 200 and next(iter(malformed.response)).decode() ==
      "retry: 1500\n\n")
malformed.close()
blocked = client.get(
    "/api/events", headers={"Origin": "https://attacker.example"}, buffered=False)
check("an arbitrary website cannot hold open Ted's private event stream",
      blocked.status_code == 403)
blocked.close()

print("\n— confirmation endpoint and HUD consumer —")
# The Future itself is exercised in test_agents. Here the route only needs to
# prove it rejects malformed/stale ids rather than approving a different task.
bad = client.post("/api/confirm/not-real", json={"approved": "yes"})
stale = client.post("/api/confirm/not-real", json={"approved": True})
foreign = client.post(
    "/api/confirm/not-real", json={"approved": True},
    headers={"Origin": "https://attacker.example"})
check("confirmation requires an actual boolean", bad.status_code == 400)
check("a stale request id cannot approve anything", stale.status_code == 404)
check("an arbitrary website cannot resolve a local confirmation",
      foreign.status_code == 403)

hud = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "ui", "ted_hud.html"), encoding="utf-8").read()
check("the HUD subscribes instead of polling for trace events",
      "new EventSource(API+'/events')" in hud)
check("the thought bubble is rendered from structured event kinds",
      "renderTraceEvent(kind,envelope)" in hud and "agent_result" in hud)
check("the HUD can approve the exact Future by request id",
      "'/confirm/'+encodeURIComponent(requestId)" in hud)

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
