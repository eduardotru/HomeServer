/// Secure Enclave key operations.
///
/// # What is the Secure Enclave?
///
/// The Secure Enclave (SE) is an isolated coprocessor built into Apple Silicon
/// (and every iPhone since 5s).  It has its own encrypted memory, boot ROM, and
/// AES engine.  Crucially, **private key material generated inside the SE never
/// leaves it** — the CPU never sees the raw key bytes.
///
/// Operations that use a SE key (sign, decrypt) are sent as requests to the SE;
/// the result is returned but the key itself stays inside the hardware boundary.
///
/// # What we build here
///
/// 1. `SeKey` — a handle to an EC P-256 private key stored in the Secure Enclave.
///    The key can be persisted (stored in the keychain under a tag) or ephemeral
///    (lives only until `SeKey` is dropped).
///
/// 2. Encryption/decryption using ECIES
///    (`kSecKeyAlgorithmECIESEncryptionCofactorVariableIVX963SHA256AESGCM`).
///    Encryption uses the public key (runs on the CPU).
///    Decryption happens **inside the SE** — we only get back the plaintext.
///
/// # Algorithm note
///
/// The Secure Enclave supports only EC keys over P-256 (secp256r1, 256 bits).
/// It does NOT support RSA.
use crate::cf::{
    cf_data_to_vec, cf_string_to_string, CfData, CfDictBuilder, CfNumber, CfString,
    OwnedCf, SecAccessControlTag, SecKeyTag,
};
use crate::error::{KeychainError, Result};
use crate::ffi::cf::{kCFAllocatorDefault, CFErrorRef, CFTypeRef};
use crate::ffi::security::{
    kSecAttrApplicationTag, kSecAttrIsPermanent, kSecAttrKeySizeInBits, kSecAttrKeyType,
    kSecAttrKeyTypeECSECPrimeRandom, kSecAttrLabel, kSecAttrTokenID,
    kSecAttrTokenIDSecureEnclave, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
    kSecAttrAccessControl, kSecClass, kSecClassKey, kSecMatchLimit, kSecMatchLimitOne,
    kSecPrivateKeyAttrs, kSecReturnRef, SecAccessControlCreateWithFlags,
    SecItemCopyMatching, SecItemDelete,
    SecKeyCreateDecryptedData, SecKeyCreateEncryptedData, SecKeyCreateRandomKey,
    SecKeyCopyPublicKey, SecKeyIsAlgorithmSupported,
    kSecKeyAlgorithmECIESEncryptionCofactorVariableIVX963SHA256AESGCM,
    SEC_ACCESS_PRIVATE_KEY_USAGE, ERR_SEC_SUCCESS,
};
use std::os::raw::c_void;
use std::ptr;

// ── SeKey ─────────────────────────────────────────────────────────────────────

/// A handle to an EC P-256 private key in the Secure Enclave.
///
/// `Drop` calls `CFRelease` on the internal `SecKeyRef`.  If the key was
/// created with `persistent: true` it remains in the keychain even after
/// the handle is dropped.
pub struct SeKey {
    /// The SecKeyRef for the private key.  The raw bits are inside the SE.
    private_key: OwnedCf<SecKeyTag>,
    /// The corresponding public key (lives on the CPU, not in the SE).
    public_key: OwnedCf<SecKeyTag>,
}

impl SeKey {
    // ── Construction ─────────────────────────────────────────────────────────

    /// Generate a new EC P-256 key pair inside the Secure Enclave.
    ///
    /// # Parameters
    ///
    /// - `label`: human-readable label (visible in Keychain Access.app).
    /// - `tag`: application-defined identifier used to look the key up later
    ///   (stored as arbitrary bytes in `kSecAttrApplicationTag`).
    /// - `persistent`: if `true`, the private key is stored in the system
    ///   keychain and survives across app launches.  If `false` the key is
    ///   ephemeral and exists only for the lifetime of `SeKey`.
    ///
    /// # Why `kSecAccessControlPrivateKeyUsage`?
    ///
    /// Without this flag the SE would still generate the key, but operations
    /// would not be properly gated by the SE's access control mechanism.
    /// With it, the SE enforces that only authorised callers can use the key.
    pub fn generate(label: &str, tag: &[u8], persistent: bool) -> Result<Self> {
        // 1. Build the access-control object.
        //    Protection: "when unlocked, on this device only" (no iCloud sync).
        //    Flag: SEC_ACCESS_PRIVATE_KEY_USAGE gates key-use operations through the SE.
        let mut cf_error: CFErrorRef = ptr::null();
        let acl = unsafe {
            SecAccessControlCreateWithFlags(
                kCFAllocatorDefault,
                kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                SEC_ACCESS_PRIVATE_KEY_USAGE,
                &mut cf_error,
            )
        };
        if acl.is_null() {
            return Err(extract_cf_error(cf_error));
        }
        let acl: OwnedCf<SecAccessControlTag> = unsafe { OwnedCf::from_raw(acl as CFTypeRef) };

        // 2. Build the private-key attribute sub-dictionary.
        //
        //    This sub-dictionary is passed under `kSecPrivateKeyAttrs` and
        //    applies only to the *private* half of the key pair.
        let cf_label = CfString::new(label);
        let cf_tag = CfData::from_bytes(tag);

        let mut priv_attrs = CfDictBuilder::new();
        unsafe {
            priv_attrs
                // Store in keychain?
                .set_bool(kSecAttrIsPermanent, persistent)
                // Human-readable label.
                .set_str_key_str_val(kSecAttrLabel, cf_label.as_ptr())
                // Machine-readable tag (for lookup).
                .set_data(kSecAttrApplicationTag, cf_tag.as_ptr())
                // Attach the access-control object.
                .set_raw(
                    kSecAttrAccessControl as *const c_void,
                    acl.as_ptr() as *const c_void,
                );
        }
        let priv_attrs_dict = priv_attrs.build();

        // 3. Build the top-level key generation parameters.
        let size_256 = CfNumber::from_i32(256);

        let mut params = CfDictBuilder::new();
        unsafe {
            params
                // Key algorithm: EC over P-256.
                .set_raw(
                    kSecAttrKeyType as *const c_void,
                    kSecAttrKeyTypeECSECPrimeRandom as *const c_void,
                )
                // Key size: 256 bits (the only valid size for SE).
                .set_number(kSecAttrKeySizeInBits, size_256.as_ptr())
                // Token ID: bind this key to the Secure Enclave hardware.
                .set_raw(
                    kSecAttrTokenID as *const c_void,
                    kSecAttrTokenIDSecureEnclave as *const c_void,
                )
                // Private key attributes (includes access control).
                .set_dict(kSecPrivateKeyAttrs, priv_attrs_dict.as_ptr() as _);
        }
        let params_dict = params.build();

        // 4. Generate the key pair.  The SE generates the private key and
        //    returns a reference to it (the bits stay in the SE).
        let mut gen_error: CFErrorRef = ptr::null();
        let private_key = unsafe {
            SecKeyCreateRandomKey(params_dict.as_ptr() as _, &mut gen_error)
        };
        if private_key.is_null() {
            return Err(extract_cf_error(gen_error));
        }
        let private_key: OwnedCf<SecKeyTag> = unsafe { OwnedCf::from_raw(private_key as CFTypeRef) };

        // 5. Derive the public key (lives on the CPU as a plain curve point).
        let public_key = unsafe { SecKeyCopyPublicKey(private_key.as_ptr() as _) };
        if public_key.is_null() {
            return Err(KeychainError::CfError(
                "SecKeyCopyPublicKey returned NULL".into(),
            ));
        }
        let public_key: OwnedCf<SecKeyTag> = unsafe { OwnedCf::from_raw(public_key as CFTypeRef) };

        Ok(SeKey {
            private_key,
            public_key,
        })
    }

    /// Load a previously persisted Secure Enclave key by its application tag.
    ///
    /// The tag must match the one used in `generate()`.
    pub fn load(tag: &[u8]) -> Result<Self> {
        let cf_tag = CfData::from_bytes(tag);

        let mut query = CfDictBuilder::new();
        unsafe {
            query
                .set_raw(kSecClass as *const c_void, kSecClassKey as *const c_void)
                // Filter to keys stored in the Secure Enclave.
                .set_raw(
                    kSecAttrTokenID as *const c_void,
                    kSecAttrTokenIDSecureEnclave as *const c_void,
                )
                // Match by application tag.
                .set_data(kSecAttrApplicationTag, cf_tag.as_ptr())
                // Return the SecKeyRef.
                .set_bool(kSecReturnRef, true)
                .set_raw(kSecMatchLimit as *const c_void, kSecMatchLimitOne as *const c_void);
        }

        let q = query.build();
        let mut result: CFTypeRef = ptr::null();
        let status = unsafe { SecItemCopyMatching(q.as_ptr() as _, &mut result) };
        if status != ERR_SEC_SUCCESS {
            return Err(KeychainError::from_os_status(status));
        }

        // result is a retained SecKeyRef (private key).
        let private_key: OwnedCf<SecKeyTag> = unsafe { OwnedCf::from_raw(result) };
        let public_key = unsafe { SecKeyCopyPublicKey(private_key.as_ptr() as _) };
        if public_key.is_null() {
            return Err(KeychainError::CfError("SecKeyCopyPublicKey returned NULL".into()));
        }
        let public_key: OwnedCf<SecKeyTag> = unsafe { OwnedCf::from_raw(public_key as CFTypeRef) };

        Ok(SeKey { private_key, public_key })
    }

    /// Delete a persisted key from the keychain by its application tag.
    pub fn delete(tag: &[u8]) -> Result<()> {
        let cf_tag = CfData::from_bytes(tag);

        let mut query = CfDictBuilder::new();
        unsafe {
            query
                .set_raw(kSecClass as *const c_void, kSecClassKey as *const c_void)
                .set_raw(
                    kSecAttrTokenID as *const c_void,
                    kSecAttrTokenIDSecureEnclave as *const c_void,
                )
                .set_data(kSecAttrApplicationTag, cf_tag.as_ptr());
        }

        let q = query.build();
        let status = unsafe { SecItemDelete(q.as_ptr() as _) };
        if status == ERR_SEC_SUCCESS {
            Ok(())
        } else {
            Err(KeychainError::from_os_status(status))
        }
    }

    // ── Crypto operations ─────────────────────────────────────────────────────

    /// Encrypt `plaintext` using the public key (ECIES / AES-GCM).
    ///
    /// This runs entirely on the CPU — no SE involvement needed for encryption.
    /// Anyone with the public key can encrypt; only the SE key can decrypt.
    pub fn encrypt(&self, plaintext: &[u8]) -> Result<Vec<u8>> {
        let cf_plain = CfData::from_bytes(plaintext);

        let mut cf_error: CFErrorRef = ptr::null();
        let ciphertext = unsafe {
            SecKeyCreateEncryptedData(
                self.public_key.as_ptr(),
                // SAFETY: the algorithm constant is a valid CFStringRef.
                kSecKeyAlgorithmECIESEncryptionCofactorVariableIVX963SHA256AESGCM,
                cf_plain.as_ptr(),
                &mut cf_error,
            )
        };
        if ciphertext.is_null() {
            return Err(extract_cf_error(cf_error));
        }
        let owned: OwnedCf<crate::cf::CfDataTag> = unsafe { OwnedCf::from_raw(ciphertext as CFTypeRef) };
        Ok(cf_data_to_vec(owned.as_ptr() as _))
    }

    /// Decrypt `ciphertext` using the private key stored in the Secure Enclave.
    ///
    /// The actual decryption happens **inside the SE coprocessor**.  If the key
    /// has an access control policy requiring biometry, the OS will display the
    /// authentication prompt automatically (you must be in a UI context).
    pub fn decrypt(&self, ciphertext: &[u8]) -> Result<Vec<u8>> {
        let cf_cipher = CfData::from_bytes(ciphertext);

        let mut cf_error: CFErrorRef = ptr::null();
        let plaintext = unsafe {
            SecKeyCreateDecryptedData(
                self.private_key.as_ptr(),
                kSecKeyAlgorithmECIESEncryptionCofactorVariableIVX963SHA256AESGCM,
                cf_cipher.as_ptr(),
                &mut cf_error,
            )
        };
        if plaintext.is_null() {
            return Err(extract_cf_error(cf_error));
        }
        let owned: OwnedCf<crate::cf::CfDataTag> = unsafe { OwnedCf::from_raw(plaintext as CFTypeRef) };
        Ok(cf_data_to_vec(owned.as_ptr() as _))
    }

    /// Check whether the chosen ECIES algorithm is supported by our public key.
    ///
    /// On genuine Apple Silicon with a Secure Enclave key this should always
    /// return `true`.  Useful for diagnostics / porting to simulator builds.
    pub fn supports_ecies(&self) -> bool {
        // operation 2 = encrypt, checked against the public key.
        unsafe {
            SecKeyIsAlgorithmSupported(
                self.public_key.as_ptr(),
                2, // kSecKeyOperationTypeEncrypt
                kSecKeyAlgorithmECIESEncryptionCofactorVariableIVX963SHA256AESGCM,
            )
        }
    }

    // ── Accessors ─────────────────────────────────────────────────────────────

    /// The raw `SecKeyRef` for the public key.
    /// Useful if you need to export the public key to a remote party.
    pub fn public_key_ref(&self) -> crate::ffi::security::SecKeyRef {
        self.public_key.as_ptr() as crate::ffi::security::SecKeyRef
    }
}

// ── Error extraction helper ───────────────────────────────────────────────────

/// Pull a description string out of a CFErrorRef (which may be null).
fn extract_cf_error(err: CFErrorRef) -> KeychainError {
    if err.is_null() {
        return KeychainError::CfError("unknown CF error (null CFErrorRef)".into());
    }
    // SAFETY: err is a non-null CFErrorRef obtained from a Security function.
    let msg = unsafe {
        let desc = crate::ffi::cf::CFErrorCopyDescription(err);
        let s = if desc.is_null() {
            "unknown CF error".into()
        } else {
            let s = cf_string_to_string(desc).unwrap_or_else(|| "unknown CF error".into());
            crate::ffi::cf::CFRelease(desc as CFTypeRef);
            s
        };
        crate::ffi::cf::CFRelease(err as CFTypeRef);
        s
    };
    KeychainError::CfError(msg)
}
