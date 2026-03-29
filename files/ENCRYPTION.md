# Files Service — Encryption

## Current approach: Fernet + HMAC filenames + encrypted index

All encryption is handled in `files.py` using Python's `cryptography` library.

- **File contents**: encrypted with `Fernet` (AES-128-CBC + HMAC-SHA256)
- **Filenames on disk**: `HMAC-SHA256(key, logical_path)` → 64-char hex — unrecognizable even on a live system
- **Directory structure**: no real directories on disk; a single encrypted `.index` file maps logical paths to disk names + metadata
- **Key**: `FILES_ENCRYPTION_KEY` in `.env` — a 32-byte URL-safe base64 string
- **Multiple deployments**: each gets its own key + `FILES_ROOT`; same logical path maps to different on-disk names across deployments

Generate a key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Pros
- Filenames hidden even while the service is running (HMAC names reveal nothing)
- Portable — no macOS dependency
- No OS-level setup required

### Cons
- ~80 lines of crypto/index management code in `files.py`
- In-memory decryption for every read (acceptable for local homeserver scale)
- No concurrent write safety (single-user assumption)

---

## Alternative: macOS encrypted sparse disk image (`hdiutil`)

macOS can create a `.sparseimage` file that acts as an AES-256 encrypted block device. Mount it with a passphrase and the OS handles all crypto transparently. The files service reverts to simple filesystem code with zero crypto dependencies.

```bash
# Create once:
hdiutil create -size 5g -encryption AES-256 -fs APFS \
  -volname HomeServerFiles -type SPARSE data/files.sparseimage

# Mount before starting the container (Makefile):
hdiutil attach data/files.sparseimage -mountpoint data/files -stdinpass <<< "$FILES_PASSPHRASE"

# Detach after stopping the container (Makefile):
hdiutil detach data/files
```

The container mounts `data/files` as before — encryption is invisible to the service.

### Pros
- Zero crypto code in `files.py` — revert to the original simple filesystem implementation
- AES-256 (vs AES-128 in Fernet)
- Sparse image grows automatically up to the set limit
- OS-level crypto, battle-tested

### Cons
- Filenames are **visible** while the image is mounted (anyone with shell access can `ls data/files/`)
- macOS only — not portable
- Requires Makefile changes for mount/detach lifecycle
- One `.sparseimage` file per deployment to manage

---

## Comparison

| | Current (Fernet) | macOS disk image |
|---|---|---|
| Filename visibility (mounted) | Hidden | Visible |
| Filename visibility (unmounted) | Hidden | Hidden |
| Crypto code in service | ~80 lines | None |
| Portability | Cross-platform | macOS only |
| Key storage | `FILES_ENCRYPTION_KEY` in `.env` | `FILES_PASSPHRASE` in `.env` |
| Encryption strength | AES-128 | AES-256 |
| macOS setup required | No | Yes (one-time `hdiutil create`) |
