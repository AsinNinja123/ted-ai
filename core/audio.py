"""
core/audio.py — Ted's audio layer.

ONE job: let the rest of Ted (a) listen for a turn, (b) speak, and (c) get
interrupted mid-sentence by the user's voice (barge-in). The mic stays ON the
whole time in BOTH modes, so you can always talk over Ted.

Two modes, chosen automatically at start():

  • "aec"      — the native Swift engine (native/ted_audio) is built AND starts.
                 Apple Voice Processing provides echo cancellation, with
                 macOS 14+'s other-audio ducking set to minimum. The mic streams
                 continuously, including while Ted speaks, so the user can
                 interrupt over speakers.

  • "fallback" — pure Python (sounddevice). Still full-duplex / always-listening,
                 so barge-in works great on HEADPHONES. On speakers Ted's own
                 voice leaks into the mic and can interrupt him, so use headphones
                 in this mode (or build the native engine).

Both modes share the same continuous-reader → queue → capture/barge-in logic;
they differ only in where mic frames come from and where playback goes.
"""


# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 23 (§23.1 – §23.4)
# =============================================================================
#
#  WHAT THIS FILE IS
#      The lowest level of Ted: raw microphone frames in, raw speaker samples
#      out, and the machinery that lets you talk over Ted mid-sentence.
#
#      You will probably never need to change this file. Read it to understand
#      why interrupting works, then leave it alone unless interrupting stops
#      working.
#
#  BARGE-IN, AND WHY IT IS HARDER THAN IT SOUNDS
#      "Let the user interrupt" sounds like: if the mic is loud while Ted is
#      talking, stop talking. That version fails in three separate ways, and all
#      three were found the hard way:
#
#      1. A clap is loud. So is a door. Loudness alone is not speech, which is
#         why there is a voice-activity detector (webrtcvad) AND a pitch check —
#         human speech has a fundamental frequency roughly 70–320 Hz, and
#         autocorrelation finds it. A clap has no pitch.
#
#      2. Ted's own voice comes out of the speakers and back into the mic, so
#         Ted interrupts himself. The native Swift engine uses Apple's Voice
#         Processing to cancel that echo. Without the native engine, use
#         headphones — that is what "fallback" mode means below.
#
#      3. THE BIG ONE: detection used to be switched on only while audio was
#         actually playing, and playback goes briefly silent BETWEEN SENTENCES —
#         which is exactly where a human interrupts. Ted was deaf at every
#         sentence boundary. The fix was `_in_reply`, which keeps detection
#         alive across the whole reply rather than each sentence.
#
#      A sliding window then requires several consecutive speech-like frames
#      before it believes you, so one stray frame cannot cut Ted off.
#
#  THE TWO MODES
#      "aec"       the native Swift binary (native/ted_audio) built and running.
#                  Echo cancelled. You can interrupt over speakers.
#      "fallback"  pure Python via sounddevice. Works fine on headphones.
#
#      start() picks one automatically. Which one you got is worth knowing when
#      barge-in misbehaves.
#
#  IF YOU WANT TO CHANGE SOMETHING
#      Turn on the barge-in debug output FIRST. Every threshold in here was
#      tuned against real recordings, and changing one blind is how the silent
#      failure comes back. §35.
# =============================================================================

import os
import sys
import time
import queue
import struct
import threading
import collections

import numpy as np

# Voice activity detection for barge-in: distinguishes speech from claps, door
# slams, and other loud transients so only a VOICE interrupts Ted. Optional —
# without it barge-in falls back to energy-only detection.
try:
    import warnings as _warnings
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore")        # webrtcvad emits a pkg_resources deprecation warning
        import webrtcvad as _webrtcvad
except ImportError:
    _webrtcvad = None

HOME = os.path.expanduser("~/ted-ai")
BINARY = os.path.join(HOME, "native", "ted_audio")  # Swift AEC binary path

# ---- Audio Constants ----
# Everything is 16 kHz mono — the rate Whisper expects for transcription.
SAMPLE_RATE     = 16000
FRAME           = 320               # 20 ms of samples at 16 kHz
FRAME_SEC       = FRAME / SAMPLE_RATE
BYTES_PER_FRAME = FRAME * 2         # int16 = 2 bytes per sample

# ---- Listening Behaviour ----
SILENCE_HANG  = 1.35    # seconds of quiet that end a turn (1.0 s + 350 ms buffer for trailing consonants)
MAX_TURN      = 120.0   # hard cap so a runaway recording can't go forever
START_TIMEOUT = 30.0    # give up waiting for speech onset after this many seconds
PREROLL       = 12      # frames to keep before speech onset so we don't clip the first word

# ---- Barge-In Tuning ----
# To interrupt Ted, the last BARGE_WINDOW frames (~300 ms) must contain:
#   • BARGE_FRAMES frames that are LOUD (energy above the ambient threshold by
#     BARGE_MARGIN) and pass webrtcvad's speech classifier, AND
#   • BARGE_PITCH_FRAMES of those with detectable vocal PITCH (autocorrelation
#     peak ≥ PITCH_MIN in the 70–320 Hz range).
# Each layer covers the others' blind spots: the energy gate rejects ambient
# noise; the frame-count floor rejects short transients (a clap is only ~4 loud
# frames); the pitch gate rejects sustained broadband noise — webrtcvad alone
# can't, because telephony fricatives ARE broadband noise, so it happily calls
# claps "speech". Measured pitch strength: real speech 0.4–0.8, claps/noise
# ≤ ~0.15. Trigger latency is roughly 250–400 ms of sustained speech.
# Margin history: 3.0 when energy was the ONLY discriminator; now that VAD +
# pitch + the frame-count floor reject non-speech, the energy gate only needs
# to clear ambient noise. At 3.0 the bar sat at ~0.018, which normal-volume
# speech from across a desk barely reaches during playback (VP shaves the
# near-end voice slightly in double-talk) — barge-in worked only when leaning
# in or speaking up.
BARGE_MARGIN       = 2.0    # voice energy must be this multiple of threshold
# Absolute floor on the barge bar. VP's echo-cancelled channel carries ≤ ~0.006
# residual of Ted's own voice, so anything above 0.012 during playback is real
# near-end sound; a noisy calibration must not raise the bar past the quiet
# bursts VP lets through in double-talk.
BARGE_BAR_MIN      = 0.012
BARGE_FRAMES       = 10     # loud+VAD frames needed within the window (~200 ms of speech)
BARGE_WINDOW       = 15     # sliding window length in frames (~300 ms)
BARGE_PITCH_FRAMES = 4      # window frames that must carry vocal pitch
PITCH_MIN          = 0.5    # normalized autocorrelation floor for "has pitch"
_PITCH_LAGS        = (50, 229)  # autocorr lag range: 320 Hz down to 70 Hz at 16 kHz
# Absolute ceiling on the barge bar. calibrate() caps the ambient threshold at
# 0.025, so threshold * BARGE_MARGIN could demand 0.075 RMS — a shout. Normal
# speech is ~0.03–0.10, so 0.030 keeps interruption possible in a noisy room.
BARGE_THRESHOLD_MAX = 0.030

# Run with TED_DEBUG_BARGE=1 to print barge-in candidate frames (RMS vs. the
# effective bar) while Ted is speaking — the only way to see why a barge did
# or didn't trigger. Lines also append to data/barge_debug.log for post-mortem.
DEBUG_BARGE = os.environ.get("TED_DEBUG_BARGE", "") not in ("", "0")
_DEBUG_LOG_PATH = os.path.join(HOME, "data", "barge_debug.log")


def _barge_debug(line):
    print(line, file=sys.stderr)
    try:
        with open(_DEBUG_LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except OSError:
        pass

DEFAULT_THRESHOLD = 0.012  # fallback VAD threshold before calibration


# ---- RMS Helpers ----

def _rms_float(frame):
    """Return the RMS energy of a float32 frame in the range [0, 1]."""
    if len(frame) == 0:
        return 0.0
    return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))


def _rms_int16(chunk):
    """Return the RMS energy of an int16 chunk normalised to [0, 1]."""
    if len(chunk) == 0:
        return 0.0
    f = chunk.astype(np.float32) / 32768.0  # normalise to float before squaring
    return float(np.sqrt(np.mean(f ** 2)))


class AudioEngine:
    def __init__(self, threshold=DEFAULT_THRESHOLD, output_sr=SAMPLE_RATE, fallback_bargein=True):
        self.mode = None                        # "aec" | "fallback", set by start()
        self.proc = None                        # subprocess handle for the Swift AEC binary
        self._threshold = threshold             # VAD silence/speech boundary
        self._output_sr = output_sr             # playback sample rate (fallback only; aec is always 16k)
        # In fallback (no echo cancellation) mode, Ted's own voice can leak into the
        # mic on SPEAKERS and trigger a false barge-in (self-interruption). Set this
        # False (config: FALLBACK_VOICE_BARGEIN) to disable voice barge-in unless the
        # native AEC engine is active. Default True preserves barge-in for headphones.
        self._fallback_bargein = fallback_bargein
        self._lock = threading.Lock()
        self._q = queue.Queue(maxsize=200)      # ~4 s of 20 ms mic frames
        self._preroll = collections.deque(maxlen=PREROLL)  # ring buffer of recent frames before speech
        self._last_rms = 0.0
        self._playing = False                   # True while Ted is speaking
        # True for the span of a whole multi-sentence reply, including the
        # synth gaps BETWEEN sentences where _playing is False. Barge-in
        # listens on (_playing or _in_reply) so a user who interrupts at a
        # sentence pause — the most natural moment — is still heard.
        self._in_reply = False
        self.barge_in = False                   # set True when user speaks over Ted
        # Sliding windows of per-frame verdicts (see barge tuning above):
        # _barge_hits = "loud AND passes VAD", _barge_pitch = "loud AND has vocal pitch".
        self._barge_hits = collections.deque(maxlen=BARGE_WINDOW)
        self._barge_pitch = collections.deque(maxlen=BARGE_WINDOW)
        # VAD mode 2: middle aggressiveness — stricter modes drop too many real
        # speech frames, looser ones let noise through.
        self._vad = _webrtcvad.Vad(2) if _webrtcvad is not None else None
        self._first_frame = threading.Event()   # signals that the mic has delivered at least one frame
        self._mic_muted = False                 # True while the mic is physically off
        # AEC mode proves itself by delivering a mic frame. When the engine
        # starts with the mic OFF there are no frames to prove it with, so the
        # proof is deferred to the first unmute — see _verify_aec_mic().
        self._mic_verified = False
        self._closing = False                   # True once close() runs — stops the restart watchdog
        # fallback sounddevice handles (None in aec mode)
        self._sd = None
        self._in_stream = None
        self._out_stream = None

    # ---- Startup ----

    def start(self, mic=True):
        """Launch the audio engine. Tries AEC first, falls back to sounddevice. Returns mode string.

        mic=False starts PLAYBACK ONLY and never installs a mic tap, so macOS
        never lights the orange recording indicator. Ted boots chat-first and
        muted; opening the mic at import time claimed the device for a feature
        that was switched off, and the indicator is what Charlie sees.

        The mic is installed later by unmute_mic(), which is also where the AEC
        device check now happens — it needs a real frame and there are none
        while the tap is off.
        """
        self._mic_muted = not mic
        if os.path.exists(BINARY) and os.access(BINARY, os.X_OK):
            try:
                import subprocess
                self.proc = subprocess.Popen(
                    [BINARY],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,                  # unbuffered so frames arrive immediately
                )
                self.mode = "aec"
                threading.Thread(target=self._reader_aec, daemon=True).start()
                threading.Thread(target=self._log_stderr, daemon=True).start()
                if not mic:
                    # Take the tap out before the binary can settle into
                    # listening. The write sits in the pipe until the binary
                    # reads it, so this lands as early as it possibly can.
                    self._send_mic_command(b"M")
                    if self.proc.poll() is None:
                        return "aec"
                elif self.proc.poll() is None and self._first_frame.wait(2.5):
                    self._mic_verified = True
                    return "aec"
                # Binary started but never sent a mic frame — likely a CoreAudio device conflict.
                print("[audio] native engine isn't delivering audio (likely macOS "
                      "echo-cancellation can't use your mic+speaker combo) — "
                      "falling back to sounddevice.", file=sys.stderr)
                try:
                    self.proc.terminate()
                except Exception:
                    pass
                self.proc = None
                time.sleep(0.6)                 # let CoreAudio release the device before sounddevice opens it
            except Exception as e:
                print(f"[audio] AEC engine failed to start ({e}); using sounddevice.",
                      file=sys.stderr)

        self.mode = "fallback"
        self._start_fallback(open_input=mic)
        return "fallback"

    @property
    def aec(self):
        """True when running in AEC (Swift binary) mode."""
        return self.mode == "aec"

    def _start_fallback(self, open_input=True):
        """Open persistent mic and output streams via sounddevice. Streams stay open for the
        lifetime of the session — opening per-turn was crashing PortAudio. (Mute/unmute
        closes and reopens only the INPUT stream, which is infrequent enough to be safe.)

        open_input=False opens the OUTPUT stream only. Ted can still speak; the
        mic device is never claimed, so there is no recording indicator."""
        import sounddevice as sd
        self._sd = sd
        if open_input:
            self._open_fallback_input()
            self._mic_verified = True
        self._out_stream = sd.OutputStream(
            samplerate=self._output_sr, channels=1, dtype="int16", blocksize=FRAME)
        self._out_stream.start()
        threading.Thread(target=self._reader_fallback, daemon=True).start()

    def _open_fallback_input(self):
        """(Re)open the sounddevice input stream — grabs the mic device."""
        self._in_stream = self._sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=FRAME)
        self._in_stream.start()

    # ---- Background Readers ----

    def _ingest_frame(self, frame):
        """Process one mic frame: update preroll, check for barge-in, push to queue."""
        if not self._first_frame.is_set():
            self._first_frame.set()             # unblock start() if still waiting
        rms = _rms_float(frame)
        with self._lock:
            self._preroll.append(frame)
            self._last_rms = rms
            if (self._playing or self._in_reply) and (self.mode == "aec" or self._fallback_bargein):
                # Check for barge-in while Ted is speaking OR between the
                # sentences of a streamed reply (_in_reply) — the synth gap is
                # where a human naturally interrupts, and Ted isn't emitting
                # sound there so even fallback mode can't self-trigger.
                # AEC mode: Ted's voice is cancelled so only the user's voice remains.
                # Fallback on speakers: Ted's voice leaks here and can false-trigger,
                # so this is gated behind _fallback_bargein (off → no self-interruption).
                bar = max(BARGE_BAR_MIN,
                          min(self._threshold * BARGE_MARGIN, BARGE_THRESHOLD_MAX))
                loud = rms > bar
                voiced = loud and self._is_voice(frame)
                pitched = loud and self._pitch_strength(frame) >= PITCH_MIN
                self._barge_hits.append(voiced)
                self._barge_pitch.append(pitched)
                hits, pitch_n = sum(self._barge_hits), sum(self._barge_pitch)
                # The pitch gate only applies in the synth gaps between sentences,
                # where mic audio arrives clean (claps ring with false periodicity
                # there, and real voice keeps its pitch). While audio is actually
                # PLAYING, macOS voice processing garbles what little near-end
                # signal it lets through — genuine voice bursts arrive unpitched —
                # and VP has already removed the echo and crushed non-speech, so
                # hits alone are trustworthy evidence.
                trigger = hits >= BARGE_FRAMES and (
                    (self._playing and self.mode == "aec")
                    or pitch_n >= BARGE_PITCH_FRAMES)
                if DEBUG_BARGE and loud:
                    _barge_debug(f"[barge] rms={rms:.4f} > bar={bar:.4f} voiced={voiced} "
                                 f"pitched={pitched} hits={hits}/{BARGE_FRAMES} "
                                 f"pitch={pitch_n}/{BARGE_PITCH_FRAMES}"
                                 f"{'  → BARGE' if trigger else ''}")
                if trigger:
                    self.barge_in = True        # conversation loop polls this flag to stop playback
        try:
            self._q.put_nowait(frame)
        except queue.Full:
            # Queue full: drop the oldest frame to make room so the reader never blocks.
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(frame)
            except queue.Full:
                pass

    def _is_voice(self, frame):
        """True when webrtcvad classifies this 20 ms frame as speech.
        Permissive on any failure (no VAD installed, odd frame length) so
        barge-in degrades to energy-only rather than going dead."""
        if self._vad is None or len(frame) != FRAME:
            return True
        try:
            pcm = (np.clip(frame, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            return self._vad.is_speech(pcm, SAMPLE_RATE)
        except Exception:
            return True

    @staticmethod
    def _pitch_strength(frame):
        """Normalized autocorrelation peak in the human pitch range [0, 1].
        Voiced speech (vowels) scores 0.4–0.8; claps, hiss and other broadband
        noise stay near 0.1 — periodicity is the one thing a transient can't fake."""
        x = frame - frame.mean()
        if np.max(np.abs(x)) < 1e-6:
            return 0.0
        r = np.fft.irfft(np.abs(np.fft.rfft(x, 1024)) ** 2)
        if r[0] <= 0:
            return 0.0
        return float(np.max(r[_PITCH_LAGS[0]:_PITCH_LAGS[1]]) / r[0])

    def _restart_aec(self):
        """Restart the Swift binary after a crash. Waits 1 s for CoreAudio to settle."""
        import subprocess
        try:
            self.proc.terminate()
        except Exception:
            pass
        time.sleep(1.0)
        try:
            self.proc = subprocess.Popen(
                [BINARY],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            threading.Thread(target=self._log_stderr, daemon=True).start()
            print("[audio] Swift binary restarted.", file=sys.stderr)
            # A fresh binary starts with the mic tap ON — if Ted was muted when
            # the old one crashed, re-apply the mute so a crash can never
            # silently turn the mic back on.
            if self._mic_muted:
                try:
                    self.proc.stdin.write(b"M")
                    self.proc.stdin.flush()
                    print("[audio] restart while muted — mic tap re-disabled.", file=sys.stderr)
                except Exception:
                    pass
        except Exception as e:
            print(f"[audio] binary restart failed: {e}", file=sys.stderr)
            self.proc = None

    def _reader_aec(self):
        """Read raw int16 mic frames from the Swift binary's stdout and push them to the queue.

        Auto-restarts the binary on crash so a CoreAudio glitch doesn't silence Ted permanently.
        """
        while True:
            if self.proc is None:
                time.sleep(1.0)
                continue
            out = self.proc.stdout
            buf = b""
            while True:
                try:
                    chunk = out.read(BYTES_PER_FRAME - len(buf))
                except Exception:
                    chunk = b""
                if not chunk:
                    if self._closing:
                        return                  # engine shut down — don't resurrect the binary
                    print("[audio] mic stream ended — restarting binary.", file=sys.stderr)
                    self._restart_aec()
                    break                       # inner loop: start reading from new proc
                buf += chunk
                if len(buf) < BYTES_PER_FRAME:
                    continue
                frame = np.frombuffer(buf, dtype=np.int16).astype(np.float32) / 32768.0
                buf = b""
                self._ingest_frame(frame)

    def _reader_fallback(self):
        """Continuously read mic frames from the sounddevice InputStream and push to queue.
        While muted the stream is None (device fully released) — idle quietly."""
        while True:
            stream = self._in_stream
            if stream is None:
                time.sleep(0.15)
                continue
            try:
                frame, _ = stream.read(FRAME)
            except Exception as e:
                if self._in_stream is None:
                    continue        # closed mid-read by mute_mic() — not an error
                print(f"[audio] mic read error: {e}", file=sys.stderr)
                time.sleep(0.1)
                continue
            self._ingest_frame(np.asarray(frame, dtype=np.float32).flatten())

    def _log_stderr(self):
        """Forward the Swift binary's stderr to Python's stderr for diagnostics."""
        for line in iter(self.proc.stderr.readline, b""):
            try:
                sys.stderr.write(line.decode("utf-8", "replace"))
            except Exception:
                pass

    # ---- Calibration ----

    def calibrate(self):
        """Sample ~0.8 s of ambient mic audio and set the VAD threshold above the noise floor.

        Without calibration, a noisy room can trigger false speech detections. The threshold
        is clamped to a minimum so a very quiet room doesn't make the engine hypersensitive.
        Returns the new threshold value.
        """
        levels = []
        t0 = time.time()
        while time.time() - t0 < 0.8:
            try:
                frame = self._q.get(timeout=0.3)
            except queue.Empty:
                break
            levels.append(_rms_float(frame))
        if levels:
            self._threshold = max(0.006, min(0.025, float(np.mean(levels)) * 2.5))  # 2.5× noise floor, capped
        return self._threshold

    # ---- Capture ----

    def capture_turn(self, prearmed=False):
        """Listen until the user stops talking and return a float32 @16 kHz array, or None on timeout.

        prearmed=True is used after a barge-in: capturing starts immediately, seeded with
        the preroll buffer, instead of waiting for speech onset from silence.
        """
        captured = []
        silent = 0
        start = time.time()
        hang_frames = int(SILENCE_HANG / FRAME_SEC)  # number of silent frames that end the turn
        preroll = []

        if prearmed:
            # User was already speaking (barge-in) — include buffered frames from before we got here.
            with self._lock:
                captured.extend(list(self._preroll))
            started = True
        else:
            self._drain_queue()                 # discard stale frames so we don't transcribe old audio
            with self._lock:
                preroll = list(self._preroll)   # save recent frames to prepend once speech starts
            started = False

        while True:
            try:
                frame = self._q.get(timeout=0.5)
            except queue.Empty:
                if not started and time.time() - start > START_TIMEOUT:
                    return None                 # nobody started talking
                continue

            rms = _rms_float(frame)
            elapsed = time.time() - start

            if rms > self._threshold:
                if not started:
                    started = True
                    captured.extend(preroll)    # include pre-speech frames so word 1 isn't clipped
                captured.append(frame)
                silent = 0                      # voice detected — reset silence counter
            else:
                if started:
                    captured.append(frame)      # always include trailing silence (natural speech rhythm)
                    silent += 1
                    if silent >= hang_frames:
                        break                   # enough consecutive silence — turn is over

            if not started and elapsed > START_TIMEOUT:
                return None
            if started and elapsed > MAX_TURN:
                break                           # safety cap — don't record forever

        if not captured:
            return None
        return np.concatenate(captured).astype(np.float32)

    def _drain_queue(self):
        """Discard all frames currently sitting in the mic queue."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    # ---- Playback ----

    def play_samples(self, samples, sr, on_amplitude=None, should_stop=None):
        """Play audio samples, calling on_amplitude(rms) per chunk and honouring barge-in.

        In aec mode: resamples to 16 kHz and sends via the binary protocol ('A' + length + PCM).
        In fallback mode: resamples to output_sr and writes directly to the sounddevice stream.
        Returns True if playback completed, False if interrupted.
        """
        # AEC is hardcoded to 16 kHz (Swift binary constraint); fallback uses whatever output_sr was set.
        target_sr = SAMPLE_RATE if self.mode == "aec" else self._output_sr
        pcm = self._to_int16(samples, sr, target_sr)
        if len(pcm) == 0:
            return True

        CH = 1024                               # ~64 ms per chunk — small enough for responsive barge-in
        finished = True
        # NOTE: barge state is deliberately NOT reset here. speak_streaming calls
        # play_samples once per sentence; resetting per call would both discard a
        # barge detected in the synth gap between sentences and zero the frame
        # counter at every boundary. Callers reset once per utterance instead
        # (speak / speak_streaming / play_chime).
        self._set_playing(True)
        try:
            for i in range(0, len(pcm), CH):
                if (should_stop and should_stop()) or self.barge_in:
                    finished = False
                    break
                chunk = pcm[i:i + CH]
                if self.mode == "aec":
                    self._send_audio(chunk.tobytes())
                    time.sleep(len(chunk) / SAMPLE_RATE * 0.9)   # throttle writes to ~realtime so the pipe doesn't fill
                else:
                    self._out_stream.write(chunk.reshape(-1, 1))  # blocking write paces itself to realtime
                if on_amplitude:
                    on_amplitude(_rms_int16(chunk))
            if not finished:
                self.stop_playback()            # send 'S' / abort so audio cuts off immediately
            elif self.mode == "aec":
                time.sleep(0.15)                # let the last chunk drain through the pipe before we clear _playing
        except Exception as e:
            print(f"[audio] playback error: {e}", file=sys.stderr)
        finally:
            self._set_playing(False)
        return finished

    def stop_playback(self):
        """Signal immediate stop: sends 'S' to the Swift binary, or aborts the sounddevice stream."""
        if self.mode == "aec":
            try:
                self.proc.stdin.write(b"S")     # binary protocol: 'S' = stop / barge-in flush
                self.proc.stdin.flush()
            except Exception:
                pass
        else:
            try:
                self._out_stream.abort()        # discard queued audio for an instant cut
                self._out_stream.start()        # re-arm for the next utterance
            except Exception:
                pass

    # ---- Helpers ----

    def _send_audio(self, raw_bytes):
        """Write a play command to the Swift binary: 'A' + 4-byte big-endian length + raw PCM."""
        header = b"A" + struct.pack(">I", len(raw_bytes))
        try:
            self.proc.stdin.write(header + raw_bytes)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass                                # binary exited — ignore silently

    def _set_playing(self, val):
        """Set the _playing flag. Does NOT touch barge state — a barge run in
        progress must survive the sentence boundaries of a streamed reply."""
        with self._lock:
            self._playing = val

    def set_in_reply(self, val):
        """Mark the span of a whole (possibly multi-sentence) reply so barge-in
        detection stays live through the synth gaps between sentences.
        Clearing it leaves barge_in untouched — the caller reads that flag
        after the reply to report barged_by_voice."""
        with self._lock:
            self._in_reply = val
            if not val:
                self._barge_hits.clear()        # don't carry partial evidence past the reply
                self._barge_pitch.clear()

    def _send_mic_command(self, byte):
        """Write one control byte to the Swift engine. Never raises."""
        if not (self.proc and self.proc.poll() is None):
            return False
        try:
            self.proc.stdin.write(byte)
            self.proc.stdin.flush()
            return True
        except Exception:
            return False

    def mic_is_open(self):
        """True when this process is holding the microphone open right now.

        Ground truth for the HUD and for anyone asking why the recording
        indicator is lit — it reports the tap, not the user's intent.
        """
        if self._mic_muted:
            return False
        if self.mode == "aec":
            return bool(self.proc and self.proc.poll() is None)
        return self._in_stream is not None

    def mute_mic(self):
        """Turn the mic PHYSICALLY off. AEC mode: send 'M' to the Swift binary,
        which removes the mic tap so macOS releases the orange indicator.
        Fallback mode: close the sounddevice input stream, releasing the device.
        Playback (TTS) is unaffected in both modes."""
        self._mic_muted = True
        if self.mode == "aec":
            self._send_mic_command(b"M")
        elif self.mode == "fallback":
            stream, self._in_stream = self._in_stream, None
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    def unmute_mic(self):
        """Turn the mic back on. AEC mode: send 'U' to the Swift binary to
        reinstall the mic tap. Fallback mode: reopen the input stream.

        This is the first moment the mic is claimed in a chat-first session, so
        it is also where the AEC device check happens now.
        """
        self._mic_muted = False
        if self.mode == "aec":
            if not self._send_mic_command(b"U"):
                return
            self._verify_aec_mic()
        elif self.mode == "fallback" and self._in_stream is None:
            try:
                self._open_fallback_input()
                self._mic_verified = True
            except Exception as e:
                print(f"[audio] mic reopen failed: {e}", file=sys.stderr)

    def _verify_aec_mic(self):
        """Prove the native engine can actually deliver mic audio, once.

        start() used to do this by waiting for a first frame, and fell back to
        sounddevice when none arrived — a real macOS failure where echo
        cancellation cannot use a given mic+speaker pair. Starting with the mic
        off removed the frames that check depended on, so the check moved here,
        to the first unmute. It runs once per session and never raises.
        """
        if self._mic_verified:
            return
        self._first_frame.clear()
        if self._first_frame.wait(2.5):
            self._mic_verified = True
            return
        print("[audio] native engine isn't delivering audio (likely macOS "
              "echo-cancellation can't use your mic+speaker combo) — "
              "falling back to sounddevice.", file=sys.stderr)
        try:
            proc, self.proc = self.proc, None
            if proc is not None:
                proc.terminate()
            time.sleep(0.6)          # let CoreAudio release the device
            self.mode = "fallback"
            self._start_fallback(open_input=True)
        except Exception as e:
            self.mode = "fallback"
            print(f"[audio] sounddevice fallback also failed: {e}", file=sys.stderr)

    def reset_barge_in(self):
        """Clear the barge-in flag and detection window before starting a new playback."""
        with self._lock:
            self.barge_in = False
            self._barge_hits.clear()
            self._barge_pitch.clear()

    @staticmethod
    def _to_int16(samples, src_sr, target_sr=SAMPLE_RATE):
        """Resample float32 audio from src_sr to target_sr and convert to int16.

        Uses linear interpolation — fast enough for realtime and good enough for TTS output.
        """
        s = np.asarray(samples, dtype=np.float32).flatten()
        if len(s) == 0:
            return np.zeros(0, dtype=np.int16)
        if src_sr and src_sr != target_sr:
            n = int(round(len(s) * target_sr / float(src_sr)))  # target number of samples
            if n > 0:
                s = np.interp(np.linspace(0, len(s) - 1, n),
                              np.arange(len(s)), s).astype(np.float32)
        s = np.clip(s, -1.0, 1.0)              # guard against float overflow before scaling
        return (s * 32767.0).astype(np.int16)

    def close(self):
        """Shut down the audio engine and release all resources."""
        self._closing = True
        if self.proc:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
        for stream in (self._in_stream, self._out_stream):
            try:
                if stream:
                    stream.stop(); stream.close()
            except Exception:
                pass
