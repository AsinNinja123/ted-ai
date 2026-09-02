import AppKit
import ApplicationServices
import Foundation

// Small, auditable macOS control bridge. It performs one Accessibility action
// per process and reports structured ground truth to Python on stdout.

func emit(_ object: [String: Any]) -> Never {
    let data = try! JSONSerialization.data(withJSONObject: object, options: [])
    print(String(data: data, encoding: .utf8)!)
    exit((object["ok"] as? Bool) == true ? 0 : 1)
}

func trusted(prompt: Bool = false) -> Bool {
    let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
    return AXIsProcessTrustedWithOptions([key: prompt] as CFDictionary)
}

func attribute(_ element: AXUIElement, _ name: CFString) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, name, &value) == .success ? value : nil
}

func stringAttribute(_ element: AXUIElement, _ name: CFString) -> String {
    guard let value = attribute(element, name) else { return "" }
    if let text = value as? String { return text }
    if let number = value as? NSNumber { return number.stringValue }
    return ""
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    return attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] ?? []
}

func actions(_ element: AXUIElement) -> [String] {
    var names: CFArray?
    guard AXUIElementCopyActionNames(element, &names) == .success else { return [] }
    return names as? [String] ?? []
}

func frontmostRoot() -> (AXUIElement, String)? {
    guard let app = NSWorkspace.shared.frontmostApplication else { return nil }
    return (AXUIElementCreateApplication(app.processIdentifier), app.localizedName ?? "Unknown")
}

func focusedElement() -> AXUIElement? {
    let system = AXUIElementCreateSystemWide()
    return attribute(system, kAXFocusedUIElementAttribute as CFString) as! AXUIElement?
}

struct Candidate {
    let element: AXUIElement
    let role: String
    let title: String
    let detail: String
    let score: Int
}

func describe(_ element: AXUIElement) -> (String, String, String) {
    let role = stringAttribute(element, kAXRoleAttribute as CFString)
    let title = stringAttribute(element, kAXTitleAttribute as CFString)
    let desc = stringAttribute(element, kAXDescriptionAttribute as CFString)
    let value = stringAttribute(element, kAXValueAttribute as CFString)
    let identifier = stringAttribute(element, kAXIdentifierAttribute as CFString)
    let detail = [title, desc, value, identifier].filter { !$0.isEmpty }.joined(separator: " · ")
    return (role, title.isEmpty ? (desc.isEmpty ? value : desc) : title, detail)
}

func walk(_ root: AXUIElement, limit: Int = 1800) -> [AXUIElement] {
    var queue = [root]
    var out: [AXUIElement] = []
    var index = 0
    while index < queue.count && out.count < limit {
        let current = queue[index]
        index += 1
        out.append(current)
        queue.append(contentsOf: children(current))
    }
    return out
}

func wordTokens(_ text: String) -> [String] {
    return text.lowercased().split { !$0.isLetter && !$0.isNumber }.map(String.init)
}

func containsPhrase(_ haystack: [String], _ needle: [String]) -> Bool {
    guard !needle.isEmpty, haystack.count >= needle.count else { return false }
    if needle.count == 1 { return haystack.contains(needle[0]) }
    for start in 0...(haystack.count - needle.count) {
        if Array(haystack[start..<(start + needle.count)]) == needle { return true }
    }
    return false
}

func bestMatch(_ root: AXUIElement, target: String, mustPress: Bool) -> Candidate? {
    let needle = target.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
    let needleTokens = wordTokens(needle)
    let words = Set(needleTokens)
    if words.isEmpty { return nil }
    var best: Candidate?
    for element in walk(root) {
        let available = actions(element)
        if mustPress && !available.contains(kAXPressAction as String) { continue }
        let (role, title, detail) = describe(element)
        let haystack = detail.lowercased()
        if haystack.isEmpty { continue }
        let haystackTokens = wordTokens(haystack)
        var score = 0
        if haystack == needle || title.lowercased() == needle { score = 120 }
        else if containsPhrase(haystackTokens, needleTokens) { score = 90 }
        else {
            let availableWords = Set(haystackTokens)
            let hits = words.filter { availableWords.contains($0) }.count
            if hits == 0 { continue }
            score = hits * 15 - max(0, words.count - hits) * 4
        }
        if role.contains("Button") || role.contains("Link") || role.contains("MenuItem") {
            score += 8
        }
        if best == nil || score > best!.score {
            best = Candidate(element: element, role: role, title: title, detail: detail, score: score)
        }
    }
    return best
}

func attributeSettable(_ element: AXUIElement, _ name: CFString) -> Bool {
    var settable = DarwinBoolean(false)
    return AXUIElementIsAttributeSettable(element, name, &settable) == .success
        && settable.boolValue
}

func bestEditableMatch(_ root: AXUIElement, target: String) -> Candidate? {
    let needle = target.lowercased().trimmingCharacters(in: .whitespacesAndNewlines)
    let needleTokens = wordTokens(needle)
    let words = Set(needleTokens)
    if words.isEmpty { return nil }
    var best: Candidate?
    for element in walk(root) {
        let (role, title, detail) = describe(element)
        let editableRole = role.contains("TextField") || role.contains("TextArea")
            || role.contains("ComboBox") || role.contains("SearchField")
        if !editableRole && !attributeSettable(element, kAXValueAttribute as CFString) {
            continue
        }
        let haystack = detail.lowercased()
        if haystack.isEmpty { continue }
        let haystackTokens = wordTokens(haystack)
        var score = 0
        if haystack == needle || title.lowercased() == needle { score = 130 }
        else if containsPhrase(haystackTokens, needleTokens) { score = 100 }
        else {
            let availableWords = Set(haystackTokens)
            let hits = words.filter { availableWords.contains($0) }.count
            if hits == 0 { continue }
            score = hits * 18 - max(0, words.count - hits) * 4
        }
        if editableRole { score += 12 }
        if best == nil || score > best!.score {
            best = Candidate(element: element, role: role, title: title,
                             detail: detail, score: score)
        }
    }
    return best
}

func unicodeType(_ text: String) -> Bool {
    guard let source = CGEventSource(stateID: .hidSystemState),
          let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
          let up = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false) else {
        return false
    }
    let units = Array(text.utf16)
    units.withUnsafeBufferPointer { buffer in
        down.keyboardSetUnicodeString(stringLength: units.count, unicodeString: buffer.baseAddress!)
        up.keyboardSetUnicodeString(stringLength: units.count, unicodeString: buffer.baseAddress!)
    }
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    return true
}

func elementFrame(_ element: AXUIElement) -> CGRect? {
    guard let positionValue = attribute(element, kAXPositionAttribute as CFString),
          let sizeValue = attribute(element, kAXSizeAttribute as CFString),
          CFGetTypeID(positionValue) == AXValueGetTypeID(),
          CFGetTypeID(sizeValue) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(positionValue as! AXValue, .cgPoint, &point),
          AXValueGetValue(sizeValue as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}

func elementOrAncestorFrame(_ element: AXUIElement, levels: Int = 5) -> CGRect? {
    var current = element
    for _ in 0..<levels {
        if let frame = elementFrame(current), frame.width > 4, frame.height > 4 {
            return frame
        }
        guard let parent = attribute(current, kAXParentAttribute as CFString),
              CFGetTypeID(parent) == AXUIElementGetTypeID() else { break }
        current = parent as! AXUIElement
    }
    return nil
}

func ancestorWindow(_ element: AXUIElement, levels: Int = 14) -> AXUIElement? {
    var current = element
    for _ in 0..<levels {
        if stringAttribute(current, kAXRoleAttribute as CFString) == (kAXWindowRole as String) {
            return current
        }
        guard let parent = attribute(current, kAXParentAttribute as CFString),
              CFGetTypeID(parent) == AXUIElementGetTypeID() else { break }
        current = parent as! AXUIElement
    }
    return nil
}

func clickPoint(_ point: CGPoint) -> Bool {
    guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                             mouseCursorPosition: point, mouseButton: .left),
          let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                           mouseCursorPosition: point, mouseButton: .left) else { return false }
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    return true
}

func postKey(_ code: CGKeyCode, flags: CGEventFlags = []) -> Bool {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
        return false
    }
    down.flags = flags; up.flags = flags
    down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
    return true
}

func runTedControl(_ args: [String]) -> Never {
guard args.count >= 2 else { emit(["ok": false, "error": "missing command"]) }
let command = args[1]

if command == "status" {
    let prompt = args.count > 2 && args[2] == "prompt"
    emit(["ok": trusted(prompt: prompt),
          "trusted": trusted(prompt: false),
          "frontmost": NSWorkspace.shared.frontmostApplication?.localizedName ?? "Unknown"])
}

guard trusted(prompt: false) else {
    emit(["ok": false, "error": "Accessibility permission is not enabled for Ted"])
}
guard let (root, appName) = frontmostRoot() else {
    emit(["ok": false, "error": "No frontmost application"])
}

switch command {
case "snapshot":
    let query = args.count > 2 ? args[2].lowercased() : ""
    var items: [[String: String]] = []
    for element in walk(root) {
        let (role, title, detail) = describe(element)
        guard !detail.isEmpty else { continue }
        if !query.isEmpty && !detail.lowercased().contains(query) { continue }
        if query.isEmpty && role.contains("Menu") { continue }
        if title.isEmpty && !role.contains("Button") && !role.contains("Link") && !role.contains("Text") { continue }
        items.append(["role": role, "name": title, "detail": detail])
        if items.count >= 80 { break }
    }
    emit(["ok": true, "app": appName, "elements": items])

case "press":
    guard args.count > 2 else { emit(["ok": false, "error": "missing target"]) }
    guard let found = bestMatch(root, target: args[2], mustPress: true) else {
        emit(["ok": false, "error": "No accessible control matched '\(args[2])'", "app": appName])
    }
    let error = AXUIElementPerformAction(found.element, kAXPressAction as CFString)
    emit(["ok": error == .success, "app": appName, "matched": found.detail,
          "role": found.role, "error": error == .success ? "" : "Accessibility press failed (\(error.rawValue))"])

case "fill":
    guard args.count > 3 else { emit(["ok": false, "error": "missing field label or text"]) }
    guard let found = bestEditableMatch(root, target: args[2]) else {
        emit(["ok": false, "error": "No accessible editable field matched '\(args[2])'", "app": appName])
    }
    let focused = AXUIElementSetAttributeValue(
        found.element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    let set = AXUIElementSetAttributeValue(
        found.element, kAXValueAttribute as CFString, args[3] as CFTypeRef)
    let after = stringAttribute(found.element, kAXValueAttribute as CFString)
    let ok = set == .success && after == args[3]
    emit(["ok": ok, "verified": ok, "app": appName, "matched": found.detail,
          "role": found.role,
          "error": ok ? "" : "Accessibility field update failed (focus \(focused.rawValue), value \(set.rawValue))"])

case "focus":
    guard args.count > 2 else { emit(["ok": false, "error": "missing target"]) }
    guard let found = bestMatch(root, target: args[2], mustPress: false) else {
        emit(["ok": false, "error": "No accessible element matched '\(args[2])'", "app": appName])
    }
    let error = AXUIElementSetAttributeValue(
        found.element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    // Chromium exposes rich editors as AXTextArea but does not always move the
    // real keyboard caret when AXFocused is set. A semantic center click uses
    // that element's accessibility geometry—still no screenshot or guessing.
    var clicked = false
    var clickX = 0.0
    var clickY = 0.0
    if error == .success,
       (found.role.contains("TextArea") || found.role.contains("WebArea")),
       let frame = elementOrAncestorFrame(found.element) {
        if let window = ancestorWindow(found.element) {
            AXUIElementPerformAction(window, kAXRaiseAction as CFString)
            usleep(100_000)
        }
        clickX = frame.midX; clickY = frame.midY
        clicked = clickPoint(CGPoint(x: clickX, y: clickY))
        usleep(120_000)
    }
    emit(["ok": error == .success && (!found.role.contains("TextArea") || clicked),
          "app": appName, "matched": found.detail, "clicked": clicked,
          "x": clickX, "y": clickY,
          "role": found.role,
          "error": error != .success ? "Accessibility focus failed (\(error.rawValue))" :
              (clicked ? "" : "Accessibility element has no clickable frame")])

case "type-text":
    guard args.count > 2 else { emit(["ok": false, "error": "missing text"]) }
    let focused = focusedElement()
    let before = focused.map { stringAttribute($0, kAXValueAttribute as CFString) } ?? ""
    let sent = unicodeType(args[2])
    usleep(160_000)
    let after = focused.map { stringAttribute($0, kAXValueAttribute as CFString) } ?? ""
    emit(["ok": sent, "verified": before != after,
          "app": appName,
          "error": "Could not create keyboard events"])

case "paste-text":
    guard args.count > 2 else { emit(["ok": false, "error": "missing text"]) }
    let board = NSPasteboard.general
    var saved: [[NSPasteboard.PasteboardType: Data]] = []
    for item in board.pasteboardItems ?? [] {
        var copy: [NSPasteboard.PasteboardType: Data] = [:]
        for type in item.types {
            if let data = item.data(forType: type) { copy[type] = data }
        }
        saved.append(copy)
    }
    board.clearContents()
    board.setString(args[2], forType: .string)
    let sent = postKey(9, flags: .maskCommand)
    usleep(300_000)
    // Canvas editors such as Google Docs do not publish their text through
    // AXValue. Verify the outcome the same way a person would: select the
    // editor contents, copy them, and compare. Collapse the selection back at
    // the end before restoring every representation of Charlie's clipboard.
    var verified = false
    if sent && postKey(0, flags: .maskCommand) {
        usleep(80_000)
        if postKey(8, flags: .maskCommand) {
            usleep(180_000)
            let copied = board.string(forType: .string) ?? ""
            verified = copied.contains(args[2])
        }
        _ = postKey(124)
        usleep(60_000)
    }
    board.clearContents()
    let restored: [NSPasteboardItem] = saved.map { values in
        let item = NSPasteboardItem()
        for (type, data) in values { item.setData(data, forType: type) }
        return item
    }
    if !restored.isEmpty { board.writeObjects(restored) }
    emit(["ok": sent, "verified": verified, "app": appName,
          "error": sent ? "" : "Could not create paste shortcut"])

case "key":
    guard args.count > 2 else { emit(["ok": false, "error": "missing key"]) }
    let map: [String: CGKeyCode] = ["return": 36, "enter": 36, "tab": 48,
        "space": 49, "delete": 51, "backspace": 51, "escape": 53,
        "left": 123, "right": 124, "down": 125, "up": 126]
    let shortcuts: [String: (CGKeyCode, CGEventFlags)] = [
        "copy": (8, .maskCommand), "paste": (9, .maskCommand),
        "cut": (7, .maskCommand), "undo": (6, .maskCommand),
        "redo": (6, [.maskCommand, .maskShift]), "select all": (0, .maskCommand),
        "save": (1, .maskCommand), "new": (45, .maskCommand),
        // Google Docs' official Mac shortcut for Tool finder (formerly menu search).
        "tool finder": (44, .maskAlternate)]
    let name = args[2].lowercased()
    let spec = shortcuts[name] ?? map[name].map { ($0, CGEventFlags()) }
    guard let (code, flags) = spec else {
        emit(["ok": false, "error": "Unknown key '\(args[2])'"])
    }
    emit(["ok": postKey(code, flags: flags), "app": appName, "key": name])

case "scroll":
    guard args.count > 2, let amount = Int32(args[2]) else {
        emit(["ok": false, "error": "missing scroll amount"])
    }
    guard let event = CGEvent(scrollWheelEvent2Source: nil, units: .pixel,
                              wheelCount: 1, wheel1: amount, wheel2: 0, wheel3: 0) else {
        emit(["ok": false, "error": "Could not create scroll event"])
    }
    event.post(tap: .cghidEventTap)
    emit(["ok": true, "app": appName, "amount": amount])

case "click":
    guard args.count > 3, let x = Double(args[2]), let y = Double(args[3]) else {
        emit(["ok": false, "error": "missing click coordinates"])
    }
    let point = CGPoint(x: x, y: y)
    guard clickPoint(point) else {
        emit(["ok": false, "error": "Could not create mouse events"])
    }
    emit(["ok": true, "app": appName, "x": x, "y": y])

default:
    emit(["ok": false, "error": "unknown command '\(command)'"])
}
}
