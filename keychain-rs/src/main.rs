/// Demo: exercise every layer of the keychain-rs library.
///
/// What this covers:
///   1. Basic password CRUD via Keychain Services
///   2. Upsert (add-or-update) semantics
///   3. Secure Enclave key generation, ECIES encryption, and SE decryption
///
/// Run with:
///   cargo run
///
/// NOTE: Secure Enclave operations only work on real Apple Silicon hardware.
/// In the Xcode Simulator or on Intel Macs the SE is emulated / unavailable
/// and SecKeyCreateRandomKey will return an error when the SE token ID is set.
use keychain_rs::keychain;
use keychain_rs::secure_enclave::SeKey;

fn main() {
    println!("=== keychain-rs demo ===\n");

    // ── 1. Basic password CRUD ────────────────────────────────────────────────
    demo_password_crud();

    println!();

    // ── 2. Secure Enclave ─────────────────────────────────────────────────────
    demo_secure_enclave();
}

fn demo_password_crud() {
    println!("--- Password CRUD ---");

    let service = "com.example.keychain-rs-demo";
    let account = "alice@example.com";
    let secret = b"hunter2";
    let new_secret = b"correct-horse-battery-staple";

    // Clean up any leftover item from a previous run.
    let _ = keychain::delete_password(service, account);

    // Add
    match keychain::add_password(service, account, secret) {
        Ok(()) => println!("[+] add_password: OK"),
        Err(e) => eprintln!("[-] add_password failed: {e}"),
    }

    // Get
    match keychain::get_password(service, account) {
        Ok(bytes) => {
            let s = String::from_utf8_lossy(&bytes);
            println!("[+] get_password: '{s}'");
            assert_eq!(bytes, secret, "retrieved secret mismatch");
        }
        Err(e) => eprintln!("[-] get_password failed: {e}"),
    }

    // Duplicate protection
    match keychain::add_password(service, account, secret) {
        Err(keychain_rs::error::KeychainError::DuplicateItem) => {
            println!("[+] duplicate item correctly detected");
        }
        other => eprintln!("[-] expected DuplicateItem, got: {other:?}"),
    }

    // Update
    match keychain::update_password(service, account, new_secret) {
        Ok(()) => println!("[+] update_password: OK"),
        Err(e) => eprintln!("[-] update_password failed: {e}"),
    }

    // Verify update
    match keychain::get_password(service, account) {
        Ok(bytes) => {
            let s = String::from_utf8_lossy(&bytes);
            println!("[+] get_password after update: '{s}'");
            assert_eq!(bytes, new_secret, "updated secret mismatch");
        }
        Err(e) => eprintln!("[-] get_password after update failed: {e}"),
    }

    // Upsert (should just update since item exists)
    match keychain::upsert_password(service, account, b"upserted!") {
        Ok(()) => println!("[+] upsert_password: OK"),
        Err(e) => eprintln!("[-] upsert_password failed: {e}"),
    }

    // Delete
    match keychain::delete_password(service, account) {
        Ok(()) => println!("[+] delete_password: OK"),
        Err(e) => eprintln!("[-] delete_password failed: {e}"),
    }

    // Confirm deletion
    match keychain::get_password(service, account) {
        Err(keychain_rs::error::KeychainError::NotFound) => {
            println!("[+] item correctly absent after deletion");
        }
        other => eprintln!("[-] expected NotFound after deletion, got: {other:?}"),
    }
}

fn demo_secure_enclave() {
    println!("--- Secure Enclave ---");
    println!("[*] Note: persistent SE keys require signing with the");
    println!("    keychain-access-groups entitlement.  Trying ephemeral key first.");
    println!("    (See entitlements/keychain-rs.entitlements for the persistent path.)");
    println!();

    let tag = b"com.example.keychain-rs.se-demo-key";
    let label = "keychain-rs demo key";

    // Clean up any leftover key from a previous run.
    let _ = SeKey::delete(tag);

    // Try persistent first, fall back to ephemeral if entitlements are missing.
    println!("[*] Generating EC P-256 key in Secure Enclave (persistent)...");
    let key = match SeKey::generate(label, tag, true /* persistent */) {
        Ok(k) => {
            println!("[+] Persistent key generated successfully");
            k
        }
        Err(persistent_err) => {
            eprintln!("[-] Persistent SE key failed: {persistent_err}");
            eprintln!("    Trying ephemeral (non-persistent) key...");
            match SeKey::generate(label, tag, false /* ephemeral */) {
                Ok(k) => {
                    println!("[+] Ephemeral SE key generated successfully");
                    k
                }
                Err(e) => {
                    // SE not available at all (Intel Mac, Simulator, VM).
                    eprintln!("[-] Ephemeral SE key also failed: {e}");
                    eprintln!("    SE is unavailable in this environment.");
                    eprintln!("    To enable persistent keys, sign the binary:");
                    eprintln!("      codesign --entitlements entitlements/keychain-rs.entitlements \\");
                    eprintln!("               --sign - target/debug/keychain-rs");
                    return;
                }
            }
        }
    };

    // Check algorithm support
    let supported = key.supports_ecies();
    println!("[+] ECIES algorithm supported: {supported}");

    // Encrypt some plaintext using the public key.
    let plaintext = b"hello from the Secure Enclave world";
    println!("[*] Encrypting: '{}'", String::from_utf8_lossy(plaintext));

    let ciphertext = match key.encrypt(plaintext) {
        Ok(c) => {
            println!("[+] Encrypted {} -> {} bytes", plaintext.len(), c.len());
            c
        }
        Err(e) => {
            eprintln!("[-] Encryption failed: {e}");
            return;
        }
    };

    // Decrypt inside the Secure Enclave.
    println!("[*] Decrypting (this runs inside the SE)...");
    let decrypted = match key.decrypt(&ciphertext) {
        Ok(d) => {
            println!("[+] Decrypted: '{}'", String::from_utf8_lossy(&d));
            d
        }
        Err(e) => {
            eprintln!("[-] Decryption failed: {e}");
            return;
        }
    };

    assert_eq!(decrypted, plaintext, "round-trip mismatch!");
    println!("[+] Round-trip verified: plaintext == decrypt(encrypt(plaintext))");

    // Load the persisted key from the keychain (simulate app restart).
    println!("[*] Loading persisted key by tag...");
    match SeKey::load(tag) {
        Ok(loaded) => {
            println!("[+] Key loaded from keychain");
            // Verify we can still decrypt with the reloaded handle.
            match loaded.decrypt(&ciphertext) {
                Ok(d) => {
                    assert_eq!(d, plaintext);
                    println!("[+] Decryption with reloaded key: OK");
                }
                Err(e) => eprintln!("[-] Decryption with reloaded key failed: {e}"),
            }
        }
        Err(e) => eprintln!("[-] SeKey::load failed: {e}"),
    }

    // Clean up the persisted key.
    match SeKey::delete(tag) {
        Ok(()) => println!("[+] Key deleted from keychain"),
        Err(e) => eprintln!("[-] Key deletion failed: {e}"),
    }
}
