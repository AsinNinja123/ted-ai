// ted_audio.swift
// ---------------------------------------------------------------------------
// Full-duplex audio bridge for Ted.
//
// WHY THIS EXISTS:
//   Full-duplex capture + playback in one process, with a mic-mute that truly
//   releases the device (orange dot off). Apple Voice Processing cancels Ted's
//   speaker output from the mic; on macOS 14+ its other-audio ducking is set to
//   minimum so Spotify keeps playing normally. If Voice Processing is
//   unavailable, capture still works and Python's VAD/pitch gates are the
//   fallback against self-interruption.
//
// PROTOCOL (talks to core/audio.py over stdio):
//   stdout : continuous raw mic audio — int16 little-endian, mono, 16 kHz,
//            already echo-cancelled. Python reads fixed-size chunks.
//   stdin  : binary control messages —
//              'A' + uint32(len, big-endian) + <len bytes int16 LE 16k mono>
//                   => enqueue this PCM for playback (Ted speaking)
//              'S'  => stop/flush playback immediately (used for barge-in)
//   stderr : human-readable logs (keeps stdout pure PCM)
//
// BUILD:  ./build.sh         (or see build.sh for the swiftc line)
// RUN:    launched automatically by core/audio.py; you don't run it by hand.
// ---------------------------------------------------------------------------

import Foundation
import AVFoundation

// ---- configuration --------------------------------------------------------
let TARGET_RATE: Double = 16000          // rate Python/Whisper expect
let CHANNELS: AVAudioChannelCount = 1

// ---- small helpers --------------------------------------------------------
func log(_ s: String) {
    FileHandle.standardError.write(("ted_audio: " + s + "\n").data(using: .utf8)!)
}

// Don't die with SIGPIPE if Python closes the pipe — we handle EOF ourselves.
signal(SIGPIPE, SIG_IGN)

// ---- audio formats --------------------------------------------------------
// What we hand to Python (and read back from it): 16 kHz mono int16.
guard let pcm16Format = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                      sampleRate: TARGET_RATE,
                                      channels: CHANNELS,
                                      interleaved: true) else {
    log("FATAL: could not create int16 format"); exit(1)
}
// ---- microphone permission ------------------------------------------------
// A CLI tool must explicitly ask, and we must WAIT for the answer before the
// engine touches the mic — otherwise the input node reports 0 Hz / 0 channels.
func ensureMicAccess() {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized:
        return
    case .notDetermined:
        let sema = DispatchSemaphore(value: 0)
        var ok = false
        AVCaptureDevice.requestAccess(for: .audio) { granted in ok = granted; sema.signal() }
        sema.wait()
        if ok { return }
        log("FATAL: microphone permission denied.")
        exit(1)
    default:
        log("FATAL: microphone access not allowed. Enable it in System Settings → Privacy & Security → Microphone for your terminal/app.")
        exit(1)
    }
}
ensureMicAccess()

// ---- engine graph ---------------------------------------------------------
// TWO engines, deliberately:
//   engine        — playback only (player → outputNode). Never touches the
//                   input node, so it never opens the microphone.
//   captureEngine — input only. Muting STOPS this whole engine, which is the
//                   only way to make macOS release the mic device and drop the
//                   orange indicator: removing the tap alone leaves the input
//                   audio unit running and the orange dot stays lit.
// Playback keeps working while muted because it lives on its own engine.
let engine = AVAudioEngine()
let player = AVAudioPlayerNode()
let captureEngine = AVAudioEngine()
let inputNode = captureEngine.inputNode

// Voice processing (real AEC — cancels Ted's own speaker output from the mic
// so the user can talk over him). It was previously disabled because enabling
// it made macOS permanently duck Spotify and other apps; macOS 14 added
// voiceProcessingOtherAudioDuckingConfiguration, which lets us turn that
// ducking down to minimum. If enabling fails we log and carry on without it —
// Python's energy threshold is then the only guard against self-interruption.
var vpEnabled = false

// Attach the player and connect it at the engine's canonical format.
// Ted's 16 kHz audio up to this format ourselves before scheduling it.
engine.attach(player)
// Connect the player DIRECTLY to the output node (skip the main mixer — touching
// it forces an extra connection that can fail to initialize under voice
// processing). We use the output's own format and convert Ted's audio into it.
let engineFormat = engine.outputNode.inputFormat(forBus: 0)
log("engine/output format: \(engineFormat.sampleRate) Hz, \(engineFormat.channelCount) ch")
engine.connect(player, to: engine.outputNode, format: engineFormat)

// Playback converter: 16 kHz mono int16 (from Python) -> engine canonical float.
let playConverter = AVAudioConverter(from: pcm16Format, to: engineFormat)

let stdoutHandle = FileHandle.standardOutput

// The input device can have ANY channel count (we've seen 9). AVAudioConverter
// can't reliably collapse many channels straight to mono, so we downmix to mono
// OURSELVES first, then let a simple mono→16k converter do the resample.
var inConverter: AVAudioConverter? = nil
var loggedInput = false
var tapInstalled = false   // tracks whether the mic tap is currently active
var lastLoggedChannel = -1
var channelLogCount = 0

// ---- mic capture: extracted so it can be installed / removed at runtime -----
// Sending 'M' removes the tap AND stops the capture engine (mic device fully
// released → orange dot off). Sending 'U' restarts it. Playback is unaffected
// either way — it runs on the separate playback engine.
func installMicTap() {
    guard !tapInstalled else { return }
    tapInstalled = true
    inputNode.installTap(onBus: 0, bufferSize: 1024, format: nil) { (buffer, _) in
        // Mic frames stream UNCONDITIONALLY — including while Ted speaks.
        // (An earlier version dropped frames during TTS to fake echo-safety,
        // which made voice barge-in physically impossible: Python never saw
        // the user talking over Ted. Barge-in vs. self-echo discrimination
        // happens in core/audio.py via the energy threshold.)
        let inFormat = buffer.format
        let n = Int(buffer.frameLength)
        if inFormat.sampleRate <= 0 || n == 0 { return }
        guard let chData = buffer.floatChannelData else { return }

        if !loggedInput {
            loggedInput = true
            log("input format (live): \(inFormat.sampleRate) Hz, \(inFormat.channelCount) ch, interleaved=\(inFormat.isInterleaved) — downmixing to mono")
        }

        let channels = Int(inFormat.channelCount)
        guard let monoFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                             sampleRate: inFormat.sampleRate,
                                             channels: 1, interleaved: false),
              let monoBuf = AVAudioPCMBuffer(pcmFormat: monoFormat,
                                             frameCapacity: AVAudioFrameCount(n)) else { return }
        monoBuf.frameLength = AVAudioFrameCount(n)
        let mono = monoBuf.floatChannelData![0]

        // Voice Processing exposes several channels on some Macs. Channel 0
        // is the echo-cancelled feed and is the right choice WHILE Ted speaks,
        // but on Charlie's current five-channel format it can be nearly silent
        // while Ted is merely listening. Outside playback, select the loudest
        // real input channel so ordinary speech cannot disappear into a quiet
        // processed lane. During playback stay on channel 0 to preserve AEC.
        var selectedChannel = 0
        var selectedEnergy: Float = 0
        let preserveAEC = vpEnabled && player.isPlaying
        if !preserveAEC && inFormat.isInterleaved {
            let base = chData[0]
            var bestCh = 0
            var bestE: Float = -1
            for c in 0..<channels {
                var e: Float = 0, idx = c
                for _ in 0..<n { let v = base[idx]; e += v * v; idx += channels }
                if e > bestE { bestE = e; bestCh = c }
            }
            selectedChannel = bestCh
            selectedEnergy = bestE
        } else if !preserveAEC {
            var bestCh = 0
            var bestE: Float = -1
            for c in 0..<channels {
                let p = chData[c]
                var e: Float = 0
                for i in 0..<n { let v = p[i]; e += v * v }
                if e > bestE { bestE = e; bestCh = c }
            }
            selectedChannel = bestCh
            selectedEnergy = bestE
        }

        if inFormat.isInterleaved {
            let base = chData[0]
            var idx = selectedChannel
            for i in 0..<n { mono[i] = base[idx]; idx += channels }
        } else {
            let p = chData[selectedChannel]
            for i in 0..<n { mono[i] = p[i] }
        }

        // Log only meaningful channel changes, and cap the count. This gives
        // the Python launch log hard evidence about which input carried speech
        // without doing unbounded I/O on the realtime audio callback.
        let selectedRMS = selectedEnergy > 0 ? sqrt(selectedEnergy / Float(n)) : 0
        if !preserveAEC && selectedRMS >= 0.0007 && selectedChannel != lastLoggedChannel
                && channelLogCount < 8 {
            let rmsText = String(format: "%.4f", selectedRMS)
            log("selected input channel \(selectedChannel), rms=\(rmsText)")
            lastLoggedChannel = selectedChannel
            channelLogCount += 1
        }

        if inConverter == nil {
            inConverter = AVAudioConverter(from: monoFormat, to: pcm16Format)
        }
        guard let conv = inConverter else { return }

        let ratio = TARGET_RATE / inFormat.sampleRate
        let outCapacity = AVAudioFrameCount(Double(n) * ratio) + 32
        guard let outBuffer = AVAudioPCMBuffer(pcmFormat: pcm16Format,
                                               frameCapacity: outCapacity) else { return }

        var consumed = false
        let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
            if consumed { outStatus.pointee = .noDataNow; return nil }
            consumed = true
            outStatus.pointee = .haveData
            return monoBuf
        }

        var convErr: NSError?
        conv.convert(to: outBuffer, error: &convErr, withInputFrom: inputBlock)
        if let convErr = convErr { log("convert error: \(convErr.localizedDescription)"); return }

        let frames = Int(outBuffer.frameLength)
        if frames == 0 { return }
        if let chan = outBuffer.int16ChannelData {
            let data = Data(bytes: chan[0], count: frames * MemoryLayout<Int16>.size)
            stdoutHandle.write(data)
        }
    }
    log("mic tap ON")
}

func removeMicTap() {
    guard tapInstalled else { return }
    inputNode.removeTap(onBus: 0)
    tapInstalled = false
    inConverter = nil    // reset so it's rebuilt fresh when tap is reinstalled
    loggedInput  = false
}

func micOff() {
    // Removing the tap is not enough — the input audio unit keeps running and
    // macOS keeps the orange indicator lit. Stopping the capture engine
    // releases the device for real.
    removeMicTap()
    captureEngine.stop()
    log("mic OFF — capture engine stopped, device released")
}

func micOn() {
    do {
        captureEngine.prepare()
        try captureEngine.start()
    } catch {
        log("mic restart failed: \(error)")
        return
    }
    installMicTap()
}

// Enable voice processing BEFORE the tap is installed and the engine starts —
// it changes the input node's format, and must be set while the engine is idle.
do {
    try inputNode.setVoiceProcessingEnabled(true)
    vpEnabled = true
    // Leave VP's AGC on: it boosts the brief near-end bursts that survive
    // double-talk suppression, and measured barge reliability drops (4/6 → 2/6)
    // without it. The distortion it adds is handled on the Python side by
    // waiving the pitch gate during active playback.
    if #available(macOS 14.0, *) {
        inputNode.voiceProcessingOtherAudioDuckingConfiguration =
            AVAudioVoiceProcessingOtherAudioDuckingConfiguration(
                enableAdvancedDucking: false, duckingLevel: .min)
        log("voice processing ON (AEC) — other-audio ducking set to minimum")
    } else {
        log("voice processing ON (AEC) — pre-macOS-14: other apps may be ducked")
    }
} catch {
    log("voice processing unavailable (\(error)) — no echo cancellation; " +
        "Ted may hear himself on loud speakers")
}

// Install the tap on startup (mic on by default).
installMicTap()

// ---- playback: int16 16k bytes from Python -> engine format -> player ------
func schedulePCM(_ int16Bytes: Data) {
    let count = int16Bytes.count / 2
    if count == 0 { return }

    // Wrap the raw 16 kHz int16 from Python in a buffer.
    guard let inBuf = AVAudioPCMBuffer(pcmFormat: pcm16Format,
                                       frameCapacity: AVAudioFrameCount(count)) else { return }
    inBuf.frameLength = AVAudioFrameCount(count)
    int16Bytes.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
        if let dst = inBuf.int16ChannelData {
            memcpy(dst[0], raw.baseAddress, count * MemoryLayout<Int16>.size)
        }
    }

    guard let conv = playConverter else { return }
    let ratio = engineFormat.sampleRate / TARGET_RATE
    let outCap = AVAudioFrameCount(Double(count) * ratio) + 64
    guard let outBuf = AVAudioPCMBuffer(pcmFormat: engineFormat,
                                        frameCapacity: outCap) else { return }

    var consumed = false
    let inputBlock: AVAudioConverterInputBlock = { _, status in
        if consumed { status.pointee = .noDataNow; return nil }
        consumed = true
        status.pointee = .haveData
        return inBuf
    }
    var err: NSError?
    conv.convert(to: outBuf, error: &err, withInputFrom: inputBlock)
    if let err = err { log("play convert error: \(err.localizedDescription)"); return }

    player.scheduleBuffer(outBuf, completionHandler: nil)
    if !player.isPlaying { player.play() }
}

func stopPlayback() {
    // .stop() discards everything still queued — instant cutoff on Stop button.
    player.stop()
}

// ---- stdin reader: parse the control protocol -----------------------------
let stdinFD = FileHandle.standardInput.fileDescriptor

func readExact(_ n: Int) -> Data? {
    var out = Data(); out.reserveCapacity(n)
    var tmp = [UInt8](repeating: 0, count: 8192)
    while out.count < n {
        let need = min(tmp.count, n - out.count)
        let r = tmp.withUnsafeMutableBytes { read(stdinFD, $0.baseAddress, need) }
        if r <= 0 { return nil }            // EOF (Python exited) or error
        out.append(contentsOf: tmp[0..<r])
    }
    return out
}

func stdinLoop() {
    while true {
        guard let typeData = readExact(1) else {
            log("stdin closed — exiting"); exit(0)
        }
        switch typeData[0] {
        case UInt8(ascii: "A"):
            guard let lenData = readExact(4) else { exit(0) }
            let len = (Int(lenData[0]) << 24) | (Int(lenData[1]) << 16)
                    | (Int(lenData[2]) << 8)  |  Int(lenData[3])
            if len <= 0 || len > 50_000_000 { continue }   // sanity guard
            guard let payload = readExact(len) else { exit(0) }
            schedulePCM(payload)
        case UInt8(ascii: "S"):
            stopPlayback()
        case UInt8(ascii: "M"):              // Mute mic: stop capture engine → mic released, orange dot off
            micOff()
        case UInt8(ascii: "U"):              // Unmute mic: restart capture engine → listening resumes
            micOn()
        default:
            continue                         // unknown byte: skip and resync
        }
    }
}

// ---- go --------------------------------------------------------------------
engine.prepare()
do {
    try engine.start()
} catch {
    log("FATAL: playback engine failed to start: \(error)"); exit(1)
}
captureEngine.prepare()
do {
    try captureEngine.start()
} catch {
    log("FATAL: capture engine failed to start: \(error)"); exit(1)
}
log("engine started — streaming mic at \(Int(TARGET_RATE)) Hz, ready for playback")

Thread.detachNewThread { stdinLoop() }

RunLoop.main.run()
