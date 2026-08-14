import Foundation
import Security

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

guard CommandLine.arguments.count == 4 else {
    fail("usage: antigravity-keychain <get|set|delete> <service> <account>")
}

let command = CommandLine.arguments[1]
let service = CommandLine.arguments[2]
let account = CommandLine.arguments[3]
let query: [String: Any] = [
    kSecClass as String: kSecClassGenericPassword,
    kSecAttrService as String: service,
    kSecAttrAccount as String: account,
]

switch command {
case "get":
    var lookup = query
    lookup[kSecMatchLimit as String] = kSecMatchLimitOne
    lookup[kSecReturnData as String] = true
    var result: CFTypeRef?
    let status = SecItemCopyMatching(lookup as CFDictionary, &result)
    if status == errSecItemNotFound { exit(44) }
    guard status == errSecSuccess, let data = result as? Data else {
        fail("Keychain read failed: \(status)")
    }
    FileHandle.standardOutput.write(data)

case "set":
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard !data.isEmpty else { fail("Refusing to save an empty Keychain value") }

    // Own KeySwitcher snapshots only. Never rewrite ACL on shared items like
    // service=gemini — that belongs to Antigravity CLI and must stay usable.
    let ownsItem = service.hasPrefix("com.eugene.keyswitcher")

    if ownsItem {
        var access: SecAccess?
        var trustedSelf: SecTrustedApplication?
        let trustedStatus = SecTrustedApplicationCreateFromPath(nil, &trustedSelf)
        if trustedStatus == errSecSuccess, let trustedSelf {
            let createStatus = SecAccessCreate(
                "KeySwitcher Antigravity" as CFString,
                [trustedSelf] as CFArray,
                &access
            )
            if createStatus != errSecSuccess {
                access = nil
            }
        }

        // SecItemUpdate cannot attach a new ACL; replace the item once.
        _ = SecItemDelete(query as CFDictionary)
        var attributes = query
        attributes[kSecValueData as String] = data
        attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        if let access {
            attributes[kSecAttrAccess as String] = access
        }
        let addStatus = SecItemAdd(attributes as CFDictionary, nil)
        guard addStatus == errSecSuccess else { fail("Keychain add failed: \(addStatus)") }
    } else {
        let status = SecItemUpdate(
            query as CFDictionary,
            [kSecValueData as String: data] as CFDictionary
        )
        if status == errSecItemNotFound {
            var attributes = query
            attributes[kSecValueData as String] = data
            attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            let addStatus = SecItemAdd(attributes as CFDictionary, nil)
            guard addStatus == errSecSuccess else { fail("Keychain add failed: \(addStatus)") }
        } else if status != errSecSuccess {
            fail("Keychain update failed: \(status)")
        }
    }

case "delete":
    let status = SecItemDelete(query as CFDictionary)
    guard status == errSecSuccess || status == errSecItemNotFound else {
        fail("Keychain delete failed: \(status)")
    }

default:
    fail("unknown Keychain command: \(command)")
}
