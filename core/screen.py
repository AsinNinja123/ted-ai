"""
core/screen.py — Screenshot + vision through Ted's normal provider route.

Qwen 3.6 handles normal screenshots on Groq; the same request falls back to
the local multimodal Qwen model when Groq or the internet is unavailable.

Public API:
    take_screenshot(path)          → path to PNG file, or None on failure
    describe_screen(question)      → text description of the current screen
"""

import os
import base64
import subprocess
import tempfile

from core.providers import CLOUD_CHAT_MODEL

VISION_MODEL = CLOUD_CHAT_MODEL


def take_screenshot(path: str = None) -> str:
    if path is None:
        path = os.path.join(tempfile.gettempdir(), "ted_screen.png")
    try:
        result = subprocess.run(
            ["screencapture", "-x", path],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0 or not os.path.isfile(path):
            return None
        return path
    except Exception as e:
        print(f"[screen] screenshot failed: {e}")
        return None


def describe_screen(question: str = "Briefly describe what's on the screen.") -> str:
    path = take_screenshot()
    if not path:
        return "Couldn't take a screenshot right now."

    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        os.unlink(path)
    except Exception as e:
        print(f"[screen] image read failed: {e}")
        return "Couldn't read the screenshot."

    try:
        from core.llm import chat_create
        resp = chat_create(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                        {
                            "type": "text",
                            "text": question,
                        },
                    ],
                }
            ],
            max_tokens=500,
            timeout=20.0,
        )
        return (resp.choices[0].message.content or "").strip() or "Nothing to describe."
    except Exception as e:
        print(f"[screen] vision error: {e}")
        return "Screen description failed — neither vision provider was available."
