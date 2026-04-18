/// Raw FFI bindings to Apple's CoreFoundation framework.
///
/// CoreFoundation uses a reference-counting memory model with two rules:
///   - **Create Rule**: if a function name contains "Create" or "Copy", the caller
///     owns the returned reference and *must* call CFRelease when done.
///   - **Get Rule**: references returned by "Get" functions are NOT owned — do not
///     release them unless you also called CFRetain.
///
/// All CF types are opaque pointer types that share a common "isa" header, which
/// is why CFTypeRef (essentially `*const c_void`) can be used as a universal handle.
use std::os::raw::{c_char, c_long, c_void};

// ── Primitive type aliases ────────────────────────────────────────────────────

/// Universal CF reference; every CF object can be cast to this.
pub type CFTypeRef = *const c_void;

/// An index / count. Signed 64-bit on Apple Silicon (LP64).
pub type CFIndex = c_long;

/// Bit-flags parameter type (64-bit).
pub type CFOptionFlags = u64;

/// String encoding identifier.
pub type CFStringEncoding = u32;

/// UTF-8 encoding constant — *not* an extern variable, just a named integer.
pub const K_CF_STRING_ENCODING_UTF8: CFStringEncoding = 0x0800_0100;

// ── Opaque CF reference types ─────────────────────────────────────────────────
//
// In C these are pointer-to-opaque-struct (e.g. `struct __CFString *`).
// We model them as `*const c_void` so the Rust compiler treats them as raw
// addresses without needing the actual struct layout.

pub type CFStringRef = *const c_void;
pub type CFMutableStringRef = *mut c_void;
pub type CFDataRef = *const c_void;
pub type CFMutableDataRef = *mut c_void;
pub type CFDictionaryRef = *const c_void;
pub type CFMutableDictionaryRef = *mut c_void;
pub type CFArrayRef = *const c_void;
pub type CFAllocatorRef = *const c_void;
pub type CFBooleanRef = *const c_void;
pub type CFNumberRef = *const c_void;
pub type CFErrorRef = *const c_void;

// ── CFNumber type constants ───────────────────────────────────────────────────
//
// CFNumberType is a plain C enum (not an extern variable).
// We only need a handful of values.
pub const K_CF_NUMBER_SINT32_TYPE: CFIndex = 3;
pub const K_CF_NUMBER_SINT64_TYPE: CFIndex = 4;

// ── Callback structs for CFDictionary ────────────────────────────────────────
//
// CFDictionaryCreate / CFDictionaryCreateMutable require pointers to two
// callback structs. In practice we always pass the pre-defined
// `kCFTypeDictionaryKeyCallBacks` / `kCFTypeDictionaryValueCallBacks` globals,
// so we only need their *addresses*, not their field layouts.
// Declaring them as `c_void` lets us take a `*const c_void` pointer without
// needing the full struct definition.
//
// The layout (6 fields: CFIndex + 5 fn-ptrs) would work too, but using c_void
// here is simpler and equally correct for our purposes.

extern "C" {
    // kCFAllocatorDefault — the "use the default allocator" sentinel.
    // Passing NULL is equivalent on modern macOS, but using the symbol is clearer.
    pub static kCFAllocatorDefault: CFAllocatorRef;

    // Pre-defined callback structs for CF collection types.
    // We take their address when creating dictionaries/arrays.
    #[link_name = "kCFTypeDictionaryKeyCallBacks"]
    pub static K_CF_TYPE_DICT_KEY_CBS: c_void;
    #[link_name = "kCFTypeDictionaryValueCallBacks"]
    pub static K_CF_TYPE_DICT_VAL_CBS: c_void;

    // Boolean singletons (not plain `true`/`false` — they carry CF type info).
    pub static kCFBooleanTrue: CFBooleanRef;
    pub static kCFBooleanFalse: CFBooleanRef;
}

// ── CoreFoundation function bindings ─────────────────────────────────────────

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    // ── Memory management ────────────────────────────────────────────────────

    /// Decrement refcount; free the object when it reaches zero.
    /// Must be called once for every Create/Copy-rule acquisition.
    pub fn CFRelease(cf: CFTypeRef);

    /// Increment refcount and return the same pointer.
    pub fn CFRetain(cf: CFTypeRef) -> CFTypeRef;

    // ── CFString ─────────────────────────────────────────────────────────────

    /// Create a CFString from a nul-terminated C string.
    /// **Create rule** — caller must CFRelease.
    pub fn CFStringCreateWithCString(
        alloc: CFAllocatorRef,
        c_str: *const c_char,
        encoding: CFStringEncoding,
    ) -> CFStringRef;

    /// Return the length of the string in UTF-16 code units (NOT bytes).
    pub fn CFStringGetLength(the_string: CFStringRef) -> CFIndex;

    /// Maximum byte count needed to encode the string in `encoding`.
    pub fn CFStringGetMaximumSizeForEncoding(
        length: CFIndex,
        encoding: CFStringEncoding,
    ) -> CFIndex;

    /// Fast path: returns a pointer into the string's internal buffer if it
    /// happens to be stored in `encoding` already; otherwise returns NULL.
    /// The returned pointer is only valid as long as the CFString is alive
    /// and not mutated.
    pub fn CFStringGetCStringPtr(
        the_string: CFStringRef,
        encoding: CFStringEncoding,
    ) -> *const c_char;

    /// Copy the string into a caller-provided buffer.
    /// Returns `true` on success.
    pub fn CFStringGetCString(
        the_string: CFStringRef,
        buffer: *mut c_char,
        buffer_size: CFIndex,
        encoding: CFStringEncoding,
    ) -> bool;

    // ── CFData ───────────────────────────────────────────────────────────────

    /// Create a CFData by copying `length` bytes from `bytes`.
    /// **Create rule** — caller must CFRelease.
    pub fn CFDataCreate(
        alloc: CFAllocatorRef,
        bytes: *const u8,
        length: CFIndex,
    ) -> CFDataRef;

    /// Number of bytes stored in the CFData object.
    pub fn CFDataGetLength(the_data: CFDataRef) -> CFIndex;

    /// Pointer to the bytes inside the CFData. Valid only while CFData is alive.
    pub fn CFDataGetBytePtr(the_data: CFDataRef) -> *const u8;

    // ── CFDictionary ─────────────────────────────────────────────────────────

    /// Create a mutable dictionary.
    /// `capacity` is a hint (0 = no hint).
    /// **Create rule** — caller must CFRelease.
    pub fn CFDictionaryCreateMutable(
        alloc: CFAllocatorRef,
        capacity: CFIndex,
        key_callbacks: *const c_void,
        value_callbacks: *const c_void,
    ) -> CFMutableDictionaryRef;

    /// Add or replace a key/value pair.
    pub fn CFDictionarySetValue(
        the_dict: CFMutableDictionaryRef,
        key: *const c_void,
        value: *const c_void,
    );

    /// Look up a value. Returns NULL if not found.
    /// **Get rule** — do NOT CFRelease the result.
    pub fn CFDictionaryGetValue(
        the_dict: CFDictionaryRef,
        key: *const c_void,
    ) -> *const c_void;

    // ── CFNumber ─────────────────────────────────────────────────────────────

    /// Box a scalar value into a CFNumber.
    /// `the_type` is a CFNumberType constant (e.g. K_CF_NUMBER_SINT32_TYPE).
    /// **Create rule** — caller must CFRelease.
    pub fn CFNumberCreate(
        alloc: CFAllocatorRef,
        the_type: CFIndex,
        value_ptr: *const c_void,
    ) -> CFNumberRef;

    // ── CFError ──────────────────────────────────────────────────────────────

    /// Human-readable description of the error.
    /// **Copy rule** — caller must CFRelease.
    pub fn CFErrorCopyDescription(err: CFErrorRef) -> CFStringRef;
}
