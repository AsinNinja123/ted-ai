"""
core/screen.py — Screenshot + vision through Ted's normal provider route.

Qwen 3.6 handles normal screenshots on Groq; the same request falls back to
the local multimodal Qwen model when Groq or the internet is unavailable.

Public API:
    take_screenshot(path)          → path to PNG file, or None on failure
    describe_screen(question)      → text description of the current screen
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 27 (§27.2)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Take a screenshot, send it to the model, describe what is on it. Goes through
#      the same provider door as everything else, so it works offline against the
#      local multimodal model too.
#
#  A SMALL PRIVACY DETAIL WORTH KEEPING
#      The screenshot is held in memory and never written to disk.
#
# =============================================================================

import os
import base64
import subprocess
import tempfile
import json

from PIL import Image

from core.providers import CLOUD_CHAT_MODEL

VISION_MODEL = CLOUD_CHAT_MODEL


def _to_screen_points(x, y, image_width, image_height, point_size=None):
    """Convert Retina screenshot pixels to Quartz event coordinates."""
    if point_size is None:
        try:
            import Quartz
            bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            point_size = (float(bounds.size.width), float(bounds.size.height))
        except Exception:
            point_size = (image_width, image_height)
    return (x * point_size[0] / image_width,
            y * point_size[1] / image_height)


def take_screenshot(path: str = None) -> str:
    if path is None:
        handle = tempfile.NamedTemporaryFile(
            prefix="ted_screen_", suffix=".png", delete=False)
        path = handle.name
        handle.close()
    try:
        result = subprocess.run(
            ["screencapture", "-x", path],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0 or not os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        return path
    except Exception as e:
        print(f"[screen] screenshot failed: {e}")
        try:
            os.unlink(path)
        except OSError:
            pass
        return None


def screenshot_policy():
    """User-facing retention description for diagnostics and tool results."""
    return ("Screenshots are created as uniquely named files in the macOS temporary "
            "folder and deleted immediately after vision reads them. Ted does not "
            "keep a screenshot history.")


def describe_screen(question: str = "Briefly describe what's on the screen.") -> str:
    path = take_screenshot()
    if not path:
        return "Couldn't take a screenshot right now."

    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        print(f"[screen] image read failed: {e}")
        return "Couldn't read the screenshot."
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

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


def locate_target(target: str):
    """Locate a visible target as screen coordinates for a cautious fallback click."""
    path = take_screenshot()
    if not path:
        return {"found": False, "error": "couldn't take a screenshot"}
    try:
        with Image.open(path) as image:
            width, height = image.size
        with open(path, "rb") as handle:
            b64 = base64.b64encode(handle.read()).decode()
    except Exception as exc:
        return {"found": False, "error": str(exc)}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    try:
        from core.llm import chat_create
        response = chat_create(
            messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": (
                    f"Find the center of the visible UI control '{target}'. "
                    f"The screenshot is {width} by {height} pixels. Reply JSON only: "
                    '{"found":true,"x":123,"y":456,"confidence":0.92} '
                    "or {\"found\":false,\"x\":0,\"y\":0,\"confidence\":0}. "
                    "Do not guess when the target is ambiguous.")},
            ]}],
            response_format={"type": "json_object"}, max_tokens=140,
            reasoning_effort="none", timeout=20.0,
        )
        raw = (response.choices[0].message.content or "").strip()
        data = json.loads(raw)
        x, y = float(data.get("x", 0)), float(data.get("y", 0))
        if not (0 <= x < width and 0 <= y < height):
            return {"found": False, "error": "vision returned off-screen coordinates"}
        point_x, point_y = _to_screen_points(x, y, width, height)
        return {"found": bool(data.get("found")), "x": point_x, "y": point_y,
                "confidence": float(data.get("confidence", 0) or 0)}
    except Exception as exc:
        return {"found": False, "error": f"screen target lookup failed: {exc}"}
