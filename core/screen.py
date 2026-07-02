"""
core/screen.py — Screenshot + vision description using Groq (Llama 4 Scout).

Uses the existing GROQ_API_KEY — no new credentials required.

Public API:
    take_screenshot(path)          → path to PNG file, or None on failure
    describe_screen(question)      → text description of the current screen
"""

import os
import base64
import subprocess
import tempfile

try:
    from config import GROQ_API_KEY
except Exception:
    GROQ_API_KEY = ""

VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


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
    if not GROQ_API_KEY:
        return "I need a Groq API key to see the screen."

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
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        resp = client.chat.completions.create(
            model=VISION_MODEL,
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
        print(f"[screen] vision API error: {e}")
        return "Screen description failed. The vision model may be unavailable."
