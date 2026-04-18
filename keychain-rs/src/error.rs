/// Error types for the keychain library.
use crate::ffi::security::{
    ERR_SEC_AUTH_FAILED, ERR_SEC_DUPLICATE_ITEM, ERR_SEC_INTERACTION_NOT_ALLOWED,
    ERR_SEC_ITEM_NOT_FOUND, ERR_SEC_NOT_AVAILABLE, OSStatus,
};
use std::fmt;

#[derive(Debug)]
pub enum KeychainError {
    /// The item was not found in the keychain.
    NotFound,
    /// An item with the same primary key already exists.
    DuplicateItem,
    /// The user or the system refused the authentication challenge.
    AuthFailed,
    /// Biometrics / UI interaction is not currently available (e.g. background process).
    InteractionNotAllowed,
    /// The keychain service itself is not available.
    ServiceNotAvailable,
    /// An unexpected OSStatus code was returned.
    Os(OSStatus),
    /// A CoreFoundation error was returned (e.g. from SecKeyCreate*).
    CfError(String),
    /// Invalid input was supplied by the caller.
    InvalidInput(String),
}

impl KeychainError {
    /// Convert an `OSStatus` into the most specific variant.
    pub fn from_os_status(status: OSStatus) -> Self {
        match status {
            ERR_SEC_ITEM_NOT_FOUND => KeychainError::NotFound,
            ERR_SEC_DUPLICATE_ITEM => KeychainError::DuplicateItem,
            ERR_SEC_AUTH_FAILED => KeychainError::AuthFailed,
            ERR_SEC_INTERACTION_NOT_ALLOWED => KeychainError::InteractionNotAllowed,
            ERR_SEC_NOT_AVAILABLE => KeychainError::ServiceNotAvailable,
            other => KeychainError::Os(other),
        }
    }
}

impl fmt::Display for KeychainError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            KeychainError::NotFound => write!(f, "keychain item not found"),
            KeychainError::DuplicateItem => write!(f, "keychain item already exists"),
            KeychainError::AuthFailed => write!(f, "authentication failed"),
            KeychainError::InteractionNotAllowed => {
                write!(f, "UI interaction not allowed in this context")
            }
            KeychainError::ServiceNotAvailable => write!(f, "keychain service not available"),
            KeychainError::Os(code) => write!(f, "OSStatus error {code}"),
            KeychainError::CfError(msg) => write!(f, "CoreFoundation error: {msg}"),
            KeychainError::InvalidInput(msg) => write!(f, "invalid input: {msg}"),
        }
    }
}

impl std::error::Error for KeychainError {}

pub type Result<T> = std::result::Result<T, KeychainError>;
