"""Checks for links and pictures appearing inside the conversation.

Charlie asked for images and pages to show up in the chat rather than only as
"I opened Chrome for you". Two halves are pinned here:

* **The renderer**, executed for real in Node rather than string-matched. The
  interesting failures are all in the substitution order, and a grep for
  "imgcard" would pass while the output was nested anchors and eaten digits.
* **The safety rule** that a link in a reply is never a live href. The HUD is a
  page inside pywebview; a real anchor navigates the window away from Ted and
  there is no way back — the app simply becomes the website.

Node is used only as a JS engine. If it is absent the renderer checks are
skipped rather than failed, because that is a missing tool and not a bug.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "ui", "ted_hud.html")
HUD = open(UI, encoding="utf-8").read()

PASS = FAIL = 0


def check(desc, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


def render(cases):
    """Run the HUD's own mdlite() over each case, in Node."""
    wanted = ("esc", "prettyUrl", "linkHtml", "imgHtml", "mdlite",
              "codeboxHtml", "highlight", "_KW")
    chunks = []
    for name in wanted:
        # Pull each helper out of the page by brace matching, so the test uses
        # the shipping implementation instead of a copy that can drift.
        m = re.search(r"^(?:function %s\(|var %s\s*=)" % (name, name),
                      HUD, re.M)
        if not m:
            continue
        start = m.start()
        depth, i, seen = 0, start, False
        while i < len(HUD):
            if HUD[i] == "{":
                depth += 1
                seen = True
            elif HUD[i] == "}":
                depth -= 1
                if seen and depth == 0:
                    break
            i += 1
        end = HUD.find("\n", i)
        chunks.append(HUD[start:end if end > 0 else i + 1])
    script = "\n".join(chunks) + """
const cases = %s;
console.log(JSON.stringify(cases.map(c => mdlite(c))));
""" % json.dumps(cases)
    path = os.path.join(tempfile.mkdtemp(), "mdlite.js")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(script)
    out = subprocess.run(["node", path], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:400])
    return json.loads(out.stdout.strip().splitlines()[-1])


print("— the renderer —")
if not shutil.which("node"):
    print("  · node not installed — renderer checks skipped")
else:
    try:
        got = render([
            "I have 3 exams and 2 papers this week.",
            "Check https://groq.com/pricing for the rates.",
            "See [the docs](https://console.groq.com/docs) for that.",
            "![a red panda](https://example.com/panda.jpg)",
            "I found 2 options: [one](https://a.com/x) and https://b.com/y — 4 stars.",
            "That is **very** important, use `pmset -g assertions`.",
            "Go to https://example.com/page, then stop.",
            "email me at bob@example.com please",
        ])
    except Exception as exc:                                   # pragma: no cover
        got = None
        FAIL += 1
        print(f"  ✗ could not run the renderer in node: {exc}")

    if got:
        digits, bare, mdlink, image, mixed, fmt, punct, email = got

        # The substitution uses numbered placeholders. With a digit delimiter
        # " 3 " in "I have 3 exams" is indistinguishable from placeholder 3,
        # and ordinary prose gets rewritten into undefined.
        check("plain digits survive the placeholder pass",
              "3 exams" in digits and "2 papers" in digits
              and "undefined" not in digits)
        check("a bare URL becomes a link", 'class="lnk"' in bare
              and 'data-url="https://groq.com/pricing"' in bare)
        check("…showing a readable label, not the raw address",
              ">groq.com/pricing<" in bare)
        check("a markdown link keeps its text",
              ">the docs<" in mdlink
              and 'data-url="https://console.groq.com/docs"' in mdlink)
        check("a markdown image becomes a figure",
              '<figure class="imgcard">' in image
              and 'src="https://example.com/panda.jpg"' in image)
        check("…with a caption and a collapse-on-error handler",
              "<figcaption>a red panda</figcaption>" in image
              and "broken" in image)

        # The ordering bug: linkifying bare URLs after inserting anchors makes
        # the regex match the href of the anchor just written.
        check("a link and a bare URL in one sentence do not nest",
              mixed.count("<a ") == 2 and "<a" not in mixed.split("<a ")[1].split("</a>")[0])
        check("…and the digits around them are untouched",
              "2 options" in mixed and "4 stars" in mixed)
        check("bold and inline code still work",
              "<b>very</b>" in fmt and "<code>pmset -g assertions</code>" in fmt)
        check("sentence punctuation is not swallowed into the address",
              'data-url="https://example.com/page"' in punct and punct.rstrip().endswith("stop."))
        check("an email address is not turned into a link", "lnk" not in email)

print("\n— a link in a reply is never live —")
check("anchors are inert", 'href="#"' in HUD and "class=\"lnk\"" in HUD)
check("the address rides in a data attribute", "data-url=" in HUD)
check("clicking is routed to Python, not to the webview",
      "open_url_external" in HUD and "e.preventDefault()" in HUD)

print("\n— what Python will open —")
# Read rather than imported on purpose: importing core.app boots the audio
# engine and calibrates the microphone, which is a heavy side effect for a
# test that only needs to know what the code says.
src = open(os.path.join(os.path.dirname(UI), "..", "core", "app.py"),
           encoding="utf-8").read()
check("only http(s) is openable",
      'startswith(("http://", "https://"))' in src)
check("…so a hallucinated file:// or javascript: link is refused",
      "refused non-web URL" in src)

print("\n— finding pictures —")
from core import tool_handlers as th
check("an empty query searches for nothing", th.find_images("") == [])
check("a silly count is clamped rather than passed through",
      "min(int(count), 6)" in open(
          os.path.join(os.path.dirname(UI), "..", "core", "tool_handlers.py"),
          encoding="utf-8").read())

print("\n— the model narrates, the window renders —")
check("pictures are pushed to the HUD, not returned as markdown",
      "tedHud.showMedia" in src)
check("…and the model is told only what was really shown",
      "Showed {len(found)} picture" in src)
check("a search that found nothing says so plainly",
      "nothing was" in src)

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
