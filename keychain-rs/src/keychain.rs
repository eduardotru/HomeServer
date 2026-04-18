/// High-level Keychain Services API.
///
/// This module wraps the raw `SecItem*` functions into ergonomic Rust.
///
/// # How Keychain items are identified
///
/// A generic password item is uniquely identified by the pair
/// `(kSecAttrService, kSecAttrAccount)`.  Think of `service` as the
/// application / website and `account` as the username.
///
/// # Thread safety
///
/// The underlying Keychain Services are thread-safe; our wrappers inherit that.
use crate::cf::{
    cf_data_to_vec, cf_string_to_string, CfData, CfDictBuilder, CfString, OwnedCf,
    SecAccessControlTag,
};
use crate::error::{KeychainError, Result};
use crate::ffi::cf::{CFErrorRef, CFTypeRef};
use crate::ffi::security::{
    kSecAttrAccount, kSecAttrAccessControl, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    kSecAttrService, kSecClass, kSecClassGenericPassword, kSecMatchLimit,
    kSecMatchLimitOne, kSecReturnData, kSecValueData, SecAccessControlCreateWithFlags,
    SecCopyErrorMessageString, SecItemAdd, SecItemCopyMatching, SecItemDelete, SecItemUpdate,
    ERR_SEC_SUCCESS, SEC_ACCESS_USER_PRESENCE,
};
use crate::ffi::cf::kCFAllocatorDefault;
use std::os::raw::c_void;
use std::ptr;

// ── Internal helpers ──────────────────────────────────────────────────────────

/// Check an `OSStatus` and return a `KeychainError` on failure.
fn check(status: crate::ffi::security::OSStatus) -> Result<()> {
    if status == ERR_SEC_SUCCESS {
        Ok(())
    } else {
        // Ask Security.framework for a human-readable message.
        let msg = unsafe {
            let cf_msg = SecCopyErrorMessageString(status, ptr::null());
            if cf_msg.is_null() {
                format!("OSStatus {status}")
            } else {
                let s = cf_string_to_string(cf_msg).unwrap_or_else(|| format!("{status}"));
                crate::ffi::cf::CFRelease(cf_msg as CFTypeRef);
                s
            }
        };
        eprintln!("[keychain] OSStatus {status}: {msg}");
        Err(KeychainError::from_os_status(status))
    }
}

// ── Public API ────────────────────────────────────────────────────────────────

/// Store a secret in the keychain.
///
/// If an item with the same `(service, account)` pair already exists this
/// returns `Err(KeychainError::DuplicateItem)`.  Use [`update_password`] to
/// change an existing item, or [`upsert_password`] to add-or-update.
pub fn add_password(service: &str, account: &str, secret: &[u8]) -> Result<()> {
    let svc = CfString::new(service);
    let acc = CfString::new(account);
    let data = CfData::from_bytes(secret);

    let mut query = CfDictBuilder::new();
    // SAFETY: all kSec* constants are valid CFStringRefs exported by Security.framework.
    unsafe {
        query
            .set_raw(kSecClass as *const c_void, kSecClassGenericPassword as *const c_void)
            .set_str_key_str_val(kSecAttrService, svc.as_ptr())
            .set_str_key_str_val(kSecAttrAccount, acc.as_ptr())
            .set_data(kSecValueData, data.as_ptr());
    }

    let dict = query.build();
    let status = unsafe { SecItemAdd(dict.as_ptr() as _, ptr::null_mut()) };
    check(status)
}

/// Retrieve a secret from the keychain.
///
/// Returns `Err(KeychainError::NotFound)` if no matching item exists.
pub fn get_password(service: &str, account: &str) -> Result<Vec<u8>> {
    let svc = CfString::new(service);
    let acc = CfString::new(account);

    let mut query = CfDictBuilder::new();
    unsafe {
        query
            .set_raw(kSecClass as *const c_void, kSecClassGenericPassword as *const c_void)
            .set_str_key_str_val(kSecAttrService, svc.as_ptr())
            .set_str_key_str_val(kSecAttrAccount, acc.as_ptr())
            // Ask for the raw data bytes back.
            .set_bool(kSecReturnData, true)
            // Only return the first match (there should only be one anyway).
            .set_raw(kSecMatchLimit as *const c_void, kSecMatchLimitOne as *const c_void);
    }

    let dict = query.build();
    let mut result: CFTypeRef = ptr::null();
    let status = unsafe { SecItemCopyMatching(dict.as_ptr() as _, &mut result) };
    check(status)?;

    // `result` is now a retained CFDataRef (Create rule — we must CFRelease it).
    let bytes = cf_data_to_vec(result as _);
    unsafe { crate::ffi::cf::CFRelease(result) };
    Ok(bytes)
}

/// Update the secret for an existing `(service, account)` item.
///
/// Returns `Err(KeychainError::NotFound)` if no matching item exists.
pub fn update_password(service: &str, account: &str, new_secret: &[u8]) -> Result<()> {
    let svc = CfString::new(service);
    let acc = CfString::new(account);
    let data = CfData::from_bytes(new_secret);

    // `query` identifies which item(s) to update.
    let mut query = CfDictBuilder::new();
    unsafe {
        query
            .set_raw(kSecClass as *const c_void, kSecClassGenericPassword as *const c_void)
            .set_str_key_str_val(kSecAttrService, svc.as_ptr())
            .set_str_key_str_val(kSecAttrAccount, acc.as_ptr());
    }

    // `attrs` contains only the fields to be changed.
    let mut attrs = CfDictBuilder::new();
    unsafe {
        attrs.set_data(kSecValueData, data.as_ptr());
    }

    let q = query.build();
    let a = attrs.build();
    let status = unsafe { SecItemUpdate(q.as_ptr() as _, a.as_ptr() as _) };
    check(status)
}

/// Add or update a secret — convenience wrapper around `add_password` +
/// `update_password`.
pub fn upsert_password(service: &str, account: &str, secret: &[u8]) -> Result<()> {
    match add_password(service, account, secret) {
        Err(KeychainError::DuplicateItem) => update_password(service, account, secret),
        other => other,
    }
}

/// Delete a keychain item.
///
/// Returns `Err(KeychainError::NotFound)` if no matching item exists.
pub fn delete_password(service: &str, account: &str) -> Result<()> {
    let svc = CfString::new(service);
    let acc = CfString::new(account);

    let mut query = CfDictBuilder::new();
    unsafe {
        query
            .set_raw(kSecClass as *const c_void, kSecClassGenericPassword as *const c_void)
            .set_str_key_str_val(kSecAttrService, svc.as_ptr())
            .set_str_key_str_val(kSecAttrAccount, acc.as_ptr());
    }

    let dict = query.build();
    let status = unsafe { SecItemDelete(dict.as_ptr() as _) };
    check(status)
}

/// Store a password protected by user presence (biometry or passcode).
///
/// The item is accessible only while the device is unlocked and a successful
/// Local Authentication challenge has been completed.  On retrieval, the OS
/// will automatically show the biometric prompt.
pub fn add_protected_password(service: &str, account: &str, secret: &[u8]) -> Result<()> {
    let svc = CfString::new(service);
    let acc = CfString::new(account);
    let data = CfData::from_bytes(secret);

    // Build the access control object.
    // kSecAttrAccessibleWhenUnlockedThisDeviceOnly = accessible while unlocked,
    //   not backed up, not synced to other devices.
    // SEC_ACCESS_USER_PRESENCE = biometry OR passcode is required to use the item.
    let mut cf_error: CFErrorRef = ptr::null();
    let acl = unsafe {
        SecAccessControlCreateWithFlags(
            kCFAllocatorDefault,
            kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            SEC_ACCESS_USER_PRESENCE,
            &mut cf_error,
        )
    };
    if acl.is_null() {
        let msg = if !cf_error.is_null() {
            unsafe {
                let desc = crate::ffi::cf::CFErrorCopyDescription(cf_error);
                let s = cf_string_to_string(desc).unwrap_or_default();
                crate::ffi::cf::CFRelease(desc as _);
                crate::ffi::cf::CFRelease(cf_error as _);
                s
            }
        } else {
            "unknown error".into()
        };
        return Err(KeychainError::CfError(msg));
    }
    let acl: OwnedCf<SecAccessControlTag> = unsafe { OwnedCf::from_raw(acl as CFTypeRef) };

    let mut query = CfDictBuilder::new();
    unsafe {
        query
            .set_raw(kSecClass as *const c_void, kSecClassGenericPassword as *const c_void)
            .set_str_key_str_val(kSecAttrService, svc.as_ptr())
            .set_str_key_str_val(kSecAttrAccount, acc.as_ptr())
            .set_data(kSecValueData, data.as_ptr())
            // Attach the access-control object.
            .set_raw(kSecAttrAccessControl as *const c_void, acl.as_ptr() as *const c_void);
    }

    let dict = query.build();
    let status = unsafe { SecItemAdd(dict.as_ptr() as _, ptr::null_mut()) };
    check(status)
}
