#!/usr/bin/env python3
"""ffbe_crypto.py — the wire cipher: AES-128 on one of two backends, same bytes either way.

Extracted 2026-09-26 from `capture_server.py` as the bottom leaf of the module split
(docs/SERVER_REFACTOR_PLAN.md, Phase 3). It is the right shape to split first because it is the only
block with **no** dependency on the rest of the server: it needs `subprocess` and `base64` and nothing
else, and nothing outside it needs anything but its four public names. Every other seam had to be
negotiated; this one did not.

The scheme is NOT a choice -- it is recovered, and the recovery is recorded at
`docs/analysis/FFBE_SERVER_CONTRACT.md` §1e.

**The backend IS a choice, and the reason arrived 2026-10-16.** The original shape was deliberately
dependency-free (TLS via `ssl`, AES via the `openssl` CLI) so a checkout starts with one command and no
install step. A release bundle must serve on a machine with no `openssl` binary -- Windows has none -- and
a process spawn per request is the wrong shape for a product. So the cipher now prefers the `cryptography`
library when it is importable (the bundle ships it) and falls back to the CLI otherwise, keeping a bare
checkout byte-identical to what it did before. `FFBE_CRYPTO=openssl` or `=cryptography` forces one backend;
`harness/crypto_backend_check.py` uses that to prove the two agree on the same vectors, which is the only
reason this is safe to switch.

Note the double-pad on the way OUT, which is not a bug and not sloppiness -- see `cbc_encrypt`.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys

IV = b"dZMjkk8gFDzKHlsx"  # contract §1e; same literal as scripts/mst_decode.py:80
CIPHER = "aes-128-cbc"

_OVERRIDE = os.environ.get("FFBE_CRYPTO", "auto").strip().lower()
if _OVERRIDE not in ("auto", "openssl", "cryptography"):
    # A typo must not silently look like a deliberate choice (audit F4, 2026-10-16).
    sys.stderr.write(f"ffbe_crypto: unknown FFBE_CRYPTO={_OVERRIDE!r}; using auto\n")
    _OVERRIDE = "auto"
try:
    if _OVERRIDE == "openssl":
        raise ImportError("FFBE_CRYPTO=openssl")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _HAVE_LIB = True
except ImportError:
    if _OVERRIDE == "cryptography":
        raise
    _HAVE_LIB = False


def backend() -> str:
    """Which AES implementation is live: `cryptography` (bundled) or the `openssl` CLI (checkout)."""
    return "cryptography" if _HAVE_LIB else "openssl"


def key16(token: str) -> bytes:
    """8-char EncodeKey, UTF-8, NUL-padded to 16 (contract §1e)."""
    raw = token.encode("utf-8")[:16]
    return raw + b"\x00" * (16 - len(raw))


def _raw_lib(key: bytes, data: bytes, iv: bytes, encrypt: bool) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    op = cipher.encryptor() if encrypt else cipher.decryptor()
    return op.update(data) + op.finalize()


def _raw_openssl(decrypt: bool, key: bytes, data: bytes, iv: bytes = IV) -> bytes:
    cmd = ["openssl", "enc", "-" + CIPHER, "-nopad", "-K", key.hex(), "-iv", iv.hex()]
    if decrypt:
        cmd.insert(2, "-d")
    proc = subprocess.run(cmd, input=data, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("openssl failed: " + proc.stderr.decode(errors="replace").strip())
    return proc.stdout


def _raw_cbc(decrypt: bool, key: bytes, data: bytes, iv: bytes = IV) -> bytes:
    if _HAVE_LIB:
        return _raw_lib(key, data, iv, encrypt=not decrypt)
    return _raw_openssl(decrypt, key, data, iv)


def cbc_decrypt(key: bytes, b64: str, iv: bytes = IV) -> bytes:
    data = base64.b64decode(b64)
    # Length validation belongs to the WIRE, not the backend: without this the library raises ValueError
    # and the CLI raises RuntimeError for the same malformed input (audit F2, 2026-10-16).
    if len(data) % 16:
        raise ValueError(f"ciphertext not 16-byte aligned (len={len(data)})")
    out = _raw_cbc(True, key, data, iv)
    if not out or not 1 <= out[-1] <= 16 or out[-out[-1]:] != bytes([out[-1]]) * out[-1]:
        raise ValueError(f"bad PKCS5 padding (len={len(out)})")
    return out[: -out[-1]]


def cbc_encrypt(key: bytes, plain: bytes, iv: bytes = IV, double_pad: bool = False) -> str:
    """Encrypt the reply body.

    `double_pad` reproduces what the CLIENT does on its own requests, which was verified live 2026-09-17:
    `LapisJNI` applies a MANUAL PKCS7 pad first (the request's decrypted plaintext ends with 13 x 0x0D), and
    `Cipher.getInstance("AES/CBC/PKCS5Padding")` then adds a further full block (`doFinal in=528 out=544`).
    Our own single-padded replies therefore do not match what the client's decode expects.
    """
    if double_pad:
        pad = 16 - (len(plain) % 16)
        data = plain + bytes([pad]) * pad + bytes([0x10]) * 16
    else:
        pad = 16 - (len(plain) % 16)
        data = plain + bytes([pad]) * pad
    return base64.b64encode(_raw_cbc(False, key, data, iv)).decode()


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """Raw AES-128-ECB decrypt, no padding -- the MST/text decoders' mode (they unpad or parse themselves).

    It lives here because it is the same fact as the wire cipher (which AES, which key shape), so the
    decoders get one backend and the bundle needs neither the `openssl` binary nor a second dependency.
    """
    if len(key) != 16:
        raise ValueError("key must be 16 bytes")
    if len(data) % 16:
        raise ValueError("ciphertext not 16-byte aligned")
    if not data:
        return b""
    if _HAVE_LIB:
        op = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return op.update(data) + op.finalize()
    proc = subprocess.run(
        ["openssl", "enc", "-d", "-aes-128-ecb", "-nopad", "-K", key.hex()],
        input=data,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("openssl failed: " + proc.stderr.decode(errors="replace").strip())
    return proc.stdout
