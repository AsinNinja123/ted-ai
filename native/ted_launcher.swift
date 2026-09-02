import AppKit
import Darwin
import Foundation

/// Native host for Ted's Python runtime.
///
/// Launching a shell script which then starts framework Python makes AppKit
/// register the visible window as "Python", so the Dock puts its running dot
/// under Python's icon. This process remains the real, regular macOS app while
/// the Python child runs as an accessory UI process. Clicking Ted in the Dock
/// activates the child's pywebview window.
final class TedDelegate: NSObject, NSApplicationDelegate {
    private var child: Process?
    private var logHandle: FileHandle?
    private var controlTimer: Timer?
    private var controlDirectory: URL?
    private var shuttingDown = false

    private var projectURL: URL {
        Bundle.main.bundleURL.deletingLastPathComponent()
    }

    private func activateChild(after delay: TimeInterval) {
        guard let child, child.isRunning else { return }
        let pid = child.processIdentifier
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            // The accessory child owns Ted's actual pywebview window. Ask the
            // child to raise that window from its own AppKit process; trying to
            // activate an accessory through LaunchServices leaves this empty
            // Dock host frontmost instead.
            NSApp.hide(nil)
            Darwin.kill(pid, SIGUSR1)
        }
    }

    /// Keep Accessibility work inside the process macOS actually trusts.
    private func startControlBridge() -> URL? {
        let directory = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("ted-control-\(getpid())", isDirectory: true)
        do {
            try FileManager.default.createDirectory(
                at: directory, withIntermediateDirectories: false,
                attributes: [.posixPermissions: 0o700])
        } catch {
            return nil
        }
        controlDirectory = directory
        controlTimer = Timer.scheduledTimer(withTimeInterval: 0.025, repeats: true) {
            [weak self] _ in self?.processControlRequest()
        }
        return directory
    }

    private func processControlRequest() {
        guard let directory = controlDirectory,
              let files = try? FileManager.default.contentsOfDirectory(
                at: directory, includingPropertiesForKeys: nil,
                options: [.skipsHiddenFiles]),
              let request = files.first(where: {
                  $0.lastPathComponent.hasSuffix(".request.json")
              }) else { return }

        let response = directory.appendingPathComponent(
            request.lastPathComponent.replacingOccurrences(
                of: ".request.json", with: ".response.json"))
        var result: [String: Any] = [
            "ok": false, "error": "Invalid native control request"]
        if let data = try? Data(contentsOf: request),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let arguments = object["args"] as? [String], !arguments.isEmpty {
            result = tedControlResult(
                [Bundle.main.executablePath ?? "Ted"] + arguments)
        }
        if let data = try? JSONSerialization.data(withJSONObject: result) {
            try? data.write(to: response, options: .atomic)
        }
        try? FileManager.default.removeItem(at: request)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let project = projectURL
        let python = project.appendingPathComponent("venv/bin/python")
        let script = project.appendingPathComponent("hud.py")
        let log = project.appendingPathComponent("data/ted_launch.log")

        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: script.path) else {
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "Ted couldn't start"
            alert.informativeText = "The Python environment or hud.py is missing from \(project.path)."
            alert.runModal()
            NSApp.terminate(nil)
            return
        }

        try? FileManager.default.createDirectory(
            at: log.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: log.path) {
            FileManager.default.createFile(atPath: log.path, contents: nil)
        }
        logHandle = FileHandle(forWritingAtPath: log.path)
        _ = try? logHandle?.seekToEnd()
        let stamp = ISO8601DateFormatter().string(from: Date())
        if let bytes = "=== \(stamp) — launching from native Ted.app ===\n".data(using: .utf8) {
            try? logHandle?.write(contentsOf: bytes)
        }

        guard let controlBridge = startControlBridge() else {
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "Ted couldn't start native control"
            alert.informativeText = "The private control bridge could not be created."
            alert.runModal()
            NSApp.terminate(nil)
            return
        }

        let process = Process()
        process.executableURL = python
        process.arguments = ["-u", script.path]
        process.currentDirectoryURL = project
        var environment = ProcessInfo.processInfo.environment
        environment["TED_NATIVE_HOST"] = "1"
        environment["TED_CONTROL_IPC"] = controlBridge.path
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        process.standardOutput = logHandle
        process.standardError = logHandle
        process.terminationHandler = { proc in
            DispatchQueue.main.async {
                if proc.terminationStatus != 0 && !self.shuttingDown {
                    let alert = NSAlert()
                    alert.alertStyle = .critical
                    alert.messageText = "Ted exited unexpectedly"
                    alert.informativeText = "See data/ted_launch.log for details."
                    alert.runModal()
                }
                NSApp.terminate(nil)
            }
        }
        do {
            try process.run()
            child = process
            activateChild(after: 2.0)
            activateChild(after: 5.0)
            activateChild(after: 10.0)
        } catch {
            let alert = NSAlert()
            alert.alertStyle = .critical
            alert.messageText = "Ted couldn't start"
            alert.informativeText = error.localizedDescription
            alert.runModal()
            NSApp.terminate(nil)
        }
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication, hasVisibleWindows flag: Bool
    ) -> Bool {
        activateChild(after: 0.15)
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let child, child.isRunning else { return .terminateNow }
        if shuttingDown { return .terminateLater }
        shuttingDown = true
        child.terminate()
        DispatchQueue.global(qos: .utility).async {
            child.waitUntilExit()
            DispatchQueue.main.async {
                NSApp.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let child, child.isRunning { child.terminate() }
        controlTimer?.invalidate()
        if let controlDirectory {
            try? FileManager.default.removeItem(at: controlDirectory)
        }
        try? logHandle?.close()
    }
}

@main
struct TedApplication {
    static func main() {
        if CommandLine.arguments.count > 1 && CommandLine.arguments[1] == "--control" {
            let controlArguments = [CommandLine.arguments[0]]
                + Array(CommandLine.arguments.dropFirst(2))
            runTedControl(controlArguments)
        }

        let app = NSApplication.shared
        let delegate = TedDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
