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

    // Create an open access control list allowing any application in the user's
    // session to access the item without prompting. This matches `security add-generic-password -A`
    // and prevents repeated Keychain authorization dialogs when switching accounts.
    var access: SecAccess?
    let accessStatus = SecAccessCreate("Antigravity Credentials" as CFString, nil, &access)
    if accessStatus != errSecSuccess {
        access = nil
    }

    // Try delete + add with open access first to replace any restrictive ACL.
    _ = SecItemDelete(query as CFDictionary)
    var attributes = query
    attributes[kSecValueData as String] = data
    attributes[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    if let access {
        attributes[kSecAttrAccess as String] = access
    }
    let addStatus = SecItemAdd(attributes as CFDictionary, nil)
    if addStatus != errSecSuccess {
        // Fallback to in-place update if delete was refused by an existing owner.
        let updateStatus = SecItemUpdate(
            query as CFDictionary,
            [kSecValueData as String: data] as CFDictionary
        )
        guard updateStatus == errSecSuccess else {
            fail("Keychain write failed (add: \(addStatus), update: \(updateStatus))")
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
