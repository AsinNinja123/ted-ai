"""core/llm.py — Ted's brain: Groq client, prompts, streaming replies,
web search, the 'ask Claude' relay, and the small composition/summarisation
helpers used by messaging and email.
"""

import json
import re
import threading
import time
import traceback
from datetime import date

from groq import Groq

from core import features, intents
from core.actions import detect_action
from core.hud_bridge import show_issue
from core.logs import error_log
from core.memory import save_memory, get_memory, save_fact, get_facts_about

from config import GROQ_API_KEY  # required — app won't start without this
try:
    from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
except Exception:
    ANTHROPIC_API_KEY, CLAUDE_MODEL = "", "claude-sonnet-5"
try:
    from config import OWNER_NAME
except Exception:
    OWNER_NAME = "Charlie"

# Groq model line-up: big model for replies + tool calling, fast model for
# JSON extraction and summaries, compound for live-web questions (it runs a
# real web search server-side before answering — no extra API keys needed).
#
# gpt-oss-120b benchmarked 5-9x faster than llama-3.3-70b with better tool
# calling (it handled the misheard-verb cases that made llama emit malformed
# tool calls). It shares a rate-limit pool with groq/compound, so every chat
# call goes through chat_create(), which falls back to llama automatically.
CHAT_MODEL          = "openai/gpt-oss-120b"
CHAT_FALLBACK_MODEL = "llama-3.3-70b-versatile"
FAST_MODEL          = "llama-3.1-8b-instant"
WEB_MODEL           = "groq/compound-mini"

groq_client = Groq(api_key=GROQ_API_KEY)


def chat_create(**kwargs):
    """chat.completions.create on the primary chat model, with automatic
    fallback to CHAT_FALLBACK_MODEL when the primary is rate-limited or
    erroring (they share limits with the web-answer model). gpt-oss is a
    reasoning model — reasoning_effort='low' keeps voice latency snappy and
    is only sent to models that accept it."""
    last_exc = None
    for model in (CHAT_MODEL, CHAT_FALLBACK_MODEL):
        params = dict(kwargs)
        params["model"] = model
        if model.startswith("openai/gpt-oss"):
            params["reasoning_effort"] = "low"
        try:
            return groq_client.chat.completions.create(**params)
        except Exception as e:
            last_exc = e
            msg = str(e)
            retryable = any(k in msg for k in
                            ("429", "rate_limit", "over capacity", "503",
                             "500", "413", "request_too_large",
                             # model retired/renamed — keep Ted alive on llama
                             "404", "model_not_found", "decommissioned"))
            if model != CHAT_FALLBACK_MODEL and retryable:
                print(f"[llm] {model} unavailable ({msg[:80]}) — "
                      f"falling back to {CHAT_FALLBACK_MODEL}")
                continue
            raise
    raise last_exc

MAX_HISTORY = 20        # messages sent to LLM per turn
MAX_CONV_MESSAGES = 40  # hard cap on stored conversation length (keeps system msg at [0])

# Tracks Groq reachability for the HUD health dot (flipped inside ask_streaming).
_GROQ_OK = True

def groq_ok():
    return _GROQ_OK

# ---------- persona ----------
SYSTEM_PROMPT = (
    f"You are Ted, {OWNER_NAME}'s AI. Think Jarvis from Iron Man — not a chatbot, "
    "a capable system that happens to have a personality. "

    # Brevity — the most important rule
    "Default to 1-2 sentences. That's it. If the answer fits in one sentence, use one. "
    "Only go longer when the question genuinely requires it — detailed how-tos, "
    "multi-part answers, or when asked to elaborate. "
    "Never pad. Never recap what was just said. Never summarise your own answer. "

    # Openers — hard ban
    "Never open with: 'Got it', 'Sure', 'Of course', 'Certainly', 'Absolutely', "
    "'Great', 'Sounds good', 'Happy to', 'No problem', 'I'd be happy to'. "
    "Just answer. The first word out of your mouth should be useful. "

    # Hedging — hard ban
    "Never hedge: no 'I think', 'it seems like', 'you might want to', 'perhaps', "
    "'it could be'. State the answer. If you're genuinely uncertain, say 'Not sure — "
    "double-check that' and move on. One sentence, not a paragraph of caveats. "

    # Tone — confident, dry, slightly formal
    "You're confident and direct, with a dry wit. Not warm and chatty — more like a "
    "brilliant, efficient colleague who respects your time. Occasional dry humour is "
    "fine, never forced. Contractions are fine. Don't be stiff. "

    # Pushback
    "If something seems off, say so plainly: 'That doesn't add up.' or 'I'd push back "
    "on that.' You don't just confirm whatever you're told. "

    # Name use
    f"Use {OWNER_NAME}'s name sparingly — once every several exchanges at most. "

    # Voice format
    "Everything is spoken aloud: no markdown, no bullet points, no lists, no emojis. "
    "Numbers and amounts spoken as words. Short sentences over long ones. "

    # Memory
    "You remember this conversation and facts about the user. Reference them naturally "
    "when relevant — never announce it with phrases like 'according to my memory'. "

    # Capabilities
    "You can set timers and reminders, keep named lists, give a morning briefing, "
    "control Spotify, and relay hard questions to Claude. Nudge toward clear phrasing "
    "if needed — 'set a timer for ten minutes', 'remind me to call back in an hour'. "

    # Honesty about actions — the one rule you never break
    "You can ONLY perform actions through your tools. Never claim to have opened, "
    "closed, sent, set, scheduled, added, played, typed, or done anything unless a "
    "tool actually ran and confirmed it. If you can't run the tool or aren't sure it "
    "worked, say what you need or that you couldn't — never pretend it's done. "
    "Describing an action is not the same as doing it. "

    # Intent over literal words
    "The user speaks naturally — sometimes with filler words, imperfect phrasing, or "
    "speech recognition artifacts (e.g. 'klose' means 'close', 'spotify' misspelled). "
    "Always interpret intent rather than requiring exact or perfect wording."
)

THINKING_CONTEXT = (
    "THINKING PARTNER MODE: Do NOT give advice, solutions, or recommendations. "
    "Instead, briefly reflect back what the user just said (one sentence), "
    "then ask ONE focused Socratic follow-up question — for example: "
    "'What makes you say that?', 'What's the ideal outcome here?', "
    "'What's stopping you?', 'What would success look like?', "
    "'What have you actually tried so far?', 'What does your gut tell you?'. "
    "Keep the total response to 2-3 sentences. Never offer advice unless explicitly asked."
)

# ---------- fact extraction (background, never blocks Ted) ----------
def extract_and_save_facts(user_input, ted_reply):
    """Fire-and-forget background task: ask the fast LLM to extract structured
    facts from the exchange and save them to Neo4j. Never raises — skips silently
    on any network or parse error so it can't crash the conversation loop."""
    try:
        resp = groq_client.chat.completions.create(
            model=FAST_MODEL,   # fast model is plenty for JSON extraction
            messages=[
                {"role": "system", "content": (
                    f"Extract factual statements about the user ({OWNER_NAME}) from this exchange. "
                    "Only extract clear, explicit facts — not guesses or implications. "
                    "Return ONLY a JSON array of objects with keys: subject, relationship, object. "
                    "Use short uppercase relationship names like WORKS_AT, LIKES, OWNS, STUDIES, LIVES_IN, PREFERS. "
                    f"Example: [{{\"subject\": \"{OWNER_NAME}\", \"relationship\": \"LIKES\", \"object\": \"jazz\"}}]. "
                    "If there are no clear facts about the user, return an empty array []. "
                    "Never include Ted's statements about himself. Never include questions as facts."
                )},
                {"role": "user", "content": f"User said: {user_input}\nTed replied: {ted_reply}"}
            ],
            max_tokens=300,
            timeout=10.0,
        )
        raw = resp.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        facts = json.loads(raw)
        for f in facts:
            if all(k in f for k in ("subject", "relationship", "object")):
                save_fact(f["subject"], f["relationship"], f["object"])
                print(f"[memory] fact saved: {f['subject']} → {f['relationship']} → {f['object']}")
    except Exception as e:
        print(f"[memory] fact extraction skipped: {e}")

# ---------- web search ----------
_NEWSY_RE = re.compile(
    r"\b(?:news|game|games|match|score|won|win|lost|schedule|standings|playoffs"
    r"|world cup|election|happened|today|tonight|yesterday)\b", re.I)

def _newsy(query):
    """True when the query wants time-sensitive info — use the news vertical."""
    return bool(_NEWSY_RE.search(query))


def search_web(query):
    """Search the web via DuckDuckGo. Returns a sentinel string on failure —
    never blocks > 8s. This is the FALLBACK path; groq/compound (below) is the
    primary source for live answers. News-ish queries use the news vertical
    (dated, fresh results) instead of generic web text."""
    try:
        try:
            from ddgs import DDGS          # newer package name
        except ImportError:
            from duckduckgo_search import DDGS  # older package name
        year = date.today().year           # freshness bias in the query
        with DDGS(timeout=6) as ddgs:
            if _newsy(query):
                results = list(ddgs.news(query, max_results=6))
            else:
                results = list(ddgs.text(f"{query} {year}", max_results=6))
        if not results:
            return "__NO_RESULTS__"   # sentinel — caller can speak a fallback
        parts = []
        for r in results:
            body = r.get("body") or r.get("excerpt") or ""
            when = r.get("date", "")
            title = r.get("title", "")
            parts.append(f"[{when}] {title}: {body}" if when else f"{title}: {body}")
        return " ".join(parts)
    except Exception as e:
        print(f"[web] search failed: {e}")
        return "__SEARCH_ERROR__"


# ---------- live web answers via groq/compound ----------
_web_location = None   # cached "City, Region" for the web prompt

def _get_web_location():
    global _web_location
    if _web_location is None:
        _web_location = ""
        try:
            if features.HAS_ASSISTANT:
                loc = features.assistant.get_location()
                if loc:
                    _web_location = f"{loc['city']}, {loc['region']}"
        except Exception:
            pass
    return _web_location


def _web_messages(user_input, conversation=None):
    """Small message set for the compound model: short voice-style system
    prompt + up to 2 recent exchanges (for follow-ups like 'what about
    tomorrow?'). Kept lean — compound requests carry search results too."""
    today = date.today().strftime("%A, %B %d, %Y")
    loc = _get_web_location()
    sys_prompt = (
        f"You are Ted, {OWNER_NAME}'s voice assistant. Today is {today}."
        + (f" The user is in {loc}." if loc else "")
        + " Use your web search to answer with CURRENT information. "
        "Your reply is spoken aloud: two to three short sentences, no markdown, "
        "no lists, no URLs, no citations. Just the facts, conversational."
    )
    msgs = [{"role": "system", "content": sys_prompt}]
    if conversation:
        for m in conversation[-2:]:
            if m["role"] in ("user", "assistant"):
                msgs.append({"role": m["role"], "content": m["content"][:200]})
    msgs.append({"role": "user", "content": user_input})
    return msgs


def _compound_answer(user_input, conversation=None, timeout=14.0):
    """One-shot compound answer, or None on ANY failure so the caller falls
    back to DuckDuckGo. Deliberately NOT streamed: on the free tier compound
    can abort mid-stream (413 when its search results blow the request cap),
    and a non-streaming call either fully succeeds or cleanly fails.
    Kept small (short history, modest max_tokens) to stay under tier limits.
    413s (request_too_large) depend on what compound's own search fetched, so
    one retry often succeeds where the first attempt blew the cap."""
    for attempt in (0, 1):
        try:
            r = groq_client.chat.completions.create(
                model=WEB_MODEL,
                messages=_web_messages(user_input, conversation),
                max_tokens=220,
                timeout=timeout,
            )
            out = (r.choices[0].message.content or "").strip()
            return out or None
        except Exception as e:
            msg = str(e)
            if "request_too_large" in msg and attempt == 0:
                print("[web] compound 413 (its search results too big) — one retry")
                continue
            print(f"[web] compound failed ({msg[:100]}) — DuckDuckGo fallback")
            return None
    return None


def web_answer(question):
    """Live-web answer for explicit 'look up X' commands.
    compound first, DuckDuckGo+summarise fallback, honest failure last."""
    out = _compound_answer(question)
    if out:
        return out
    raw = search_web(question)
    if raw in ("__NO_RESULTS__", "__SEARCH_ERROR__"):
        return "I couldn't find anything solid on that right now."
    today = date.today().strftime("%A, %B %d, %Y")
    try:
        r = chat_create(
            messages=[{"role": "user", "content":
                       f"Today is {today}. Using ONLY these web search snippets, answer "
                       f"the question for a voice assistant in one or two short spoken "
                       f"sentences. If they don't fully answer it, give the closest "
                       f"useful information they contain — never mention searching, "
                       f"snippets, or being unable.\n\nQuestion: {question}\n\n"
                       f"Snippets:\n{raw[:4000]}"}],
            max_tokens=140, timeout=10.0,
        )
        return (r.choices[0].message.content or "").strip() or raw[:250]
    except Exception:
        return raw[:250]


def _remember_exchange(user_input, full_reply, conversation):
    """Append a finished exchange to the conversation and kick off the
    background memory/fact writes. Shared by the chat and web paths."""
    _clean = re.sub(r"[\s.,!?;:'\"-]+", "", full_reply)
    if not (full_reply.strip() and _clean):
        return
    conversation.append({"role": "user",      "content": user_input})
    conversation.append({"role": "assistant", "content": full_reply})
    if len(conversation) > MAX_CONV_MESSAGES + 1:
        del conversation[1:len(conversation) - MAX_CONV_MESSAGES]
    threading.Thread(target=save_memory,
                     args=(user_input, full_reply), daemon=True).start()
    if intents._worth_extracting(user_input):
        threading.Thread(target=extract_and_save_facts,
                         args=(user_input, full_reply), daemon=True).start()

# ---------- ask Claude (second brain) ----------
def ask_claude(prompt):
    """Relay a hard question to Claude via the Anthropic API. No SDK needed —
    plain HTTPS. Returns a spoken-style answer or a friendly fallback."""
    if not ANTHROPIC_API_KEY:
        return "I'd need an Anthropic API key set in the config before I can ask Claude."
    import urllib.request
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 400,
        "system": ("You are Claude, answering a question relayed by Ted, a voice "
                   "assistant, so your reply will be read aloud. Be conversational and "
                   "concise — usually one to four sentences, no markdown or lists."),
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json",
                 "x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = " ".join(b.get("text", "") for b in data.get("content", [])
                        if b.get("type") == "text").strip()
        return text or "Claude didn't have anything to add."
    except Exception as e:
        print("Claude error:", e)
        return "I couldn't reach Claude just now."

# ---------- streaming conversation ----------
def ask_streaming(user_input, conversation, frustrated=False, thinking_mode=False, window=None):
    """Yield LLM reply text chunks from Groq (streaming).

    frustrated      — True → append a tone-adjustment note so Ted is more direct.
    thinking_mode   — True → inject Socratic partner instructions (no advice).

    Mutates `conversation` in-place so the history accumulates.
    Saves the final reply to Neo4j memory and logs facts, both on background threads.
    """
    global _GROQ_OK
    action = detect_action(user_input)
    if action:
        yield action
        return

    today = date.today().strftime("%B %d, %Y")

    # --- live-web questions: groq/compound answers with a real web search ---
    search_results = ""
    _web_error_msg = None
    if intents._needs_web(user_input):
        ans = _compound_answer(user_input, conversation)
        if ans:
            yield ans
            _remember_exchange(user_input, ans, conversation)
            return

        # compound unavailable (rate limit etc.) — fall back to DuckDuckGo
        raw = search_web(user_input)
        if raw == "__NO_RESULTS__":
            _web_error_msg = "I couldn't find anything on that."
        elif raw == "__SEARCH_ERROR__":
            _web_error_msg = "I couldn't reach the web right now."
        else:
            search_results = raw

    # If web search completely failed, short-circuit rather than hallucinating
    if _web_error_msg and not search_results:
        yield _web_error_msg
        return

    # --- memory retrieval (run concurrently — independent network/DB round-trips,
    #     so doing them in parallel instead of back-to-back cuts pre-reply latency) ---
    _ctx = {"mem": "", "facts": "", "know": ""}
    def _load_mem():   _ctx["mem"]   = get_memory(user_input)
    def _load_facts(): _ctx["facts"] = get_facts_about(OWNER_NAME)
    def _load_know():
        if features.HAS_KNOWLEDGE:
            _ctx["know"] = features.knowledge.search(user_input, k=3)
    _lk_threads = [threading.Thread(target=f, daemon=True)
                   for f in (_load_mem, _load_facts, _load_know)]
    for _t in _lk_threads: _t.start()
    for _t in _lk_threads: _t.join(timeout=4.0)
    past_memory   = _ctx["mem"]
    known_facts   = _ctx["facts"]
    knowledge_ctx = _ctx["know"]

    # The user turn is appended to `conversation` only after a successful reply
    # (see the streaming finally below) so failed calls don't leave a dangling
    # user message with no assistant response.

    # Build the context string injected as a system message just before recent history.
    # Keeping it as a single message (rather than modifying the system prompt) means
    # per-turn data (today's date, web results) stays out of the prompt cache.
    context_parts = [f"Today is {today}."]
    context_parts.append(f"Relevant web results: {search_results or 'none'}.")
    context_parts.append(f"Known facts about {OWNER_NAME}: {known_facts or 'none'}.")
    context_parts.append(f"Relevant past exchanges: {past_memory or 'none'}.")
    context_parts.append(f"Personal knowledge base: {knowledge_ctx or 'none'}.")

    # Memory continuity nudge
    context_parts.append(
        "If something from earlier in this conversation is relevant to the current message, "
        "reference it naturally — 'like you mentioned', 'you said earlier' — "
        "but only when it genuinely fits."
    )

    # Tone adjustment when frustration has been detected
    if frustrated:
        context_parts.append(
            "The user seems frustrated. Drop any cheerful energy. Be direct, "
            "get to the point faster, and skip filler phrases."
        )

    # Thinking partner: override the persona to Socratic mode (no advice)
    if thinking_mode:
        context_parts.append(THINKING_CONTEXT)

    context = "(Context: " + " ".join(context_parts) + ")"

    # Trim conversation to MAX_HISTORY recent turns; always keep [0] (system prompt).
    # The per-turn context block is inserted at position [1] so Groq sees:
    #   [system_prompt, context, ...recent turns...]
    recent   = conversation[1:][-MAX_HISTORY:]
    messages = ([conversation[0], {"role": "system", "content": context}]
                + recent + [{"role": "user", "content": user_input}])

    def _do_groq_call():
        """Inner helper so the retry logic below can call the same request.
        chat_create handles the gpt-oss → llama fallback internally."""
        return chat_create(
            messages=messages,
            max_tokens=250,
            stream=True,
            timeout=12.0,
        )

    # Auto-retry on rate limits (up to 3×) and transient timeouts (up to 2×).
    # Any other API error is logged and surfaced as a spoken fallback.
    import groq as _groq_mod
    resp = None
    try:
        resp = _do_groq_call()
    except _groq_mod.RateLimitError:
        print("[groq] rate limited — waiting 3s then retrying")
        time.sleep(3)
        try:
            resp = _do_groq_call()
        except _groq_mod.RateLimitError:
            print("[groq] still rate limited — waiting 5s")
            time.sleep(5)
            try:
                resp = _do_groq_call()
            except Exception as e:
                print(f"[groq] rate limit retry failed: {e}")
                _GROQ_OK = False
                if window: show_issue(window, "Groq is rate-limiting Ted right now — give it a moment.")
                yield "I'm being throttled right now — give me a moment and try again."
                return
    except (_groq_mod.APITimeoutError, Exception) as e:
        if "timeout" in str(e).lower() or isinstance(e, _groq_mod.APITimeoutError):
            print(f"[groq] timeout on first attempt — retrying: {e}")
            time.sleep(1)
            try:
                resp = _do_groq_call()
            except Exception as e2:
                print(f"[groq] timeout retry failed: {e2}")
                _GROQ_OK = False
                if window: show_issue(window, "Ted can't reach Groq (timeout) — check your connection.")
                yield "I can't reach my brain right now — try again in a moment."
                return
        else:
            print(f"[groq] API error: {e}")
            error_log.error(f"Groq API error: {e}\n{traceback.format_exc()}")
            _GROQ_OK = False
            if window: show_issue(window, "Groq API error — Ted couldn't generate a reply.")
            yield "I ran into an issue — give me a second and try again."
            return

    _GROQ_OK = True   # we have a response object — Groq is reachable
    full_reply = ""
    try:
        for chunk in resp:
            delta = chunk.choices[0].delta.content or ""
            if delta:                # skip empty keep-alive chunks
                full_reply += delta
                yield delta
    except Exception as e:
        print(f"[groq] stream error mid-response: {e}")
        if full_reply.strip():
            pass   # already yielded some content — let finally save what we have
        else:
            yield "Something cut out — ask me again."
    finally:
        _remember_exchange(user_input, full_reply, conversation)
        try:
            resp.close()
        except Exception:
            pass

# ---------- composition helpers (messages / email) ----------
def generate_message_text(instruction, contact):
    """Use a quick Groq call to turn a spoken instruction into an actual message text."""
    try:
        resp = chat_create(
            messages=[
                {"role": "system", "content":
                 "Convert the following spoken instruction into a short, natural, casual "
                 "text message (1–2 sentences). Return ONLY the message text — no quotes, "
                 "no preamble, no commentary."},
                {"role": "user", "content":
                 f"Recipient: {contact}\nInstruction: {instruction}"},
            ],
            max_tokens=80,
            timeout=8.0,
        )
        return resp.choices[0].message.content.strip().strip("\"'")
    except Exception:
        return instruction   # fallback: send the raw instruction


def generate_message_with_style(instruction, contact_name, style):
    """LLM-compose an iMessage given a plain-language instruction and style."""
    prompt = (
        f"Write a text message to {contact_name}. "
        f"The message should: {instruction}. "
        f"Tone/style: {style}. "
        "Reply with ONLY the message text — no quotes, no preamble, no sign-off."
    )
    try:
        resp = chat_create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.75,
        )
        return resp.choices[0].message.content.strip().strip('"').strip("'")
    except Exception:
        return f"Hey, just checking — {instruction}"


def generate_email_body(instruction, to_address, subject, style):
    """LLM-compose a full email body."""
    prompt = (
        f"Write a professional email to {to_address} with subject '{subject}'. "
        f"The email should: {instruction}. "
        f"Tone/style: {style or 'professional and clear'}. "
        "Reply with ONLY the email body text — no subject line, no 'Dear X', just the body and a short sign-off."
    )
    try:
        resp = chat_create(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.6,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return instruction


def summarize_email_body(body, contact_name):
    """Summarize email content via LLM."""
    if len(body.split()) < 40:
        return body
    try:
        resp = groq_client.chat.completions.create(
            model=FAST_MODEL,
            messages=[{
                "role": "user",
                "content": f"Summarize this email from {contact_name} in 2 sentences max, spoken aloud style:\n\n{body[:2000]}"
            }],
            max_tokens=80,
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return body[:300]


def generate_session_summary(conversation):
    """Use the fast LLM to write a 2-3 sentence summary of the current session.
    Returns the summary string, or None on failure."""
    conv = conversation[1:]               # skip system prompt
    if len(conv) < 4:                     # need at least 2 exchanges
        return None
    recent = conv[-14:]
    transcript = "\n".join(
        f"{'User' if m['role']=='user' else 'Ted'}: {m['content'][:200]}"
        for m in recent
    )
    try:
        resp = groq_client.chat.completions.create(
            model=FAST_MODEL,
            messages=[
                {"role": "system", "content": (
                    "Write a 2-3 sentence plain-text summary of this conversation. "
                    "State what topics were discussed. No bullet points, no headers. "
                    "Start with what was talked about, not 'The conversation was about'."
                )},
                {"role": "user", "content": transcript},
            ],
            max_tokens=150,
            temperature=0.3,
            timeout=10.0,
        )
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:
        print(f"[session] summary generation failed: {e}")
        return None
