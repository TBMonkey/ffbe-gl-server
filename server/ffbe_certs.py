#!/usr/bin/env python3
"""ffbe_certs.py — generate the lab root CA and the server leaf, in-process (no `openssl` binary).

**Why this exists.** `harness/gen_ca.sh` has been the cert producer since the TLS work, and it is the right
tool for a checkout. It is the wrong tool for the product: a release bundle has no `openssl` binary, and a
Windows user has none either, so cert generation cannot be a shell script on the user path. The SHAPE is the
script's, kept deliberately because it is recovered, not chosen:

  * the client verifies against the **Android system trust store**, so the ROOT must be installable there as
    a CA (basicConstraints CA:TRUE, keyCertSign) -- `docs/notes/tls_pinning_findings.md` §5;
  * the LEAF must carry `v900-lapis.gumi.sg` in its SAN or the default HostnameVerifier rejects it;
  * the leaf must stay under Chromium's ~398-day limit or the in-game WebView fails its handshake
    (net error -213), which is how the blank ToS screen was found. `gen_ca.sh` uses 397 days for both the
    root and the leaf; that is kept.

`harness/cert_check.py` is the verification: it generates a pair here, verifies the chain with `openssl`
when that binary exists, and compares `subject_hash_old` -- the Android cacerts filename -- against
`openssl x509 -subject_hash_old`. That comparison is the point: a wrong hash installs a CA the guest never
looks at, and nothing else would say so.

CLI (also what the engine's future `gen-certs` wraps):
    python3 server/ffbe_certs.py --out DIR [--force] [--host HOST]
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CANON_HOST = "v900-lapis.gumi.sg"
EXTRA_SANS = ("localhost", "*.gumi.sg")  # lab convenience; harmless on a private root
VALID_DAYS = 397  # Chromium's leaf limit (net error -213 above ~398); kept for the root too, as gen_ca.sh does
ORG = "ffbe-lab"


def subject_hash_old(cert: x509.Certificate) -> str:
    """OpenSSL's legacy `subject_hash_old`: MD5 of the subject name's DER, first 4 bytes little-endian.

    Android names a system anchor `<hash>.0` in /system/etc/security/cacerts/. The name's DER from
    `cryptography` is the UTF8String encoding, which is what OpenSSL's canonical form produces for these
    attributes -- `harness/cert_check.py` proves that against `openssl x509 -subject_hash_old`.
    """
    md = hashlib.md5(cert.subject.public_bytes()).digest()
    return "%08x" % (md[0] | md[1] << 8 | md[2] << 16 | md[3] << 24)


def _write(path: str, data: bytes, mode: int) -> None:
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, mode)


def _ca_key_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=False, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
        encipher_only=False, decipher_only=False,
    )


def _leaf_key_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True, content_commitment=False, key_encipherment=True,
        data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
        encipher_only=False, decipher_only=False,
    )


def generate_certs(out_dir: str, *, force: bool = False, host: str = CANON_HOST,
                   extra_sans: tuple[str, ...] = EXTRA_SANS, days: int = VALID_DAYS) -> dict:
    """Write `ca.key/ca.pem/leaf.key/leaf.pem` into `out_dir`; return the paths, host and Android name."""
    ca_pem = os.path.join(out_dir, "ca.pem")
    if os.path.exists(ca_pem) and not force:
        raise FileExistsError(f"{ca_pem} exists (pass force=True / --force to regenerate)")
    os.makedirs(out_dir, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(minutes=5)
    not_after = now + datetime.timedelta(days=days)

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    ca_name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
        x509.NameAttribute(NameOID.COMMON_NAME, "FFBE Lab Root CA"),
    ])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_ca_key_usage(), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
        x509.NameAttribute(NameOID.COMMON_NAME, host),
    ])
    sans = [x509.DNSName(host)] + [x509.DNSName(s) for s in extra_sans]
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_leaf_key_usage(), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    no_enc = serialization.NoEncryption()
    _write(os.path.join(out_dir, "ca.key"), ca_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, no_enc), 0o600)
    _write(ca_pem, ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)
    _write(os.path.join(out_dir, "leaf.key"), leaf_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, no_enc), 0o600)
    _write(os.path.join(out_dir, "leaf.pem"), leaf_cert.public_bytes(serialization.Encoding.PEM), 0o644)

    return {
        "ca_pem": ca_pem,
        "ca_key": os.path.join(out_dir, "ca.key"),
        "leaf_pem": os.path.join(out_dir, "leaf.pem"),
        "leaf_key": os.path.join(out_dir, "leaf.key"),
        "host": host,
        "days": days,
        "android_name": subject_hash_old(ca_cert) + ".0",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="generate the lab root CA and server leaf (no openssl needed)")
    ap.add_argument("--out", required=True, help="output directory (e.g. <data-root>/certs)")
    ap.add_argument("--force", action="store_true", help="overwrite an existing ca.pem")
    ap.add_argument("--host", default=CANON_HOST, help=f"leaf CN + first SAN (default {CANON_HOST})")
    args = ap.parse_args(argv)
    try:
        info = generate_certs(args.out, force=args.force, host=args.host)
    except FileExistsError as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 1
    print(f"wrote {info['leaf_pem']} and {info['ca_pem']} ({info['days']} days, host {info['host']})")
    print(f"Android cacerts filename: {info['android_name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
