"""Verified macOS Accessibility and keyboard control for Ted."""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 24 (§24.2)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Typing text, pressing keys, and reading or writing the clipboard on your
#      behalf. This is macOS Accessibility control — the same permission a screen
#      reader needs — so it will be silently useless until Ted's launcher has been
#      granted it in System Settings.
#
#  A NOTE ON RISK
#      type_text and clipboard_write currently run WITHOUT confirmation. That is low
#      risk today because Ted has no browser automation. It stops being low risk the
#      moment that lands. See §35.
#
# =============================================================================

import json
import os
import re
import subprocess
import time


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP_EXECUTABLE = os.path.join(_ROOT, "Ted.app", "Contents", "MacOS", "Ted")
_STANDALONE_HELPER = os.path.join(_ROOT, "native", "ted_control")
_HELPER = os.environ.get("TED_CONTROL_HELPER") or (
    _APP_EXECUTABLE
    if os.environ.get("TED_NATIVE_HOST") == "1" and os.path.isfile(_APP_EXECUTABLE)
    else _STANDALONE_HELPER)
_HELPER_PREFIX = ["--control"] if _HELPER == _APP_EXECUTABLE else []
_permission_prompted = False

_CONSEQUENTIAL_TARGET = re.compile(
    r"\b(?:delete|remove|erase|trash|buy|purchase|checkout|pay|place order|"
    r"send|submit|post|publish|confirm|log out|sign out|quit|force quit|"
    r"restart|shut down|install|uninstall|allow|grant|authorize|transfer|"
    r"withdraw)\b", re.I)


def _native(command, *args):
    """Run one native control command and return its JSON object."""
    global _permission_prompted
    if not os.path.isfile(_HELPER) or not os.access(_HELPER, os.X_OK):
        return {"ok": False, "error": "Ted's native control helper is not built"}
    try:
        argv = [_HELPER, *_HELPER_PREFIX, command, *[str(a) for a in args]]
        result = subprocess.run(
            argv, capture_output=True,
            text=True, timeout=12,
        )
        line = (result.stdout or "").strip().splitlines()
        data = json.loads(line[-1]) if line else {
            "ok": False, "error": (result.stderr or "native helper failed").strip()}
        if (not data.get("ok") and "Accessibility permission" in data.get("error", "")
                and not _permission_prompted):
            _permission_prompted = True
            subprocess.run([_HELPER, *_HELPER_PREFIX, "status", "prompt"], capture_output=True,
                           text=True, timeout=12)
        return data
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def accessibility_status(prompt=False):
    return _native("status", "prompt" if prompt else "check")


def inspect_ui(query=""):
    result = _native("snapshot", query or "")
    if not result.get("ok"):
        return (result.get("error") or "Couldn't inspect the current app") + (
            ". Enable Ted in System Settings → Privacy & Security → Accessibility."
            if "Accessibility" in result.get("error", "") else ".")
    elements = result.get("elements") or []
    if not elements:
        suffix = f" matching '{query}'" if query else ""
        return f"No accessible controls{suffix} were exposed by {result.get('app', 'this app')}."
    lines = []
    for item in elements[:40]:
        name = item.get("name") or item.get("detail") or "unnamed"
        lines.append(f"{item.get('role', 'control')}: {name}")
    return f"Accessible controls in {result.get('app', 'the front app')}:\n" + "\n".join(lines)


def has_accessible_text(text):
    """Whether the frontmost app exposes exact text through Accessibility."""
    needle = " ".join(str(text or "").lower().split())
    if not needle:
        return False
    result = _native("snapshot", text)
    if not result.get("ok"):
        return False
    return any(
        needle in " ".join(str(item.get("detail") or "").lower().split())
        for item in (result.get("elements") or [])
    )


def press_target(target):
    """Press a named AX control, then use vision coordinates only if AX cannot."""
    if _CONSEQUENTIAL_TARGET.search(target or ""):
        return (f"I won't press '{target}' through generic screen control. Use "
                "the specific confirmed tool for consequential actions.")
    result = _native("press", target)
    used = "Accessibility"
    matched = result.get("matched") or target
    if not result.get("ok") and "No accessible control matched" in result.get("error", ""):
        try:
            from core.screen import locate_target
            location = locate_target(target)
        except Exception as exc:
            location = {"found": False, "error": str(exc)}
        if location.get("found") and float(location.get("confidence", 0)) >= 0.75:
            result = _native("click", location["x"], location["y"])
            used = "screen vision"
            matched = target
        else:
            why = location.get("error") or "screen vision was not confident enough"
            return f"Couldn't find a control named '{target}' — {why}."
    if not result.get("ok"):
        return (result.get("error") or f"Couldn't press '{target}'") + "."
    if used == "Accessibility":
        return f"Pressed {matched} through Accessibility."
    return (f"Clicked {matched} through screen vision. The click was sent, but "
            "the resulting page state was not semantically verified.")


def fill_field(target, text):
    """Set a labeled native or HTML form field through Accessibility, no image."""
    result = _native("fill", target, text)
    if not result.get("ok"):
        return (result.get("error") or f"Couldn't fill '{target}'") + "."
    preview = text[:40] + ("..." if len(text) > 40 else "")
    return f"Filled {result.get('matched') or target} with: {preview}"


def type_text(text):
    result = _native("type-text", text)
    if not result.get("ok"):
        return (result.get("error") or "Couldn't type the text") + "."
    preview = text[:40] + ("..." if len(text) > 40 else "")
    if result.get("verified"):
        return f"Typed: {preview}"
    # Some web editors do not expose their complete AXValue, but do expose the
    # inserted text as a descendant. Query that semantic tree before giving up;
    # this is still image-free.
    check = " ".join(text.strip().split())[:48]
    if check:
        semantic = _native("snapshot", check)
        if semantic.get("ok") and semantic.get("elements"):
            return f"Typed: {preview}"
    return f"Sent the keystrokes, but I couldn't verify that '{preview}' appeared."


def _paste_text(text):
    """Paste into a rich editor while preserving every clipboard data type."""
    result = _native("paste-text", text)
    if not result.get("ok"):
        return (result.get("error") or "Couldn't paste the text") + "."
    preview = text[:40] + ("..." if len(text) > 40 else "")
    if result.get("verified"):
        return f"Typed: {preview}"
    return f"Sent the text, but I couldn't verify that '{preview}' appeared."


def _active_browser_url(app_name):
    if app_name not in {"Google Chrome", "Brave Browser"}:
        return ""
    source = (
        f'var a=Application({json.dumps(app_name)}); '
        'var w=a.windows()[0]; w ? w.activeTab().url() : ""')
    try:
        result = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", source],
            capture_output=True, text=True, timeout=4)
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _docs_tool_command(command):
    """Run one Google Docs Tool finder command using its official Mac shortcut."""
    opened = _native("key", "tool finder")
    if not opened.get("ok"):
        return False
    time.sleep(0.15)
    typed = _native("type-text", command)
    if not typed.get("ok"):
        return False
    time.sleep(0.15)
    chosen = _native("key", "enter")
    time.sleep(0.25)
    return bool(chosen.get("ok"))


def _format_google_doc(font_size=None, line_spacing=None):
    sent = []
    failed = []
    if not (font_size or line_spacing):
        return sent, failed
    selected = _native("key", "select all")
    if not selected.get("ok"):
        return [], ["selecting the document"]
    time.sleep(0.15)
    if font_size:
        label = f"{int(font_size)}-point font"
        (sent if _docs_tool_command(f"font size {int(font_size)}") else failed).append(label)
    if line_spacing:
        spacing = str(line_spacing).lower()
        command = {"double": "double spacing", "single": "single spacing",
                   "1.5": "1.5 spacing", "1.15": "1.15 spacing"}.get(
                       spacing, f"{spacing} spacing")
        label = f"{spacing} line spacing"
        (sent if _docs_tool_command(command) else failed).append(label)
    _native("key", "right")
    return sent, failed


def create_document(text, app="google_docs", browser="Chrome", font_size=None,
                    line_spacing=None):
    """Open a fresh document, focus its editor, and type the requested text.

    This is intentionally one outcome-level action. Leaving the model to infer
    that "open Docs" must be followed by focusing an editor and typing caused
    it to stop after the browser opened and then claim it could not write.
    """
    destination = (app or "google_docs").strip().lower().replace(" ", "_")
    if destination in {"textedit", "text_edit", "local"}:
        try:
            opened = subprocess.run(
                ["open", "-a", "TextEdit"], capture_output=True, text=True,
                timeout=8)
        except Exception as exc:
            return f"I couldn't open TextEdit: {exc}"
        if opened.returncode != 0:
            return "I couldn't open TextEdit."
        deadline = time.time() + 6
        while time.time() < deadline:
            if accessibility_status().get("frontmost") == "TextEdit":
                break
            time.sleep(0.2)
        made = _native("key", "new")
        if not made.get("ok"):
            return (made.get("error") or "I couldn't create a new TextEdit document") + "."
        deadline = time.time() + 5
        while time.time() < deadline:
            ready = _native("snapshot", "First Text View")
            if ready.get("ok") and ready.get("elements"):
                break
            time.sleep(0.2)
        # TextEdit exposes a settable AXTextArea, so use it and verify the exact
        # value instead of depending on whichever control happened to retain
        # keyboard focus during its launch animation.
        filled = _native("fill", "First Text View", text)
        if filled.get("ok") and filled.get("verified"):
            return "Opened a new TextEdit document and typed the text."
        typed = type_text(text)
        if typed.startswith("Typed:"):
            return "Opened a new TextEdit document and typed the text."
        return "Opened a new TextEdit document. " + typed

    if destination not in {"google_docs", "docs", "google"}:
        return "I can create a document in Google Docs or TextEdit."

    # docs.new creates a blank document directly, avoiding brittle template
    # gallery coordinates. Launch Services reuses the existing browser window
    # and opens a new tab unless the user explicitly asked for a new window.
    from core.tool_handlers import tool_browse_to
    opened = tool_browse_to("https://docs.new", browser=browser or "Chrome")
    if "couldn't" in opened.lower():
        return opened
    expected = {"chrome": "Google Chrome", "google chrome": "Google Chrome",
                "google": "Google Chrome", "brave": "Brave Browser"}.get(
                    (browser or "Chrome").strip().lower(), browser or "Google Chrome")
    deadline = time.time() + 12
    while time.time() < deadline:
        status = accessibility_status()
        front = status.get("frontmost", "")
        url = _active_browser_url(expected)
        editor_url = ("docs.google.com/document/d/" in url and "/edit" in url)
        if front == expected and editor_url:
            # A gallery card and a real editor both contain the word "document".
            # Only the editor exposes Document content, so this prevents typing
            # into the Docs home page and calling the outcome complete.
            ready = _native("snapshot", "Document content")
            if ready.get("ok") and any(
                    item.get("role") == "AXTextArea"
                    for item in ready.get("elements", [])):
                focused = _native("focus", "Document content")
                if focused.get("ok"):
                    break
        time.sleep(0.35)
    else:
        return ("Google Docs opened, but I never reached a writable new-document "
                "editor, so I did not type anywhere.")
    # Google Docs' canvas ignores synthetic Unicode key events even when its
    # AXTextArea is focused. A normal Command-V is accepted; the native helper
    # snapshots and restores every clipboard representation around the paste.
    typed = _paste_text(text)
    if typed.startswith("Typed:"):
        sent, failed = _format_google_doc(font_size, line_spacing)
        result = (f"Opened a new Google Doc in {expected}'s existing window and "
                  "typed the text.")
        if sent:
            # The native helper confirms that the official Tool finder command
            # was entered and chosen, but Docs' canvas does not expose the
            # resulting paragraph style reliably through Accessibility.
            result += " Sent Google Docs' formatting command for " + " and ".join(sent) + "."
        if failed:
            result += " I couldn't apply " + " or ".join(failed) + "."
        return result
    return f"Opened a new Google Doc in {expected}'s existing window. " + typed


def press_key(key):
    result = _native("key", key)
    return (f"Pressed {key}." if result.get("ok") else
            (result.get("error") or f"Couldn't press {key}") + ".")


def scroll(direction="down", amount=600):
    pixels = max(80, min(2400, int(amount or 600)))
    # Quartz positive values scroll up; negative values scroll down.
    signed = pixels if str(direction).lower() == "up" else -pixels
    result = _native("scroll", signed)
    return (f"Scrolled {direction}." if result.get("ok") else
            (result.get("error") or "Couldn't scroll") + ".")


def get_focused_app():
    return accessibility_status().get("frontmost", "Unknown")


def get_clipboard():
    try:
        return subprocess.run(["pbpaste"], capture_output=True, text=True,
                              timeout=5).stdout
    except Exception:
        return ""


def set_clipboard(text):
    try:
        result = subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
        return "Copied." if result.returncode == 0 else "Clipboard write failed."
    except Exception as exc:
        return f"Clipboard error: {exc}"
