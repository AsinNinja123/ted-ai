"""
core/audio.py — Ted's audio layer.

ONE job: let the rest of Ted (a) listen for a turn, (b) speak, and (c) get
interrupted mid-sentence by the user's voice (barge-in). The mic stays ON the
whole time in BOTH modes, so you can always talk over Ted.

Two modes, chosen automatically at start():

  • "aec"      — the native Swift engine (native/ted_audio) is built AND starts.
                 Gives hardware echo cancellation, so you can barge in over
                 SPEAKERS without Ted hearing himself. Best mode.

  • "fallback" — pure Python (sounddevice). Still full-duplex / always-listening,
                 so barge-in works great on HEADPHONES. On speakers Ted's own
                 voice leaks into the mic and can interrupt him, so use headphones
                 in this mode (or build the native engine).

Both modes share the same continuous-reader → queue → capture/barge-in logic;
they differ only in where mic frames come from and where playback goes.
"""

import os
import sys
import time
import queue
import struct
import threading
import collections

import numpy as np

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
# Voice must exceed the ambient threshold by BARGE_MARGIN for BARGE_FRAMES consecutive frames
# to count as intentional speech rather than noise — prevents a single loud click from interrupting.
BARGE_MARGIN  = 3.0     # voice energy must be this multiple of threshold
BARGE_FRAMES  = 6       # ...for this many consecutive frames (~120 ms)

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
        self.barge_in = False                   # set True when user speaks over Ted
        self._barge_run = 0                     # consecutive frames above barge threshold
        self._first_frame = threading.Event()   # signals that the mic has delivered at least one frame
        self._mic_muted = False                 # True while the mic is physically off
        self._closing = False                   # True once close() runs — stops the restart watchdog
        # fallback sounddevice handles (None in aec mode)
        self._sd = None
        self._in_stream = None
        self._out_stream = None

    # ---- Startup ----

    def start(self):
        """Launch the audio engine. Tries AEC first, falls back to sounddevice. Returns mode string."""
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
                if self.proc.poll() is None and self._first_frame.wait(2.5):
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
        self._start_fallback()
        return "fallback"

    @property
    def aec(self):
        """True when running in AEC (Swift binary) mode."""
        return self.mode == "aec"

    def _start_fallback(self):
        """Open persistent mic and output streams via sounddevice. Streams stay open for the
        lifetime of the session — opening per-turn was crashing PortAudio. (Mute/unmute
        closes and reopens only the INPUT stream, which is infrequent enough to be safe.)"""
        import sounddevice as sd
        self._sd = sd
        self._open_fallback_input()
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
            if self._playing and (self.mode == "aec" or self._fallback_bargein):
                # Only check for barge-in while Ted is speaking.
                # AEC mode: Ted's voice is cancelled so only the user's voice remains.
                # Fallback on speakers: Ted's voice leaks here and can false-trigger,
                # so this is gated behind _fallback_bargein (off → no self-interruption).
                if rms > self._threshold * BARGE_MARGIN:
                    self._barge_run += 1
                    if self._barge_run >= BARGE_FRAMES:
                        self.barge_in = True    # conversation loop polls this flag to stop playback
                else:
                    self._barge_run = 0         # reset run on any quiet frame so it can't accumulate slowly
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
        self._set_playing(True)
        self.reset_barge_in()
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
        """Set the _playing flag and reset the barge-in counter when starting playback."""
        with self._lock:
            self._playing = val
            if val:
                self._barge_run = 0             # always start a fresh barge-in count

    def mute_mic(self):
        """Turn the mic PHYSICALLY off. AEC mode: send 'M' to the Swift binary,
        which removes the mic tap so macOS releases the orange indicator.
        Fallback mode: close the sounddevice input stream, releasing the device.
        Playback (TTS) is unaffected in both modes."""
        self._mic_muted = True
        if self.mode == "aec" and self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"M")
                self.proc.stdin.flush()
            except Exception:
                pass
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
        reinstall the mic tap. Fallback mode: reopen the input stream."""
        self._mic_muted = False
        if self.mode == "aec" and self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"U")
                self.proc.stdin.flush()
            except Exception:
                pass
        elif self.mode == "fallback" and self._in_stream is None:
            try:
                self._open_fallback_input()
            except Exception as e:
                print(f"[audio] mic reopen failed: {e}", file=sys.stderr)

    def reset_barge_in(self):
        """Clear the barge-in flag and run counter before starting a new playback."""
        with self._lock:
            self.barge_in = False
            self._barge_run = 0

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
