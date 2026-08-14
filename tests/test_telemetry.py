"""tests/test_telemetry.py — the diagnostics panel, the brain switch, the icon.

Charlie asked to be able to judge Ted without a terminal. That means three
things have to be true and stay true:

  * a turn is recorded even when it fails, is interrupted, or never reaches a
    model — a log that only contains successes is worse than no log;
  * token counts say whether they are real or estimated, because a guess that
    looks like a measurement next to an 8,000-per-minute ceiling is how you
    conclude you have headroom and keep getting rate limited;
  * a pinned brain does not silently fall back, or the panel reports one
    model's behaviour under the other one's name.

The icon checks are here rather than in a build script because the original
failure survived precisely because it could only be reproduced on the one Mac
it was broken on.
"""

import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Point the turn log at a scratch database BEFORE core.telemetry is imported.
os.environ["TED_DB"] = os.path.join(tempfile.mkdtemp(), "telemetry_test.db")

from core import telemetry                                    # noqa: E402

_fails = [0]
_checks = [0]


def check(label, ok):
    _checks[0] += 1
    print(("  ✓ " if ok else "  ✗ ") + label)
    if not ok:
        _fails[0] += 1


print("— a turn is recorded, including the bad ones —")

telemetry.clear()
t = telemetry.Turn("play a different one", source="chat")
t.provider, t.model = "groq", "qwen/qwen3.6-27b"
t.prompt_tokens, t.completion_tokens = 1420, 96
t.tokens_estimated = False
t.context_scope = "relevant"
t.note_tool("play_music")
t.finish(reply="Playing A Whole New World.")

rows = telemetry.recent()
check("the turn is in the log", len(rows) == 1)
r = rows[0]
check("…with a timestamp", bool(r["ts"]) and "T" in r["ts"])
check("…the brain that answered", r["provider"] == "groq")
check("…real token counts, totalled", r["total_tokens"] == 1516)
check("…marked as measured, not estimated", r["tokens_estimated"] == 0)
check("…the tool it called", r["tools_called"] == "play_music")
check("…and is marked ok", r["ok"] == 1)

fail = telemetry.Turn("open notes", source="chat")
fail.provider = "none"
fail.finish(reply="", error="Both brains failed; Groq: 429; Ollama: refused")
rows = telemetry.recent()
check("a failed turn is recorded too", len(rows) == 2)
check("…with the real error text, not a summary",
      "Ollama: refused" in rows[0]["error"])
check("…and is marked not-ok", rows[0]["ok"] == 0)

twice = telemetry.Turn("hello")
twice.finish(reply="Hi.")
twice.finish(reply="Hi again.")
check("finish() is idempotent — one turn is never two rows",
      len(telemetry.recent()) == 3)


print("\n— the numbers the header shows —")

s = telemetry.stats()
check("turns are counted", s["turns"] == 3)
check("errors are counted", s["errors"] == 1)
check("tokens-per-minute reflects what was just spent", s["tpm"] >= 1516)
check("the ceiling is reported so the gauge has a denominator",
      s["tpm_limit"] == telemetry.DEFAULT_TPM_LIMIT)

est = telemetry.Turn("estimate me")
est.completion_tokens = 40
est.tokens_estimated = True
est.finish(reply="x" * 160)
check("an estimated count is flagged as estimated",
      telemetry.recent()[0]["tokens_estimated"] == 1)

report = telemetry.as_report(5)
check("the report names the ceiling", "of 8000" in report)
check("the report includes the failure", "Ollama: refused" in report)

check("clearing empties the log", telemetry.clear() == 4 and not telemetry.recent())


print("\n— a pinned brain is not a suggestion —")

from core import providers                                    # noqa: E402

check("the default is auto", providers.get_provider_mode() == "auto")
try:
    providers.set_provider_mode("banana")
    check("a bad mode is refused", False)
except ValueError:
    check("a bad mode is refused", True)

providers.set_provider_mode("local")
check("the mode persists to disk and reads back",
      providers.get_provider_mode() == "local")

# Pinned to cloud with the cloud broken must RAISE, not answer locally. The
# whole point of pinning is to observe that brain; a silent failover would
# report Ollama's behaviour under the label "cloud".
calls = {"ollama": 0}
_real_ollama = providers._ollama_create
providers._ollama_create = lambda **kw: calls.__setitem__("ollama", calls["ollama"] + 1)
_real_groq = providers._groq


class _Boom:
    class chat:
        class completions:
            @staticmethod
            def create(**kw):
                raise RuntimeError("429 rate limit")


providers._groq = _Boom
providers.set_provider_mode("cloud")
try:
    providers.chat_create(messages=[], stream=True)
    check("cloud-pinned surfaces the cloud failure", False)
except Exception as e:
    check("cloud-pinned surfaces the cloud failure", "429" in str(e))
check("…and never quietly answers from the local brain", calls["ollama"] == 0)

providers.set_provider_mode("local")
providers.chat_create(messages=[], stream=True)
check("local-pinned never touches the cloud", calls["ollama"] == 1)

providers._ollama_create = _real_ollama
providers._groq = _real_groq
providers.set_provider_mode("auto")
check("mode restored to auto for the next run",
      providers.get_provider_mode() == "auto")


print("\n— the app icon builds, and is a real .icns —")

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import make_icon                                              # noqa: E402

data = make_icon.build_icns()
check("starts with the icns magic", data[:4] == b"icns")
declared = struct.unpack(">I", data[4:8])[0]
check("the declared length matches the file — Finder rejects it otherwise",
      declared == len(data))

off, seen = 8, []
parsed_clean = True
while off < len(data):
    ostype, n = struct.unpack(">4sI", data[off:off + 8])
    payload = data[off + 8:off + n]
    if payload[:8] != b"\x89PNG\r\n\x1a\n" or n <= 8:
        parsed_clean = False
        break
    w, h = struct.unpack(">II", payload[16:24])
    seen.append((ostype, w, h))
    off += n
check("every entry carries a valid PNG", parsed_clean)
check("it parses cleanly to the end", off == len(data))
check("all eight sizes are present", len(seen) == len(make_icon.ICNS_TYPES))
check("each entry's PNG is the size its OSType promises",
      all(w == h == size for (ostype, w, h), (_t, size)
          in zip(seen, make_icon.ICNS_TYPES)))
check("the 1024 entry exists — Retina Finder and Get Info want it",
      any(w == 1024 for _t, w, _h in seen))

small = make_icon.downsample(make_icon.render_rgba(64), 64, 16)
check("downsampling produces the right number of bytes", len(small) == 16 * 16 * 4)
check("…and the corners stay transparent (no dark halo)",
      small[3] == 0 and small[(15 * 16 + 15) * 4 + 3] == 0)
check("…while the centre is opaque", small[(8 * 16 + 8) * 4 + 3] > 200)


print("\n— the HUD can actually reach the diagnostics panel —")

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
hud = open(os.path.join(_root, "ui", "ted_hud.html"), encoding="utf-8").read()
check("there is a Diagnostics button in the sidebar, next to Memory",
      'id="diagbtn"' in hud and 'id="membtn"' in hud)
check("…wired to a toggle", "tedHud.toggleDiagnostics()" in hud)
check("…that loads the dashboard page, not the memory one",
      "127.0.0.1:5175/diagnostics" in hud)
check("opening one overlay closes the other — both are full-screen",
      "tedHud.hideMemory(); tedHud.showDiagnostics()" in hud
      and "tedHud.hideDiagnostics(); tedHud.showMemory()" in hud)
check("closing it stops the 2s polling loop",
      "about:blank" in hud)

build = open(os.path.join(_root, "tools", "make_app.sh"), encoding="utf-8").read()
check("the bundle gets a PkgInfo — without it Finder may not see an app",
      "APPL????" in build)
check("the bundle keeps its inode so a Dock tile survives a rebuild",
      'rm -rf "$APP/Contents"' in build and 'rm -rf "$APP"\n' not in build)
check("LaunchServices is unregistered before being re-registered",
      '"$LSREG" -u' in build and '"$LSREG" -f' in build)

app = open(os.path.join(_root, "dashboard", "app.py"), encoding="utf-8").read()
check("the dashboard serves /diagnostics", '"/diagnostics"' in app)
check("…and advertises the capability so the HUD can detect an old server",
      '"diagnostics": True' in app)


print("\n" + "=" * 50)
print(f"{_checks[0] - _fails[0]} passed, {_fails[0]} failed")
