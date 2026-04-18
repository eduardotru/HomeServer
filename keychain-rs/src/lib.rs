/// keychain-rs — Apple Secure Keychain via raw FFI (no external crates).
///
/// Module layout:
///
/// ```
/// ffi/
///   cf.rs        — CoreFoundation raw bindings (CFString, CFData, CFDictionary, …)
///   security.rs  — Security.framework raw bindings (SecItem*, SecKey*, …)
/// cf.rs          — Safe RAII wrappers: CfString, CfData, CfDictBuilder, OwnedCf<T>
/// error.rs       — KeychainError enum
/// keychain.rs    — High-level password CRUD (add / get / update / delete)
/// secure_enclave.rs — SeKey: SE key generation, ECIES encrypt/decrypt
/// ```
pub mod cf;
pub mod error;
pub mod ffi;
pub mod keychain;
pub mod secure_enclave;
