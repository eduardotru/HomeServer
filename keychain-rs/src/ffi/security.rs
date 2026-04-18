/// Raw FFI bindings to Apple's Security framework.
///
/// The Security framework exposes two main subsystems we use:
///
/// 1. **Keychain Services** — generic key-value secret storage backed by the
///    system keychain database.  Items are identified by a "class" (generic
///    password, certificate, key, …) plus a set of attribute key/value pairs.
///
/// 2. **SecKey / Secure Enclave** — asymmetric key generation and use.
///    When `kSecAttrTokenIDSecureEnclave` is specified the private key is
///    generated *inside* the Secure Enclave coprocessor and its raw bits never
///    leave that hardware boundary.
use super::cf::{
    CFAllocatorRef, CFDataRef, CFDictionaryRef, CFErrorRef, CFOptionFlags, CFStringRef, CFTypeRef,
};
use std::os::raw::c_void;

// ── OSStatus ─────────────────────────────────────────────────────────────────
//
// Most Keychain Services functions return an OSStatus (a signed 32-bit int).
// Zero means success; negative values are error codes.

pub type OSStatus = i32;

pub const ERR_SEC_SUCCESS: OSStatus = 0;
pub const ERR_SEC_ITEM_NOT_FOUND: OSStatus = -25300;
pub const ERR_SEC_DUPLICATE_ITEM: OSStatus = -25299;
pub const ERR_SEC_AUTH_FAILED: OSStatus = -25293;
pub const ERR_SEC_USER_CANCELED: OSStatus = -128;
pub const ERR_SEC_PARAM: OSStatus = -50;
pub const ERR_SEC_ALLOCATE: OSStatus = -108;
pub const ERR_SEC_NOT_AVAILABLE: OSStatus = -25291;
pub const ERR_SEC_INTERACTION_NOT_ALLOWED: OSStatus = -25308;

// ── Opaque SecKey / SecAccessControl handles ──────────────────────────────────

pub type SecKeyRef = *const c_void;
pub type SecAccessControlRef = *const c_void;

/// Algorithm identifier — always a CFStringRef under the hood.
pub type SecKeyAlgorithm = CFStringRef;

// ── SecAccessControlCreateFlags ───────────────────────────────────────────────
//
// These are plain compile-time bit-flag constants (CF_OPTIONS / CF_ENUM),
// **not** extern variables.  They control what proof-of-presence is required
// before the Secure Enclave will perform an operation with a protected key.

/// The operation is allowed when the device is unlocked (no biometry required).
pub const SEC_ACCESS_PRIVATE_KEY_USAGE: CFOptionFlags = 1 << 30;

/// Require *any* enrolled biometric (Face ID / Touch ID) — enrollment can change.
pub const SEC_ACCESS_BIOMETRY_ANY: CFOptionFlags = 1 << 1;

/// Require a biometric that was enrolled *at the time the item was created*.
/// Adding a new fingerprint / face invalidates the item.
pub const SEC_ACCESS_BIOMETRY_CURRENT_SET: CFOptionFlags = 1 << 3;

/// Require the device passcode.
pub const SEC_ACCESS_DEVICE_PASSCODE: CFOptionFlags = 1 << 4;

/// Require user presence (biometry OR passcode).
pub const SEC_ACCESS_USER_PRESENCE: CFOptionFlags = 1 << 0;

// ── Extern variable constants ─────────────────────────────────────────────────
//
// These are *runtime* global variables exported by Security.framework.
// They are CFStringRef values (or CFTypeRef for the accessible constants).
// We declare them as `*const c_void` because their internal representation
// is opaque — we only ever use their *addresses* as dictionary keys/values.

#[link(name = "Security", kind = "framework")]
extern "C" {
    // ── Item class ───────────────────────────────────────────────────────────

    /// Dictionary key: what kind of keychain item this is.
    pub static kSecClass: CFStringRef;

    /// Class value: a generic password (service + account + secret blob).
    pub static kSecClassGenericPassword: CFStringRef;

    /// Class value: a cryptographic key.
    pub static kSecClassKey: CFStringRef;

    // ── Generic password attributes ──────────────────────────────────────────

    /// String attribute: the "service" name (analogous to a URL or app bundle ID).
    pub static kSecAttrService: CFStringRef;

    /// String attribute: the "account" name (a username / identifier within the service).
    pub static kSecAttrAccount: CFStringRef;

    /// Data attribute: the secret bytes to store or the retrieved secret bytes.
    pub static kSecValueData: CFStringRef;

    /// Human-readable label for the item (shown in Keychain Access.app).
    pub static kSecAttrLabel: CFStringRef;

    // ── Query modifiers ──────────────────────────────────────────────────────

    /// Boolean query option: include the item data in the result dictionary.
    pub static kSecReturnData: CFStringRef;

    /// Boolean query option: include a SecKeyRef (or other object ref) in the result.
    pub static kSecReturnRef: CFStringRef;

    /// Return a full dictionary of all attributes.
    pub static kSecReturnAttributes: CFStringRef;

    /// How many items to return.
    pub static kSecMatchLimit: CFStringRef;

    /// Value for kSecMatchLimit: return at most one item.
    pub static kSecMatchLimitOne: CFStringRef;

    /// Value for kSecMatchLimit: return all matching items.
    pub static kSecMatchLimitAll: CFStringRef;

    // ── Access control ───────────────────────────────────────────────────────

    /// Attach a SecAccessControlRef to an item, controlling when it can be read.
    pub static kSecAttrAccessControl: CFStringRef;

    /// Accessibility value: accessible while unlocked, on *this device only*
    /// (not synced to iCloud Keychain, not included in backups).
    pub static kSecAttrAccessibleWhenUnlockedThisDeviceOnly: CFTypeRef;

    /// Accessibility value: only accessible when the device is unlocked (may sync).
    pub static kSecAttrAccessibleWhenUnlocked: CFTypeRef;

    // ── Key generation attributes ─────────────────────────────────────────────

    /// The cryptographic algorithm family for a key (EC, RSA, …).
    pub static kSecAttrKeyType: CFStringRef;

    /// Value for kSecAttrKeyType: Elliptic Curve over P-256 (prime256v1).
    /// This is the *only* key type supported by the Secure Enclave.
    pub static kSecAttrKeyTypeECSECPrimeRandom: CFStringRef;

    /// Key size in bits. For Secure Enclave EC keys this must be 256.
    pub static kSecAttrKeySizeInBits: CFStringRef;

    /// Sub-dictionary of attributes to apply to the *private* key half.
    pub static kSecPrivateKeyAttrs: CFStringRef;

    /// Sub-dictionary of attributes to apply to the *public* key half.
    pub static kSecPublicKeyAttrs: CFStringRef;

    /// Whether to store the key in the keychain permanently.
    pub static kSecAttrIsPermanent: CFStringRef;

    /// Token ID attribute — set to kSecAttrTokenIDSecureEnclave to bind the key
    /// to the Secure Enclave hardware.
    pub static kSecAttrTokenID: CFStringRef;

    /// Token ID value meaning "this key lives in the Secure Enclave".
    pub static kSecAttrTokenIDSecureEnclave: CFStringRef;

    /// Application-defined tag for locating a key later (arbitrary bytes).
    pub static kSecAttrApplicationTag: CFStringRef;

    // ── Encryption algorithms ─────────────────────────────────────────────────
    //
    // For Secure Enclave EC keys the usable algorithm is ECIES (integrated
    // encryption scheme).  The variant below uses:
    //   - Cofactor Diffie-Hellman for key agreement
    //   - Variable-length IV
    //   - X9.63 KDF with SHA-256
    //   - AES-GCM for symmetric encryption

    pub static kSecKeyAlgorithmECIESEncryptionCofactorVariableIVX963SHA256AESGCM: SecKeyAlgorithm;

    // ── Keychain CRUD ────────────────────────────────────────────────────────

    /// Add a new item to the keychain.
    /// `result` can be NULL if you don't need the returned value/ref.
    /// Returns ERR_SEC_SUCCESS or an error code.
    pub fn SecItemAdd(attributes: CFDictionaryRef, result: *mut CFTypeRef) -> OSStatus;

    /// Search the keychain.
    /// `result` receives a retained CFTypeRef (CFDataRef for passwords,
    /// SecKeyRef for keys, etc.) — you must CFRelease it.
    pub fn SecItemCopyMatching(query: CFDictionaryRef, result: *mut CFTypeRef) -> OSStatus;

    /// Update items matching `query` with the attributes in `attrs_to_update`.
    pub fn SecItemUpdate(query: CFDictionaryRef, attrs_to_update: CFDictionaryRef) -> OSStatus;

    /// Delete all keychain items matching the query.
    pub fn SecItemDelete(query: CFDictionaryRef) -> OSStatus;

    // ── Key operations ───────────────────────────────────────────────────────

    /// Generate a new cryptographic key pair.
    /// Pass a Secure Enclave token ID to generate inside the SE.
    /// **Create rule** — caller must CFRelease the returned SecKeyRef.
    pub fn SecKeyCreateRandomKey(
        parameters: CFDictionaryRef,
        error: *mut CFErrorRef,
    ) -> SecKeyRef;

    /// Extract the public key from a private key reference.
    /// **Create rule** — caller must CFRelease.
    pub fn SecKeyCopyPublicKey(key: SecKeyRef) -> SecKeyRef;

    /// Encrypt `plaintext` using `public_key` and `algorithm`.
    /// **Create rule** — caller must CFRelease the returned CFDataRef.
    pub fn SecKeyCreateEncryptedData(
        key: SecKeyRef,
        algorithm: SecKeyAlgorithm,
        plaintext: CFDataRef,
        error: *mut CFErrorRef,
    ) -> CFDataRef;

    /// Decrypt `ciphertext` using `private_key` and `algorithm`.
    /// For Secure Enclave keys this operation executes *inside the SE*.
    /// **Create rule** — caller must CFRelease the returned CFDataRef.
    pub fn SecKeyCreateDecryptedData(
        key: SecKeyRef,
        algorithm: SecKeyAlgorithm,
        ciphertext: CFDataRef,
        error: *mut CFErrorRef,
    ) -> CFDataRef;

    /// Check whether `key` supports `algorithm` for the given `operation`.
    /// operation: 0 = sign, 1 = verify, 2 = encrypt, 3 = decrypt, 4 = keyExchange
    pub fn SecKeyIsAlgorithmSupported(
        key: SecKeyRef,
        operation: i32,
        algorithm: SecKeyAlgorithm,
    ) -> bool;

    // ── Access control ───────────────────────────────────────────────────────

    /// Create an access-control object.
    /// `protection` is one of the `kSecAttrAccessible*` constants.
    /// `flags` is a bitwise-OR of SEC_ACCESS_* constants.
    /// **Create rule** — caller must CFRelease.
    pub fn SecAccessControlCreateWithFlags(
        alloc: CFAllocatorRef,
        protection: CFTypeRef,
        flags: CFOptionFlags,
        error: *mut CFErrorRef,
    ) -> SecAccessControlRef;

    // ── Error helpers ────────────────────────────────────────────────────────

    /// Return a human-readable description of an OSStatus error code.
    /// **Copy rule** — caller must CFRelease.
    pub fn SecCopyErrorMessageString(
        status: OSStatus,
        reserved: *const c_void,
    ) -> CFStringRef;
}
