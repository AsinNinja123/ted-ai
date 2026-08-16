"""Checks for attaching files, photos and PDFs to a turn.

The expensive half (does Qwen actually see the image) needs the network and is
verified by hand; what is pinned here is everything that decides *what gets
sent* — because the failure that matters is not a bad answer, it is Ted
receiving an empty attachment and inventing what was in it.
"""

import base64
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import attachments as A


PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


TMP = tempfile.mkdtemp(prefix="ted-attach-test-")


def write(name, data="hello"):
    path = os.path.join(TMP, name)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as fh:
        fh.write(data)
    return path


def make_png(name="shot.png", size=(40, 30)):
    from PIL import Image
    path = os.path.join(TMP, name)
    Image.new("RGB", size, (90, 140, 200)).save(path)
    return path


print("— what kind of file is this —")
check("a photo is an image", A.kind_for("/x/holiday.JPG") == "image")
check("an iPhone photo is an image", A.kind_for("/x/IMG_0042.heic") == "image")
check("a pdf is a document", A.kind_for("/x/syllabus.pdf") == "document")
check("source code is text", A.kind_for("/x/app.py") == "text")
check("a video is not something Ted reads",
      A.kind_for("/x/lecture.mov") == "unsupported")
# An extensionless file is usually still text (LICENSE, Makefile, a dotfile).
check("an extensionless text file is sniffed, not refused",
      A.kind_for(write("LICENSE", "MIT License\n")) == "text")
check("…and an extensionless binary is still refused",
      A.kind_for(write("blob", b"\x00\x01\x02\x00")) == "unsupported")

print("\n— refusals are reported, never staged silently —")
missing = A.load(os.path.join(TMP, "nope.pdf"))
check("a file that isn't there says so",
      not missing.ok and "isn't there" in missing.error)
big = write("huge.txt", "x" * 64)
os.truncate(big, A.MAX_BYTES + 1)
oversized = A.load(big)
check("an oversized file is refused before it is read",
      not oversized.ok and "over the" in oversized.error)
empty = A.load(write("blank.txt", "   \n"))
check("an empty text file is refused", not empty.ok and "empty" in empty.error)
check("a refused file is never ok", not any(a.ok for a in
      (missing, oversized, empty)))

print("\n— text and documents —")
doc = A.load(write("syllabus.txt", "Exam 1: Thursday\nExam 2: September 24\n"),
             file_to_knowledge=False)
check("a text file is read", doc.ok and "September 24" in doc.text)
check("…and summarised for the chip", "words" in doc.summary())
long_doc = A.load(write("thesis.txt", "word " * 20000), file_to_knowledge=False)
check("a very long file is truncated rather than sent whole",
      long_doc.truncated and len(long_doc.text) <= A.MAX_TEXT_CHARS)
check("…and the chip says so", "truncated" in long_doc.summary())

print("\n— images —")
img = A.load(make_png(), file_to_knowledge=False)
check("a png becomes a data URL", img.ok and img.data_url.startswith("data:image/"))
check("…that is real base64",
      len(base64.b64decode(img.data_url.split(",", 1)[1])) > 0)

from PIL import Image
big_img = A.load(make_png("wide.png", (4032, 3024)), file_to_knowledge=False)
decoded = Image.open(io.BytesIO(base64.b64decode(big_img.data_url.split(",", 1)[1])))
check("a phone-sized photo is downscaled before it is sent",
      max(decoded.size) == A.MAX_IMAGE_EDGE)
check("…keeping its aspect ratio", abs(decoded.width / decoded.height - 4 / 3) < 0.02)

print("\n— composing the message —")
# An ordinary turn must come out byte-identical, because Groq's prefix cache
# depends on the static part of the prompt not moving.
check("no attachment leaves the message a plain string",
      A.build_user_content("hey ted", []) == "hey ted")
check("…and refused files count as no attachment",
      A.build_user_content("hey ted", [missing, oversized]) == "hey ted")

with_doc = A.build_user_content("when is exam 2", [doc])
check("a document rides inside the text part",
      isinstance(with_doc, str) and "September 24" in with_doc
      and "when is exam 2" in with_doc)
check("…and is fenced so the model can tell it from the question",
      "--- attached file: syllabus.txt ---" in with_doc
      and "--- end of attached files ---" in with_doc)

with_img = A.build_user_content("what is this", [img])
check("an image makes the message multi-part", isinstance(with_img, list))
check("…with the image first and the question last",
      with_img[0]["type"] == "image_url" and with_img[-1]["type"] == "text"
      and with_img[-1]["text"] == "what is this")
check("an image with no question still asks one",
      A.build_user_content("", [img])[-1]["text"].strip() != "")

both = A.build_user_content("compare these", [doc, img])
check("a document and an image travel together",
      isinstance(both, list) and both[0]["type"] == "image_url"
      and "September 24" in both[-1]["text"])

print("\n— what the HUD is told —")
d = img.as_dict()
check("the chip gets name, kind and size",
      d["name"] == "shot.png" and d["kind"] == "image" and d["size"] > 0)
check("the chip can re-stage itself after a removal", d["path"] == img.path)
check("the base64 is NOT echoed back to the window", "data_url" not in d)
check("neither is the extracted text", "text" not in d)
check("a description names every usable file",
      "syllabus.txt" in A.describe_for_log([doc, img, missing])
      and "shot.png" in A.describe_for_log([doc, img, missing]))
check("…and omits the ones that failed",
      "nope.pdf" not in A.describe_for_log([doc, missing]))

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
