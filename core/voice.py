"""core/voice.py — Ted's ears and mouth.

Owns the audio engine (native AEC or sounddevice fallback), the TTS engines
(Kokoro local / ElevenLabs cloud), speech-text cleanup, sentence-streamed
playback, and microphone capture + transcription filtering.

Importing this module loads the models and starts the audio engine — it is
the runtime, not a library of pure helpers (those live in core/intents.py).
"""

import os
import re
import subprocess
import threading
import time
import warnings

import numpy as np
import soundfile as sf

from core.audio import AudioEngine, SAMPLE_RATE
from core.hud_bridge import js, set_state, amp_cb
from core.paths import DATA, INPUT_FILE

try:
    from config import ELEVENLABS_API_KEY, ELEVEN_LABS_VOICE_ID
except Exception:
    ELEVENLABS_API_KEY = ""
    ELEVEN_LABS_VOICE_ID = ""
try:
    from config import USE_ELEVENLABS
except Exception:
    USE_ELEVENLABS = False
try:
    from config import USE_GROQ_STT
except Exception:
    USE_GROQ_STT = True
try:
    from config import FALLBACK_VOICE_BARGEIN
except Exception:
    FALLBACK_VOICE_BARGEIN = True
try:
    from config import OWNER_NAME
except Exception:
    OWNER_NAME = "Charlie"
try:
    from config import VOICE_LOCK
except Exception:
    VOICE_LOCK = False
try:
    from config import VOICE_LOCK_THRESHOLD
except Exception:
    VOICE_LOCK_THRESHOLD = 0.68

try:
    from elevenlabs.client import ElevenLabs as _ElevenLabsClient
    from elevenlabs import VoiceSettings as _VoiceSettings
    _HAS_ELEVENLABS = True
except ImportError:
    _HAS_ELEVENLABS = False

# ---------- settings ----------
VOICE = "am_michael"
SPEED = 1.1   # 1.2 felt rushed; 1.1 is calmer and clearer

# ── whisper detection ──
WHISPER_RMS_THRESHOLD = 0.018   # RMS below this = user is speaking quietly

# ---------- transcription noise filtering (tunable) ----------
# Whisper invents stock phrases when fed silence. We reject clips that are too
# short or too quiet to be speech, then reject low-confidence results, then drop
# the handful of phrases Whisper famously hallucinates. Loosen these if Ted ever
# ignores real (quiet) speech.
MIN_CAPTURE_SEC = 0.25     # ignore clips shorter than this
MIN_CAPTURE_RMS = 0.011    # ignore clips quieter than this
NO_SPEECH_MAX   = 0.6      # reject if Whisper is this sure it's not speech
LOGPROB_MIN     = -1.0     # reject very low-confidence transcriptions
_HALLUCINATIONS = {
    "thank you", "thank you.", "thank you so much", "thank you very much",
    "thanks", "thanks.", "thanks for watching", "thanks for watching!",
    "you", "you.", "bye", "bye.", "bye bye", "please subscribe",
    "i'm sorry", "see you next time", ".", "..", "...",
}

def _looks_hallucinated(text):
    return text.strip().lower() in _HALLUCINATIONS

# ---------- short-fragment guard ----------
# A cough, a chair creak or a door closing is loud enough to pass the energy gate
# and confident enough to pass the logprob gate, and Whisper renders it as a
# short plausible word ("Tep.", "Start.", "Hmm."). Those then got acted on as
# commands. But plenty of REAL commands are one or two words, so length alone
# can't decide — anything short has to match something Ted actually does.
_SHORT_OK = {
    # transport / playback
    "stop", "pause", "play", "resume", "next", "skip", "back", "previous",
    "louder", "quieter", "mute", "unmute", "shuffle", "repeat",
    # confirmations
    "yes", "yeah", "yep", "no", "nope", "okay", "ok", "sure", "cancel",
    "nevermind", "never mind", "done", "go", "wait", "stop it", "shut up",
    # address / attention
    "ted", "hey ted", "hi ted", "hello", "hi", "hey",
    # common one-word asks
    "weather", "time", "help", "again", "repeat that", "continue", "listen",
    "sleep", "wake up", "mute yourself", "what", "why", "how", "more",
}
MIN_WORDS_UNLESS_KNOWN = 3   # under this, the phrase must be in _SHORT_OK


def _is_junk_fragment(text):
    """True when a short transcription isn't a command Ted recognises.

    Only applies to fragments under MIN_WORDS_UNLESS_KNOWN words — longer
    utterances are handled by the ambient-speech guards further down.
    """
    cleaned = text.strip().lower().strip(".,!?;:'\"")
    if not cleaned:
        return True
    words = cleaned.split()
    if len(words) >= MIN_WORDS_UNLESS_KNOWN:
        return False
    if cleaned in _SHORT_OK:
        return False
    # Allow short phrases built entirely from known command words ("play next").
    if all(w in _SHORT_OK for w in words):
        return False
    return True

# Whisper prints a harmless FP16 warning on CPU — silence it.
warnings.filterwarnings("ignore", message="FP16 is not supported on CPU")

# ---------- load models once at import ----------

# STT — Groq Whisper (cloud) is the default: faster than local small.en and far
# more accurate. Set USE_GROQ_STT = False in config.py for the local fallback.
whisper_model = None
if not USE_GROQ_STT:
    import whisper
    print("Loading Whisper (local)…")
    whisper_model = whisper.load_model("small.en")
    print("Whisper loaded.")
else:
    print("STT: Groq Whisper cloud — local model will load only if needed")


def _get_local_whisper():
    """Lazy-load local Whisper, including when cloud STT fails mid-session."""
    global whisper_model
    if whisper_model is None:
        import whisper
        print("Loading Whisper (local fallback)…")
        whisper_model = whisper.load_model("small.en")
    return whisper_model


def _transcribe_local(path):
    result = _get_local_whisper().transcribe(
        path,
        language="en",
        fp16=False,
        condition_on_previous_text=False,
        no_speech_threshold=NO_SPEECH_MAX,
        logprob_threshold=LOGPROB_MIN,
        temperature=0,
    )
    text = (result.get("text") or "").strip()
    if not text:
        return None
    segs = result.get("segments", []) or []
    if segs:
        ns = sum(s.get("no_speech_prob", 0.0) for s in segs) / len(segs)
        lp = sum(s.get("avg_logprob", 0.0) for s in segs) / len(segs)
        if ns > NO_SPEECH_MAX and lp < -0.4:
            print(f"   (ignored — likely silence: {text!r})")
            return None
    return text

# TTS — ElevenLabs (cloud) if configured, otherwise Kokoro (local).
_elevenlabs_client = None
kokoro = None
if _HAS_ELEVENLABS and ELEVENLABS_API_KEY and USE_ELEVENLABS:
    _elevenlabs_client = _ElevenLabsClient(api_key=ELEVENLABS_API_KEY)
    print("TTS: ElevenLabs (cloud)")
else:
    from kokoro_onnx import Kokoro
    print("Loading Kokoro…")
    kokoro = Kokoro(
        os.path.join(DATA, "kokoro-v1.0.onnx"),
        os.path.join(DATA, "voices-v1.0.bin"),
    )
    print("TTS: Kokoro (local)")

def voice_label():
    """Human-readable name of the active TTS engine (shown on the HUD)."""
    return ("ElevenLabs · flash v2.5" if _elevenlabs_client is not None
            else f"Kokoro · {VOICE}")

# ---------- audio engine ----------
engine = AudioEngine(
    output_sr=24000 if _elevenlabs_client is not None else SAMPLE_RATE,
    fallback_bargein=FALLBACK_VOICE_BARGEIN,
)
_mode = engine.start()
if _mode == "aec":
    print("🎧 Audio: native engine with echo cancellation — voice barge-in ON.")
else:
    print("🔉 Audio: sounddevice fallback (no echo cancellation). Build native/ted_audio "
          "to enable talking over Ted.")
print("Calibrating microphone — stay quiet for a second…")
try:
    _thr = engine.calibrate()
    print(f"Mic calibrated (silence threshold ≈ {_thr:.4f})")
except Exception as e:
    print("Mic calibration skipped:", e)

# ── session-level synth overrides ──
# Set per-turn by the conversation loop; read by synth(). No threading concern
# because TedApi._busy ensures only one response is active at a time.
_active_volume = 1.0   # 0.5 when whispering

def set_active_volume(v):
    global _active_volume
    _active_volume = v

# module-level capture-RMS tracker — set by capture(); read in conversation_loop
_last_capture_rms = 0.0

def last_capture_rms():
    return _last_capture_rms


def adjust_speed(delta):
    """Nudge Ted's speaking rate at runtime (voice 'slow down' / 'speed up')."""
    global SPEED
    SPEED = max(0.8, min(1.5, round(SPEED + delta, 2)))
    return SPEED

# ---------- clean text before it goes to the voice ----------
# Kokoro reads what you hand it literally, so strip anything that would be
# spoken as a symbol, and apply simple respellings for tricky names.
#
# PRONUNCIATION: add words Ted mangles. Write them how they SHOULD sound —
# kokoro pronounces the respelling, e.g. "Sortly" -> "Sort-lee".
PRONUNCIATION = {
    # "Sortly":   "Sort-lee",
}
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)
_MAX_SPEECH_CHARS = 600   # truncate TTS input beyond this to avoid 45-second monologues

# Filler openers the LLM produces compulsively — strip before speaking
_FILLER_RE = re.compile(
    r"^(yeah|yep|yup|got it|sure thing|sure|of course|absolutely|certainly|great|"
    r"sounds good|no problem|happy to(?: help)?|i'?d be happy to|right away|"
    r"i can do that|i'?ll get on that|let me|one sec|one moment|"
    r"understood|noted|okay,?\s+so|alright,?\s+so|of course,?\s+)"
    r"[,!.]?\s*",
    re.IGNORECASE,
)

def _strip_filler(text):
    """Remove compulsive LLM opener phrases that kill the Jarvis feel."""
    return _FILLER_RE.sub("", text).strip()


def _strip_opener(text):
    """Strip leading filler/sycophantic opener from the first sentence."""
    stripped = _strip_filler(text)
    if stripped:
        return stripped[0].upper() + stripped[1:] if len(stripped) > 1 else stripped.upper()
    return text

# ---------- contractions enforcer ----------
_CONTRACTIONS = [
    (re.compile(r"\bI will\b",    re.I), "I'll"),
    (re.compile(r"\bI am\b",      re.I), "I'm"),
    (re.compile(r"\bI have\b",    re.I), "I've"),
    (re.compile(r"\bI would\b",   re.I), "I'd"),
    (re.compile(r"\bdo not\b",    re.I), "don't"),
    (re.compile(r"\bdoes not\b",  re.I), "doesn't"),
    (re.compile(r"\bdid not\b",   re.I), "didn't"),
    (re.compile(r"\bcannot\b",    re.I), "can't"),
    (re.compile(r"\bcan not\b",   re.I), "can't"),
    (re.compile(r"\bwill not\b",  re.I), "won't"),
    (re.compile(r"\bwould not\b", re.I), "wouldn't"),
    (re.compile(r"\bit is\b",     re.I), "it's"),
    (re.compile(r"\bthat is\b",   re.I), "that's"),
    (re.compile(r"\bthere is\b",  re.I), "there's"),
    (re.compile(r"\bhe is\b",     re.I), "he's"),
    (re.compile(r"\bshe is\b",    re.I), "she's"),
    (re.compile(r"\bthey are\b",  re.I), "they're"),
    (re.compile(r"\bwe are\b",    re.I), "we're"),
    (re.compile(r"\byou are\b",   re.I), "you're"),
]

def _enforce_contractions(text):
    for pat, repl in _CONTRACTIONS:
        text = pat.sub(repl, text)
    return text


def _clean_for_speech(text):
    """Sanitise text for the TTS engine: strip markdown/emoji, apply contractions
    and pronunciation overrides, and truncate overlong responses gracefully."""
    t = _EMOJI_RE.sub("", text)
    t = re.sub(r"https?://\S+|\bwww\.\S+", "", t)   # never read URLs aloud
    t = re.sub(r"\[(\d+|[^\]]{1,30})\]", "", t)      # web citations like [1]
    t = re.sub(r"[*_`#>~|]+", "", t)        # markdown / stray symbols
    t = re.sub(r"\s+", " ", t).strip()
    t = _enforce_contractions(t)             # natural contractions before TTS
    for word, say in PRONUNCIATION.items():
        t = re.sub(rf"\b{re.escape(word)}\b", say, t, flags=re.IGNORECASE)
    # Truncate very long responses at the last sentence boundary before the cap,
    # so we don't cut a word mid-flow. Fall back to a hard cut if no "." exists.
    if len(t) > _MAX_SPEECH_CHARS:
        cut = t.rfind(".", 0, _MAX_SPEECH_CHARS)
        if cut < _MAX_SPEECH_CHARS * 0.5:   # no sentence break found — hard cut
            cut = _MAX_SPEECH_CHARS
        t = t[:cut + 1].rstrip() + " ...want me to continue?"
    return t

# ---------- synthesis ----------
def synth(text, speed=None, volume=None):
    """Synthesise text. ElevenLabs (cloud) if configured, Kokoro (local) otherwise.
    speed:  explicit rate override; None → use global SPEED.
    volume: explicit scale [0.0–1.0]; None → use module-level _active_volume.
    """
    text = _clean_for_speech(text)
    if not text:
        return np.zeros(1, dtype=np.float32), SAMPLE_RATE
    spd = speed if speed is not None else SPEED

    if _elevenlabs_client is not None:
        try:
            # ElevenLabs Flash v2.5 — ~150ms latency, natural prosody
            el_speed = max(0.7, min(1.2, spd))   # ElevenLabs speed range 0.7–1.2
            vs = _VoiceSettings(stability=0.45, similarity_boost=0.75, speed=el_speed)
            audio_gen = _elevenlabs_client.text_to_speech.convert(
                voice_id=ELEVEN_LABS_VOICE_ID,
                text=text,
                model_id="eleven_flash_v2_5",
                output_format="pcm_24000",
                voice_settings=vs,
            )
            # Collect audio on a thread with a hard 15s timeout so a stalled
            # ElevenLabs connection never holds the busy lock indefinitely.
            _el_result = [None]
            _el_exc = [None]
            def _collect():
                try:
                    _el_result[0] = b"".join(audio_gen)
                except Exception as _e:
                    _el_exc[0] = _e
            _t = threading.Thread(target=_collect, daemon=True)
            _t.start()
            _t.join(timeout=15.0)
            if _t.is_alive() or _el_result[0] is None:
                raise TimeoutError("ElevenLabs TTS timed out after 15s")
            if _el_exc[0]:
                raise _el_exc[0]
            audio_bytes = _el_result[0]
            samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            sr = 24000
        except Exception as e:
            print(f"[synth] ElevenLabs error — silent: {e}")
            return np.zeros(1, dtype=np.float32), SAMPLE_RATE
    else:
        # Kokoro local fallback
        samples, sr = kokoro.create(text, voice=VOICE, speed=spd, lang="en-us")
        samples = np.asarray(samples, dtype=np.float32)

    vol = volume if volume is not None else _active_volume
    if vol != 1.0:
        samples = np.clip(samples * vol, -1.0, 1.0)
    return samples, sr

# ---------- sentence segmentation for streamed speech ----------
_SENTENCE_END = (".", "!", "?", "…")

# Common abbreviations that end in "." but should NOT trigger a sentence break.
_ABBREVS = {
    "mr", "mrs", "ms", "dr", "prof", "vs", "st", "ave", "blvd",
    "jr", "sr", "inc", "ltd", "etc", "approx", "est", "vol", "dept",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
}

def _find_sentence_break(s):
    """Return the index of the first real sentence-ending character in `s`, or -1.
    A period is only counted as a sentence break if it's NOT an abbreviation
    (e.g. 'Mr. Smith' or 'Feb. 14' should never split mid-name)."""
    for i, ch in enumerate(s):
        if ch in _SENTENCE_END:
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if nxt in ("", " ", "\n", "\"", "'", ")"):
                if ch == ".":
                    # Check whether the word before the period is a known abbreviation
                    parts = s[:i].rsplit(None, 1)
                    if parts:
                        w = parts[-1].lower().rstrip(".")
                        if len(w) <= 2 or w in _ABBREVS:  # e.g. "dr", "feb"
                            continue
                return i
    return -1

# Minimum words to synthesize as one chunk. If the first complete sentence is
# shorter than this, we bundle it with the next sentence before calling Kokoro.
# Larger chunks → more prosodic context → more natural delivery.
_MIN_SYNTH_WORDS = 8   # lower = faster first audio

# ---------- speaking ----------
def speak(window, text, api):
    """Speak a fixed string. Silent no-op while muted (chat-first mode) —
    callers display their own text via add_message, so nothing is lost."""
    if getattr(api, "muted", False):
        return
    set_state(window, "speaking")
    samples, sr = synth(text)
    engine.reset_barge_in()   # play_samples no longer resets — arm fresh per utterance
    engine.play_samples(samples, sr, on_amplitude=amp_cb(window),
                        should_stop=lambda: api.interrupt_speech)
    js(window, "tedHud.clearAmplitude()")
    api.interrupt_speech = False
    set_state(window, "idle")


def speak_streaming(window, text_gen, api, speed=None, volume=None):
    """Consume a streaming LLM token generator and speak it sentence-by-sentence.

    Sentences are played as soon as they are complete — the user hears the first
    sentence after ~150 ms rather than waiting for the full reply. Short sentences
    (< _MIN_SYNTH_WORDS words) are bundled with the next one before synthesis so
    Kokoro gets enough prosodic context to sound natural.

    Returns (full_text: str, barged_by_voice: bool). barged_by_voice=True means
    the user spoke over Ted; the caller should set prearmed=True and skip the
    inter-turn pause so their follow-up is captured immediately.
    """
    import json as _json

    # Muted = pure chat mode: stream the text to the HUD with no synthesis.
    # Same return contract as the spoken path so callers don't care.
    if getattr(api, "muted", False):
        set_state(window, "thinking")
        full, pend = "", ""
        for chunk in text_gen:
            if api.interrupt_speech:
                break
            full += chunk
            pend += chunk
            if len(pend) >= 24:      # batch tokens — one JS call per ~2 dozen chars
                js(window, f"tedHud.streamTedText({_json.dumps(pend)})")
                pend = ""
        if pend:
            js(window, f"tedHud.streamTedText({_json.dumps(pend)})")
        js(window, "tedHud.endTedReply()")
        api.interrupt_speech = False
        set_state(window, "idle")
        return full, False

    set_state(window, "speaking")
    amp  = amp_cb(window)
    stop = lambda: api.interrupt_speech
    buffer           = ""
    full             = ""
    interrupted      = False
    first_sentence   = True   # opener stripping applies only to the first sentence

    def say(piece, strip_opener=False):
        if not piece.strip():
            return True
        if strip_opener:
            piece = _strip_opener(piece)
        if not piece.strip():
            return True
        # Read-along: show each sentence on the HUD exactly as it begins speaking.
        js(window, f"tedHud.streamTedText({_json.dumps(piece)})")
        samples, sr = synth(piece, speed=speed, volume=volume)
        return engine.play_samples(samples, sr, on_amplitude=amp, should_stop=stop)

    # Arm barge-in once for the WHOLE reply. play_samples no longer resets it,
    # so a voice interruption during the synth gap between sentences is kept,
    # and set_in_reply keeps detection live through those gaps.
    engine.reset_barge_in()
    engine.set_in_reply(True)
    try:
        for chunk in text_gen:
            if api.interrupt_speech or engine.barge_in:
                interrupted = True
                break
            buffer += chunk   # accumulate tokens until we have a complete sentence
            full   += chunk
            while True:
                cut = _find_sentence_break(buffer)
                if cut == -1:
                    break   # no complete sentence yet — keep accumulating
                synth_chunk = buffer[:cut + 1]
                # If this sentence is very short, merge it with the next before synthesis
                # so Kokoro has enough context for natural prosody ("Sure." sounds choppy alone).
                if len(synth_chunk.split()) < _MIN_SYNTH_WORDS:
                    rest = buffer[cut + 1:]
                    cut2 = _find_sentence_break(rest)
                    if cut2 != -1:
                        synth_chunk = buffer[:cut + 1 + cut2 + 1]   # merged pair
                        buffer = buffer[cut + 1 + cut2 + 1:]
                    else:
                        break  # second sentence hasn't arrived yet — wait
                else:
                    buffer = buffer[cut + 1:]
                if not say(synth_chunk, strip_opener=first_sentence):
                    interrupted = True
                    break
                first_sentence = False
            if interrupted:
                break

        if not interrupted and buffer.strip():
            if not say(buffer, strip_opener=first_sentence):
                interrupted = True
    finally:
        engine.set_in_reply(False)   # leaves barge_in itself intact for the read below

    js(window, "tedHud.clearAmplitude()")
    js(window, "tedHud.endTedReply()")
    barged_by_voice = engine.barge_in and not api.interrupt_speech
    api.interrupt_speech = False
    set_state(window, "idle")
    return full, barged_by_voice

# ---------- capture (mic → text) ----------
def capture(prearmed=False):
    """Record one spoken turn and return its transcription, or None if nothing usable.

    Multi-stage rejection pipeline:
      1. Energy gate  — drop clips too short or too quiet to be real speech.
      2. Confidence   — drop Whisper output that fails no_speech_prob / logprob thresholds.
      3. Blocklist    — drop phantom phrases Whisper produces on near-silence.
      4/5. Ambient guards — drop background TV/music transcriptions.
    """
    global _last_capture_rms
    audio = engine.capture_turn(prearmed=prearmed)  # blocks until speech ends
    if audio is None:
        return None

    # Gate 1: energy — skip clips too short or too quiet to be real speech
    dur = len(audio) / SAMPLE_RATE
    rms = float(np.sqrt(np.mean(np.square(audio)))) if len(audio) else 0.0
    _last_capture_rms = rms   # stored for whisper-mode volume scaling
    if dur < MIN_CAPTURE_SEC or rms < MIN_CAPTURE_RMS:
        return None

    # Gate 1.5: voice lock — when enabled and a profile is enrolled, only the
    # owner's voice gets through (None = can't verify → let it through).
    if VOICE_LOCK:
        from core import speaker
        if speaker.verify(audio, threshold=VOICE_LOCK_THRESHOLD) is False:
            print(f"   (ignored — voice lock: not {OWNER_NAME})")
            return None

    sf.write(INPUT_FILE, audio, SAMPLE_RATE)

    if USE_GROQ_STT:
        # 2) Cloud STT — Groq Whisper large-v3-turbo
        from core.llm import groq_client
        try:
            with open(INPUT_FILE, "rb") as f:
                result = groq_client.audio.transcriptions.create(
                    file=("input.wav", f, "audio/wav"),
                    model="whisper-large-v3-turbo",
                    response_format="verbose_json",
                    language="en",
                    temperature=0,
                    # Keep this minimal: a long command-word prompt biases Whisper
                    # into EMITTING those words on ambiguous audio (phantom commands).
                    # The name alone fixes "Ted"/"Todd"/"Ed" mishearings without that.
                    prompt=f"{OWNER_NAME}, Ted.",
                )
            text = (getattr(result, "text", None) or "").strip()
            if not text:
                return None
            # Confidence gate from segment metadata (same thresholds as local)
            segs = getattr(result, "segments", None) or []
            if segs:
                ns = sum(getattr(s, "no_speech_prob", 0.0) for s in segs) / len(segs)
                lp = sum(getattr(s, "avg_logprob", 0.0) for s in segs) / len(segs)
                if ns > NO_SPEECH_MAX and lp < -0.4:
                    print(f"   (ignored — likely silence: {text!r})")
                    return None
                # Standalone logprob gate: very low confidence even without high
                # no_speech_prob means the transcription is uncertain (ambient noise).
                if lp < LOGPROB_MIN:
                    print(f"   (ignored — low confidence logprob {lp:.2f}: {text!r})")
                    return None
        except Exception as e:
            print(f"[capture] Groq STT unavailable — using local Whisper: {e}")
            try:
                text = _transcribe_local(INPUT_FILE)
                if not text:
                    return None
            except Exception as local_error:
                print(f"[capture] local Whisper fallback failed: {local_error}")
                return None
    else:
        # 2) Local Whisper
        text = _transcribe_local(INPUT_FILE)
        if not text:
            return None

    # 3) blocklist — phantom phrases both Whisper variants hallucinate on silence
    if _looks_hallucinated(text):
        print(f"   (ignored — hallucination: {text!r})")
        return None

    # 3.5) short-fragment guard — a cough or bump transcribes as a short plausible
    #      word ("Tep.", "Start.") that clears every gate above and then gets run
    #      as a command. Anything under 3 words now has to be a real command.
    if _is_junk_fragment(text):
        print(f"   (ignored — short fragment: {text!r})")
        return None

    # 4) ambient-speech guard — background TV/conversation produces plausible English
    #    sentences that don't address Ted. Real commands almost never start with
    #    connective or referential words like "and", "it", "they", "one", "so", etc.
    _words = text.split()
    _first_w = _words[0].lower().rstrip(".,!?") if _words else ""
    _AMBIENT_STARTERS = {
        "and", "but", "or", "so", "yet", "nor",          # conjunctions
        "it", "its", "this", "that", "these", "those",    # demonstratives
        "he", "she", "they", "we", "one", "their", "our", # third-person
        "in", "on", "at", "of", "there",                  # prepositions / expletives
    }
    if len(_words) > 10 and _first_w in _AMBIENT_STARTERS:
        print(f"   (ignored — ambient speech: {text!r})")
        return None

    # 5) long-transcription guard — music lyrics bleed through the mic as long
    #    multi-sentence transcriptions. Real commands are almost always < 25 words.
    #    If the capture is very long AND it doesn't start with a known verb, drop it.
    if len(_words) > 25:
        _cmd_starters = {"play", "pause", "stop", "resume", "skip", "next", "set",
                         "remind", "cancel", "open", "what", "how", "who", "when",
                         "where", "is", "are", "can", "will", "tell", "show", "ted"}
        if _first_w not in _cmd_starters:
            print(f"   (ignored — likely ambient audio: {len(_words)} words)")
            return None

    print(f"📝 Heard: {text!r}")
    return text

# ---------- small sounds ----------
def play_chime(window, api):
    """Play a soft 440 Hz ping (~120 ms) as wake-activation feedback."""
    dur = 0.12
    t   = np.linspace(0, dur, int(SAMPLE_RATE * dur), endpoint=False)
    tone = (np.sin(2 * np.pi * 440 * t) * np.exp(-30 * t) * 0.22).astype(np.float32)
    engine.reset_barge_in()   # a stale barge flag would silently skip the chime
    engine.play_samples(tone, SAMPLE_RATE, on_amplitude=amp_cb(window))


def play_timer_bell():
    """Play a two-chime bell sound using macOS afplay (blocking — call in a thread
    or inline before speaking so Ted waits for the bell before announcing)."""
    _snd = "/System/Library/Sounds/Glass.aiff"
    for _ in range(2):
        try:
            subprocess.run(["afplay", _snd], timeout=3)
        except Exception:
            pass
        time.sleep(0.20)


def spotify_volume(pct):
    """Set Spotify's volume — only if Spotify is ALREADY running (never launch it)."""
    script = (
        f'if application "Spotify" is running then\n'
        f'    tell application "Spotify" to set sound volume to {pct}\n'
        f'end if'
    )
    try:
        subprocess.run(["osascript", "-e", script], timeout=3, capture_output=True)
    except Exception:
        pass
