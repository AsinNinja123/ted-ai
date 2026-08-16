"""core/attachments.py — turn a file Charlie attached into something a turn can use.

Two destinations, decided by what the file actually is:

* **Images** become a data URL on the user message, which is the same shape
  core/screen.py already sends for screenshots. Qwen 3.6 handles them on Groq
  and the local multimodal model handles them offline, so this needed no new
  provider — only a way in.
* **Documents and text** are extracted to plain text and put in the prompt.
  Anything long is also filed in the knowledge base, so it stays askable after
  the conversation that introduced it has scrolled away.

Nothing here reaches the network. Loading is pure local file work, so a broken
or enormous file fails as a message in the chat rather than as a stalled turn.
"""

from __future__ import annotations

import base64
import io
import mimetypes
import os
import subprocess
import tempfile
from dataclasses import dataclass, field

# Anything Preview would open. HEIC is included because it is what an iPhone
# photo actually is, and Charlie will drag those in without thinking about it.
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif",
             ".heic", ".heif"}
DOC_EXT = {".pdf"}
TEXT_EXT = {".txt", ".md", ".text", ".csv", ".tsv", ".json", ".jsonl", ".log",
            ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".html", ".css",
            ".py", ".js", ".jsx", ".ts", ".tsx", ".swift", ".java", ".c", ".h",
            ".cpp", ".hpp", ".rs", ".go", ".rb", ".sh", ".zsh", ".sql", ".r",
            ".m", ".mm", ".kt", ".php", ".pl", ".lua", ".vim", ".gitignore"}

# Refused before reading, not after. A 400 MB video dragged in by accident
# should cost one stat() call.
MAX_BYTES = 25 * 1024 * 1024
# Enough for a long paper; past this the tail is dropped from the prompt and
# the knowledge base is the way to reach the rest.
MAX_TEXT_CHARS = 24_000
# Groq counts an image against the request either way, so the only thing a
# 4032px phone photo buys over a 1400px one is latency.
MAX_IMAGE_EDGE = 1400


@dataclass
class Attachment:
    """One attached file, already resolved into what the model will receive."""
    path: str
    name: str
    kind: str = "unsupported"          # image | document | text | unsupported
    size: int = 0
    text: str = ""                     # extracted, for document/text
    data_url: str = ""                 # for image
    truncated: bool = False
    error: str = ""
    filed_chunks: int = 0              # rows added to the knowledge base

    @property
    def ok(self):
        return not self.error and self.kind != "unsupported"

    def summary(self):
        """One line for the HUD chip and for Ted's own grounding."""
        if self.error:
            return f"{self.name} — {self.error}"
        if self.kind == "image":
            return f"{self.name} (image)"
        words = len(self.text.split())
        note = f"{words:,} words" if words else "no readable text"
        if self.truncated:
            note += ", truncated"
        if self.filed_chunks:
            note += f", filed in knowledge ({self.filed_chunks} chunks)"
        return f"{self.name} ({note})"

    def as_dict(self):
        """What the HUD needs. Deliberately excludes data_url and text — the
        window does not need a 2 MB base64 string echoed back at it.

        ``path`` is included so removing one chip can re-stage the others; for
        a dropped or pasted file that is the temp copy, which is exactly the
        thing that can be read again.
        """
        return {"name": self.name, "kind": self.kind, "size": self.size,
                "path": self.path, "error": self.error,
                "summary": self.summary()}


def kind_for(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in DOC_EXT:
        return "document"
    if ext in TEXT_EXT:
        return "text"
    # An extensionless file is usually still text (LICENSE, Makefile, a dotfile).
    # Guess from content rather than refusing something perfectly readable.
    if not ext and _looks_like_text(path):
        return "text"
    return "unsupported"


def _looks_like_text(path, sniff=2048):
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(sniff)
    except Exception:
        return False
    if not chunk or b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _heic_to_png(path):
    """Convert with macOS's own sips. Pillow cannot read HEIC without an extra
    wheel, and an iPhone screenshot is the single likeliest thing to be
    attached, so this must not depend on an optional install."""
    out = os.path.join(tempfile.mkdtemp(), "converted.png")
    try:
        result = subprocess.run(
            ["sips", "-s", "format", "png", path, "--out", out],
            capture_output=True, timeout=25)
        if result.returncode == 0 and os.path.isfile(out):
            return out
    except Exception as exc:
        print(f"[attach] sips conversion failed: {exc}")
    return ""


def _image_data_url(path):
    """Downscale and re-encode to a data URL. Returns ("", reason) on failure."""
    source, ext = path, os.path.splitext(path)[1].lower()
    if ext in {".heic", ".heif"}:
        source = _heic_to_png(path)
        if not source:
            return "", "I couldn't convert that HEIC image."
    try:
        from PIL import Image
        with Image.open(source) as im:
            # A palette or 16-bit image will not survive JPEG/PNG encoding as
            # itself; normalising first avoids a mode error deep in save().
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGB")
            if max(im.size) > MAX_IMAGE_EDGE:
                ratio = MAX_IMAGE_EDGE / float(max(im.size))
                im = im.resize((max(1, int(im.width * ratio)),
                                max(1, int(im.height * ratio))),
                               Image.LANCZOS)
            buf = io.BytesIO()
            # PNG keeps text in screenshots crisp, which is most of what gets
            # attached; photographs are the exception and JPEG is fine there.
            fmt = "PNG" if im.mode == "RGBA" or ext == ".png" else "JPEG"
            if fmt == "JPEG" and im.mode != "RGB":
                im = im.convert("RGB")
            im.save(buf, format=fmt, quality=86, optimize=True)
        mime = "image/png" if fmt == "PNG" else "image/jpeg"
        return (f"data:{mime};base64,"
                + base64.b64encode(buf.getvalue()).decode()), ""
    except Exception as exc:
        print(f"[attach] image encode failed: {exc}")
        return "", "I couldn't read that image."


def load(path, file_to_knowledge=True):
    """Resolve one path into an Attachment. Never raises."""
    path = os.path.expanduser(str(path or "").strip())
    name = os.path.basename(path) or "attachment"
    if not path or not os.path.isfile(path):
        return Attachment(path=path, name=name, error="that file isn't there")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return Attachment(path=path, name=name, error=f"I couldn't read it ({exc})")
    if size > MAX_BYTES:
        return Attachment(path=path, name=name, size=size,
                          error=f"it's {size / 1e6:.0f} MB, over the "
                                f"{MAX_BYTES / 1e6:.0f} MB limit")

    kind = kind_for(path)
    att = Attachment(path=path, name=name, kind=kind, size=size)

    if kind == "image":
        att.data_url, att.error = _image_data_url(path)
        if att.error:
            att.kind = "unsupported"
        return att

    if kind in ("document", "text"):
        from core import knowledge
        text = knowledge._extract_text(path) or ""
        if not text.strip():
            att.error = ("I couldn't get any text out of it — it may be a "
                         "scanned image rather than a text PDF"
                         if kind == "document" else "it looks empty")
            att.kind = "unsupported"
            return att
        if len(text) > MAX_TEXT_CHARS:
            att.truncated = True
            att.text = text[:MAX_TEXT_CHARS]
        else:
            att.text = text
        # Long documents outlive the turn that introduced them, so they are
        # also filed. Short ones are not: a two-line note does not belong in a
        # vector store competing with real sources.
        if file_to_knowledge and len(text) > 2000:
            try:
                from core import features
                if features.HAS_KNOWLEDGE:
                    att.filed_chunks = knowledge.add_text(text, source=name)
            except Exception as exc:
                print(f"[attach] knowledge filing skipped: {exc}")
        return att

    att.error = "I can't read that kind of file yet"
    return att


def load_many(paths, file_to_knowledge=True):
    return [load(p, file_to_knowledge=file_to_knowledge) for p in (paths or [])]


def build_user_content(user_text, attachments):
    """Compose the user message that carries these attachments.

    Returns a plain string when there is nothing to attach, so an ordinary turn
    produces byte-identical output to before — the prefix cache depends on it.
    Returns the multi-part list form only when an image is actually present.
    """
    usable = [a for a in (attachments or []) if a.ok]
    if not usable:
        return user_text

    # Document text rides in the text part rather than as a separate message,
    # so it stays attached to the question it was sent with.
    docs = [a for a in usable if a.kind in ("document", "text")]
    images = [a for a in usable if a.kind == "image"]

    text = user_text or ""
    if docs:
        blocks = []
        for att in docs:
            head = f"--- attached file: {att.name} ---"
            tail = ("\n[truncated — the rest is in Ted's knowledge base]"
                    if att.truncated else "")
            blocks.append(f"{head}\n{att.text}{tail}")
        joined = "\n\n".join(blocks)
        text = (f"{joined}\n\n--- end of attached files ---\n\n"
                f"{user_text}" if user_text else joined)

    if not images:
        return text

    parts = [{"type": "image_url", "image_url": {"url": a.data_url}}
             for a in images]
    parts.append({"type": "text",
                  "text": text or "What do you make of this?"})
    return parts


def describe_for_log(attachments):
    """A short, human line naming what came in — used in the chat bubble and
    in Ted's telemetry, so an attached turn is identifiable after the fact."""
    usable = [a for a in (attachments or []) if a.ok]
    if not usable:
        return ""
    return "Attached: " + ", ".join(a.summary() for a in usable)
