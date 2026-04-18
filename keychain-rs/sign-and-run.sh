#!/usr/bin/env bash
# sign-and-run.sh — build and run (with optional Developer-ID signing).
#
# The Secure Enclave key storage path requires a real Apple Developer certificate.
# See entitlements/keychain-rs.entitlements for the details.
#
# Usage:
#   ./sign-and-run.sh                      # plain run (ephemeral SE keys only)
#   ./sign-and-run.sh "Developer ID: ..."  # sign with cert then run (persistent SE keys)
set -euo pipefail

cargo build

CERT="${1:-}"

if [[ -n "$CERT" ]]; then
    echo "[sign] signing with: $CERT"
    codesign \
        --force \
        --sign "$CERT" \
        --entitlements entitlements/keychain-rs.entitlements \
        target/debug/keychain-rs
    echo "[sign] done"
else
    echo "[info] running unsigned — SE keys will be ephemeral"
fi

./target/debug/keychain-rs
