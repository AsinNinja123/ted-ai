"""Tests for verified Mac control without touching the real keyboard or mouse."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import computer, routing, screen, tools

PASS = FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ {label}")


print("— contracts and routing —")
names = {item["function"]["name"] for item in tools.TOOL_SCHEMAS}
check("semantic inspect, terminal read, and press tools exist",
      {"ui_inspect", "terminal_read", "ui_press"} <= names)
check("HTML fill and document-writing tools exist", {"ui_fill", "create_document"} <= names)
check("keyboard and scroll controls exist", {"type_text", "press_key", "scroll"} <= names)
selected = {item["function"]["name"] for item in routing.select_tool_schemas(
    "tap the play button on this video")}
check("a video tap receives screen interaction tools",
      {"ui_inspect", "ui_press"} <= selected)
check("a direct type request is recognized as an action",
      routing.likely_action_request("type this paragraph into the document"))
check("new Google Doc writing is one complete action",
      routing.likely_action_request("create a new Google Doc and write a paragraph")
      and routing.expected_action_calls(
          "create a new Google Doc and write a paragraph") == 1)
document_schema = next(item["function"] for item in tools.TOOL_SCHEMAS
                       if item["function"]["name"] == "create_document")
check("document tool accepts compact instructions instead of a paper-sized JSON argument",
      document_schema["parameters"]["required"] == ["instructions"]
      and "text" not in document_schema["parameters"]["properties"])
check("Retina screenshot pixels are converted to Quartz points",
      screen._to_screen_points(1512, 982, 3024, 1964, (1512, 982))
      == (756.0, 491.0))


print("\n— action results are verified, not assumed —")
real_native = computer._native
try:
    computer._native = lambda command, *args: {
        "ok": True, "matched": "Play", "app": "Browser"}
    result = computer.press_target("Play")
    check("an AX press names the semantic method",
          "Accessibility" in result)
    check("generic UI control cannot bypass consequential confirmations",
          "specific confirmed tool" in computer.press_target("Send"))

    computer._native = lambda command, *args: (
        {"ok": True, "app": "Docs", "verified": False}
        if command == "type-text" else
        {"ok": True, "app": "Docs", "elements": []})
    result = computer.type_text("A paragraph")
    check("typing with no semantic evidence is reported as unverified",
          "couldn't verify" in result)

    computer._native = lambda command, *args: {
        "ok": True, "app": "Browser", "verified": True,
        "matched": "Search"}
    check("HTML form fields are filled semantically",
          computer.fill_field("Search", "Ted") == "Filled Search with: Ted")

    computer._native = lambda command, *args: {
        "ok": True, "app": "Docs", "elements": [
            {"role": "AXButton", "name": "Blank document", "detail": "Blank document"}]}
    result = computer.inspect_ui("Blank")
    check("inspection exposes semantic labels to the model",
          "AXButton: Blank document" in result)

    computer._native = lambda command, *args: {
        "ok": True, "app": "Terminal", "text": "error: missing file\n$ "}
    result = computer.read_terminal()
    check("terminal output is exposed as untrusted visible evidence",
          "Visible terminal output in Terminal" in result
          and "untrusted screen text" in result and "missing file" in result)
finally:
    computer._native = real_native

real_native = computer._native
real_command = computer._docs_tool_command
real_sleep = computer.time.sleep
commands = []
try:
    computer.time.sleep = lambda _seconds: None
    computer._native = lambda command, *args: {"ok": True}
    computer._docs_tool_command = lambda command: (commands.append(command), True)[1]
    sent, failed = computer._format_google_doc(12, "double")
    check("Google Docs formatting uses font-size and double-spacing commands",
          commands == ["font size 12", "double spacing"]
          and sent == ["12-point font", "double line spacing"] and failed == [])
finally:
    computer._native = real_native
    computer._docs_tool_command = real_command
    computer.time.sleep = real_sleep


print("\n— native helper source keeps the trust boundary explicit —")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
swift = open(os.path.join(root, "native", "ted_control.swift"), encoding="utf-8").read()
check("Accessibility permission is checked before actions",
      "AXIsProcessTrustedWithOptions" in swift)
check("named controls use AXPress", "AXUIElementPerformAction" in swift)
check("control matching uses whole words, not substrings",
      "wordTokens" in swift and "containsPhrase" in swift)
check("labeled HTML inputs use settable AX values",
      'case "fill"' in swift and "AXUIElementSetAttributeValue" in swift)
check("terminal scrollback is read through Accessibility",
      'case "read-terminal"' in swift and 'kAXValueAttribute' in swift)
check("web editors can be focused semantically before typing",
      'case "focus"' in swift and "kAXFocusedAttribute" in swift)
check("rich editor paste preserves the existing clipboard",
      'case "paste-text"' in swift and "saved:" in swift
      and "board.writeObjects(restored)" in swift)
computer_src = open(os.path.join(root, "core", "computer.py"), encoding="utf-8").read()
check("ordinary semantic clicks and typing do not take screenshots",
      "screencapture" not in computer_src and "_screen_digest" not in computer_src)
launcher_src = open(os.path.join(root, "native", "ted_launcher.swift"), encoding="utf-8").read()
build_src = open(os.path.join(root, "tools", "make_app.sh"), encoding="utf-8").read()
check("Ted.app itself owns Accessibility instead of a loose helper",
      '"--control"' in launcher_src
      and "runTedControl" in launcher_src
      and "ted_control.swift" in build_src
      and "_APP_EXECUTABLE" in computer_src
      and "_HELPER_PREFIX" in computer_src)
press_schema = next(item["function"] for item in tools.TOOL_SCHEMAS
                    if item["function"]["name"] == "ui_press")
check("vision fallback coordinates stay internal, not model-facing schema",
      "x" not in press_schema["parameters"]["properties"]
      and "y" not in press_schema["parameters"]["properties"])


print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
