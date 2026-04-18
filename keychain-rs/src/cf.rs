/// Safe, RAII wrappers around CoreFoundation types.
///
/// # Memory model
///
/// Every CF object is reference-counted.  When you receive one via a Create/Copy
/// function you *own* it and must call `CFRelease` when done.  `OwnedCf` implements
/// `Drop` to do that automatically, mirroring what `Box<T>` does for heap memory.
///
/// # Design
///
/// `OwnedCf` stores the pointer as the universal `CFTypeRef` (`*const c_void`).
/// A phantom type parameter `Tag` carries the logical CF type so the compiler
/// keeps `OwnedCf<CfStringTag>` and `OwnedCf<CfDataTag>` distinct.
/// Typed wrappers (`CfString`, `CfData`, …) provide ergonomic methods on top.
use crate::ffi::cf::{
    kCFAllocatorDefault, kCFBooleanFalse, kCFBooleanTrue, CFBooleanRef, CFDataRef,
    CFDictionaryRef, CFIndex, CFMutableDictionaryRef, CFNumberRef, CFStringRef, CFTypeRef,
    K_CF_NUMBER_SINT32_TYPE, K_CF_STRING_ENCODING_UTF8, K_CF_TYPE_DICT_KEY_CBS,
    K_CF_TYPE_DICT_VAL_CBS,
};
use crate::ffi::cf::{
    CFDataCreate, CFDataGetBytePtr, CFDataGetLength, CFDictionaryCreateMutable,
    CFDictionarySetValue, CFNumberCreate, CFRelease, CFStringCreateWithCString,
    CFStringGetCString, CFStringGetCStringPtr, CFStringGetLength,
    CFStringGetMaximumSizeForEncoding,
};
use std::ffi::CString;
use std::marker::PhantomData;
use std::os::raw::c_void;

// ── OwnedCf<Tag> ──────────────────────────────────────────────────────────────

/// RAII owner for any CoreFoundation object.
///
/// `Tag` is a zero-sized marker type that records the *logical* CF type
/// (e.g. `CfStringTag`, `CfDataTag`).  At runtime the value is always stored
/// as a `CFTypeRef` (`*const c_void`) — identical to how CF stores it internally.
///
/// # Safety invariant
/// `raw` must be a valid, non-null CF object obtained via a Create/Copy rule.
pub struct OwnedCf<Tag> {
    raw: CFTypeRef,
    _tag: PhantomData<*mut Tag>, // invariant over Tag; !Send/!Sync by default (CF objects are)
}

// CF objects can be sent between threads; CF's refcount is atomic.
unsafe impl<Tag> Send for OwnedCf<Tag> {}
unsafe impl<Tag> Sync for OwnedCf<Tag> {}

impl<Tag> OwnedCf<Tag> {
    /// Wrap a raw CF pointer, transferring ownership.
    ///
    /// # Safety
    /// `raw` must be a non-null CF object from a Create/Copy function.
    pub unsafe fn from_raw(raw: CFTypeRef) -> Self {
        debug_assert!(!raw.is_null(), "OwnedCf::from_raw: received NULL pointer");
        Self {
            raw,
            _tag: PhantomData,
        }
    }

    /// Borrow the underlying `CFTypeRef` without releasing ownership.
    pub fn as_cf_type_ref(&self) -> CFTypeRef {
        self.raw
    }

    /// Return the raw pointer as `*const c_void`.
    ///
    /// Since every CF type is `*const c_void` under the hood, this is always
    /// the right type to pass into CF/Security API calls.
    pub fn as_ptr(&self) -> *const c_void {
        self.raw
    }

    /// Consume the wrapper without calling `CFRelease`.
    /// The caller is now responsible for memory management.
    pub fn into_raw(self) -> CFTypeRef {
        let raw = self.raw;
        std::mem::forget(self);
        raw
    }
}

impl<Tag> Drop for OwnedCf<Tag> {
    fn drop(&mut self) {
        // SAFETY: raw is a valid owned CF object (invariant upheld at construction).
        unsafe { CFRelease(self.raw) }
    }
}

// ── Marker types ──────────────────────────────────────────────────────────────
//
// These are zero-sized types used purely as type-level tags.
// They never exist at runtime.

pub enum CfStringTag {}
pub enum CfDataTag {}
pub enum CfNumberTag {}
pub enum CfDictionaryTag {}
pub enum SecKeyTag {}
pub enum SecAccessControlTag {}

// ── CfString ──────────────────────────────────────────────────────────────────

/// An owned `CFString`.
pub struct CfString(OwnedCf<CfStringTag>);

impl CfString {
    /// Create a `CFString` from a Rust `&str` (copied into CF memory).
    pub fn new(s: &str) -> Self {
        let c = CString::new(s).expect("CfString::new: input contains interior NUL");
        // SAFETY: allocator is always valid; c.as_ptr() is a valid NUL-terminated UTF-8 string.
        let raw = unsafe {
            CFStringCreateWithCString(kCFAllocatorDefault, c.as_ptr(), K_CF_STRING_ENCODING_UTF8)
        };
        assert!(!raw.is_null(), "CFStringCreateWithCString returned NULL");
        CfString(unsafe { OwnedCf::from_raw(raw as CFTypeRef) })
    }

    /// Return the raw `CFStringRef` for use in CF/Security API calls.
    pub fn as_ptr(&self) -> CFStringRef {
        self.0.as_cf_type_ref() as CFStringRef
    }

    /// Convert to a Rust `String`, returning `None` on failure.
    pub fn to_string(&self) -> Option<String> {
        cf_string_to_string(self.as_ptr())
    }
}

/// Convert any borrowed `CFStringRef` to a `String` without taking ownership.
pub fn cf_string_to_string(s: CFStringRef) -> Option<String> {
    if s.is_null() {
        return None;
    }
    // SAFETY: `s` is a valid CFStringRef (caller's responsibility).
    unsafe {
        // Fast path: the string may already be stored as a C UTF-8 array internally.
        let fast = CFStringGetCStringPtr(s, K_CF_STRING_ENCODING_UTF8);
        if !fast.is_null() {
            return std::ffi::CStr::from_ptr(fast).to_str().ok().map(String::from);
        }
        // Slow path: allocate a buffer large enough for the worst case.
        let len = CFStringGetLength(s);
        let max_size = CFStringGetMaximumSizeForEncoding(len, K_CF_STRING_ENCODING_UTF8) + 1;
        let mut buf: Vec<u8> = vec![0u8; max_size as usize];
        let ok = CFStringGetCString(
            s,
            buf.as_mut_ptr() as *mut i8,
            max_size,
            K_CF_STRING_ENCODING_UTF8,
        );
        if ok {
            std::ffi::CStr::from_ptr(buf.as_ptr() as *const i8)
                .to_str()
                .ok()
                .map(String::from)
        } else {
            None
        }
    }
}

// ── CfData ────────────────────────────────────────────────────────────────────

/// An owned `CFData`.
pub struct CfData(OwnedCf<CfDataTag>);

impl CfData {
    /// Create a `CFData` by copying the given byte slice.
    pub fn from_bytes(bytes: &[u8]) -> Self {
        // SAFETY: bytes is a valid slice; length fits in CFIndex (i64).
        let raw = unsafe {
            CFDataCreate(kCFAllocatorDefault, bytes.as_ptr(), bytes.len() as CFIndex)
        };
        assert!(!raw.is_null(), "CFDataCreate returned NULL");
        CfData(unsafe { OwnedCf::from_raw(raw as CFTypeRef) })
    }

    pub fn as_ptr(&self) -> CFDataRef {
        self.0.as_cf_type_ref() as CFDataRef
    }

    /// Copy the bytes out into a `Vec<u8>`.
    pub fn to_vec(&self) -> Vec<u8> {
        cf_data_to_vec(self.as_ptr())
    }
}

/// Copy bytes from any borrowed `CFDataRef` without taking ownership.
pub fn cf_data_to_vec(data: CFDataRef) -> Vec<u8> {
    if data.is_null() {
        return Vec::new();
    }
    // SAFETY: data is a valid CFDataRef.
    unsafe {
        let len = CFDataGetLength(data) as usize;
        let ptr = CFDataGetBytePtr(data);
        std::slice::from_raw_parts(ptr, len).to_vec()
    }
}

// ── CfNumber ──────────────────────────────────────────────────────────────────

/// An owned `CFNumber` wrapping a 32-bit integer.
pub struct CfNumber(OwnedCf<CfNumberTag>);

impl CfNumber {
    pub fn from_i32(n: i32) -> Self {
        // SAFETY: &n is a valid pointer to an i32; K_CF_NUMBER_SINT32_TYPE matches.
        let raw = unsafe {
            CFNumberCreate(
                kCFAllocatorDefault,
                K_CF_NUMBER_SINT32_TYPE,
                &n as *const i32 as *const c_void,
            )
        };
        assert!(!raw.is_null(), "CFNumberCreate returned NULL");
        CfNumber(unsafe { OwnedCf::from_raw(raw as CFTypeRef) })
    }

    pub fn as_ptr(&self) -> CFNumberRef {
        self.0.as_cf_type_ref() as CFNumberRef
    }
}

// ── CfDictBuilder ─────────────────────────────────────────────────────────────

/// A builder for `CFMutableDictionary`.
///
/// Calling `.set_*()` adds key/value pairs; `.build()` finalises the dictionary
/// and hands back an `OwnedCf<CfDictionaryTag>` that will `CFRelease` on drop.
///
/// The dictionary uses the standard CF "type" callbacks, which retain each
/// inserted value, so every value outlives the dictionary.
pub struct CfDictBuilder {
    // CFMutableDictionaryRef = *mut c_void.  We store it as *mut c_void here
    // and cast to *const c_void for CFRelease (a safe narrowing).
    dict: CFMutableDictionaryRef,
}

impl CfDictBuilder {
    pub fn new() -> Self {
        // SAFETY: kCFAllocatorDefault and the standard callbacks are always valid.
        let dict = unsafe {
            CFDictionaryCreateMutable(
                kCFAllocatorDefault,
                0, // capacity hint: 0 = let CF decide
                &K_CF_TYPE_DICT_KEY_CBS as *const _ as *const c_void,
                &K_CF_TYPE_DICT_VAL_CBS as *const _ as *const c_void,
            )
        };
        assert!(!dict.is_null(), "CFDictionaryCreateMutable returned NULL");
        CfDictBuilder { dict }
    }

    // ── Internal ─────────────────────────────────────────────────────────────

    fn set_kv(&mut self, key: *const c_void, value: *const c_void) -> &mut Self {
        // SAFETY: dict is valid; key/value are valid CF objects retained by the dict.
        unsafe { CFDictionarySetValue(self.dict, key, value) };
        self
    }

    // ── Typed setters ─────────────────────────────────────────────────────────

    /// Set a CFStringRef key → CFStringRef value.
    pub fn set_str_key_str_val(&mut self, key: CFStringRef, value: CFStringRef) -> &mut Self {
        self.set_kv(key as _, value as _)
    }

    /// Set a CFStringRef key → CFBooleanRef value.
    pub fn set_bool(&mut self, key: CFStringRef, value: bool) -> &mut Self {
        // SAFETY: kCFBooleanTrue / kCFBooleanFalse are immortal global singletons.
        let bool_ref: CFBooleanRef =
            if value { unsafe { kCFBooleanTrue } } else { unsafe { kCFBooleanFalse } };
        self.set_kv(key as _, bool_ref as _)
    }

    /// Set a CFStringRef key → CFNumberRef value.
    pub fn set_number(&mut self, key: CFStringRef, value: CFNumberRef) -> &mut Self {
        self.set_kv(key as _, value as _)
    }

    /// Set a CFStringRef key → CFDataRef value.
    pub fn set_data(&mut self, key: CFStringRef, value: CFDataRef) -> &mut Self {
        self.set_kv(key as _, value as _)
    }

    /// Set a CFStringRef key → sub-dictionary (CFDictionaryRef) value.
    pub fn set_dict(&mut self, key: CFStringRef, value: CFDictionaryRef) -> &mut Self {
        self.set_kv(key as _, value as _)
    }

    /// Set a raw (key, value) pair — both typed as `*const c_void`.
    ///
    /// Use this for CF globals (kSecClass, kSecClassGenericPassword, …) which
    /// are already `CFStringRef = *const c_void`.
    pub fn set_raw(&mut self, key: *const c_void, value: *const c_void) -> &mut Self {
        self.set_kv(key, value)
    }

    // ── Finalise ─────────────────────────────────────────────────────────────

    /// Consume the builder and return an owned `CFDictionaryRef`.
    /// The mutable dict is safe to use as a `CFDictionaryRef` (it IS one).
    pub fn build(self) -> OwnedCf<CfDictionaryTag> {
        let dict = self.dict;
        std::mem::forget(self); // prevent Drop from double-releasing
        // Cast *mut c_void → *const c_void: always safe (narrowing).
        unsafe { OwnedCf::from_raw(dict as CFTypeRef) }
    }
}

impl Drop for CfDictBuilder {
    // Only reached if `build()` was never called (unusual but must be handled).
    fn drop(&mut self) {
        unsafe { CFRelease(self.dict as CFTypeRef) }
    }
}

// ── Convenience: get the CFDictionaryRef pointer from a built dictionary ──────

impl OwnedCf<CfDictionaryTag> {
    /// Get the raw `CFDictionaryRef` for passing to Security API functions.
    pub fn as_dict_ptr(&self) -> CFDictionaryRef {
        self.as_cf_type_ref() as CFDictionaryRef
    }
}
