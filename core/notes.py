"""
core/notes.py — Read and write Apple Notes via AppleScript.

Public API:
    add_note(title, body, folder)       → confirmation string
    append_to_note(title_fragment, text) → confirmation string
    search_notes(query)                  → list of {title, snippet}
    get_note(title_fragment)             → full note body string, or ""
"""

import subprocess


def _run(script: str) -> str:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        return r.stdout.strip()
    except Exception as e:
        print(f"[notes] AppleScript error: {e}")
        return ""


def add_note(title: str, body: str, folder: str = None) -> str:
    safe_title = title.replace('"', "'")
    safe_body  = body.replace('"', "'").replace("\n", "\\n")
    if folder:
        safe_folder = folder.replace('"', "'")
        script = f"""
tell application "Notes"
    tell folder "{safe_folder}"
        make new note with properties {{name: "{safe_title}", body: "{safe_body}"}}
    end tell
end tell
"""
    else:
        script = f"""
tell application "Notes"
    make new note with properties {{name: "{safe_title}", body: "{safe_body}"}}
end tell
"""
    _run(script)
    return f"Note created: {title}."


def append_to_note(title_fragment: str, text: str) -> str:
    safe_frag = title_fragment.replace('"', "'")
    safe_text = text.replace('"', "'").replace("\n", "\\n")
    script = f"""
tell application "Notes"
    set matchNote to missing value
    repeat with n in notes
        if name of n contains "{safe_frag}" then
            set matchNote to n
            exit repeat
        end if
    end repeat
    if matchNote is not missing value then
        set body of matchNote to (body of matchNote) & return & "{safe_text}"
        return "ok"
    else
        return "not found"
    end if
end tell
"""
    result = _run(script)
    if result == "ok":
        return f"Appended to {title_fragment}."
    return f"No note found matching '{title_fragment}'."


def search_notes(query: str) -> list:
    safe_q = query.replace('"', "'")
    script = f"""
tell application "Notes"
    set outList to {{}}
    repeat with n in notes
        if name of n contains "{safe_q}" or body of n contains "{safe_q}" then
            set bodySnip to text 1 thru (min {{100, length of (body of n)}}) of (body of n)
            set end of outList to (name of n & "|" & bodySnip)
        end if
    end repeat
    return outList
end tell
"""
    raw = _run(script)
    if not raw:
        return []
    results = []
    for chunk in raw.split(", "):
        chunk = chunk.strip()
        if "|" not in chunk:
            continue
        parts = chunk.split("|", 1)
        results.append({
            "title":   parts[0].strip(),
            "snippet": parts[1].strip() if len(parts) > 1 else "",
        })
    return results


def get_note(title_fragment: str) -> str:
    safe_frag = title_fragment.replace('"', "'")
    script = f"""
tell application "Notes"
    repeat with n in notes
        if name of n contains "{safe_frag}" then
            return body of n
        end if
    end repeat
    return ""
end tell
"""
    return _run(script)
