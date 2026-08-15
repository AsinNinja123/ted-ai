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

        let process = Process()
        process.executableURL = python
        process.arguments = ["-u", script.path]
        process.currentDirectoryURL = project
        var environment = ProcessInfo.processInfo.environment
        environment["TED_NATIVE_HOST"] = "1"
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
        try? logHandle?.close()
    }
}

let app = NSApplication.shared
let delegate = TedDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
