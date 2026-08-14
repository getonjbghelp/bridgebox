from __future__ import annotations

import ipaddress
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)

# certutil and icacls each flash a console window without this. BridgeBox
# is meant to be one window - same reasoning as autostart.py and
# zapret/process.py, which set it for the same reason.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Bound on icacls/certutil. Both run synchronously on the caller's thread
# during bridge start, so a hung one would freeze the whole start button -
# the same failure ZapretProcess.stop() already had with taskkill.
ICACLS_TIMEOUT_S = 10
CERTUTIL_TIMEOUT_S = 30

# Well-known SIDs, not the English group names. icacls resolves account names
# in the SYSTEM LOCALE, so "Administrators:F" fails with 1332
# (ERROR_NONE_MAPPED) on a Russian install where the group is
# "Администраторы" - which is exactly what this machine did, silently, until
# the exit code started being checked. A SID is the same on every locale.
SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"

# The store is keyed on this when an old generation has to be removed, so it
# must stay stable across releases.
CA_COMMON_NAME = "BridgeBox Local CA"

CA_CERT_FILENAME = "bridgebox-ca.pem"
CA_KEY_FILENAME = "bridgebox-ca-key.pem"
LEAF_CERT_FILENAME = "localhost.pem"
LEAF_KEY_FILENAME = "localhost-key.pem"

# Written by whoever installs the CA into the Windows trust store, and cleared
# by generate_ca when it reissues one. Lives here rather than in runtime_core
# because generate_ca is what invalidates it.
CA_INSTALLED_MARKER = ".ca-installed"

# Long-lived on purpose: this CA is only ever trusted by the local machine's
# own store and only ever presented to game clients, not public browsers, so
# the usual short-lived public-CA constraints don't apply.
CA_VALIDITY = timedelta(days=3650)
LEAF_VALIDITY = timedelta(days=3650)
NOT_BEFORE_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class CertPaths:
    cert: Path
    key: Path


def _write_private_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    # Removed first: an existing key may carry a restrictive ACL from an older
    # BridgeBox that granted the current user read-only, and overwriting that
    # in place raises PermissionError. Deleting is governed by the directory's
    # ACL, which we have not touched.
    path.unlink(missing_ok=True)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _decode_console(raw: bytes) -> str:
    """Decode a Windows console tool's output.

    icacls and certutil write in the OEM codepage (cp866 on a Russian
    install), not UTF-8 - decoding as UTF-8 turned a perfectly clear error
    message into a line of replacement characters, which is worse than no
    message because it looks like corruption rather than a localised string."""
    for encoding in ("oem", "utf-8"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def _harden_key_permissions(path: Path, *, runner=subprocess.run) -> None:
    """Restrict a private key file to the current user, SYSTEM, and
    Administrators, dropping inherited ACLs. This matters most if BridgeBox
    is ever installed to a machine-wide, world-readable path - the CA is
    trusted machine-wide (install_ca_windows), so a readable private key
    there would let any other local account forge trusted certificates.

    Best-effort: a failure here (e.g. icacls unavailable) must not block
    certificate issuance, so any error is reported rather than raised. It is
    reported, though - this is the only thing standing between the CA private
    key and any other local account, and a silent failure left the key
    inheriting whatever ACL the folder had with nothing on screen or in the
    log to say so."""
    username = os.environ.get("USERNAME", "")
    if not username:
        logger.error(
            "USERNAME is unset - cannot restrict %s to the current user; "
            "the CA private key may be readable by other local accounts",
            path,
        )
        return
    try:
        result = runner(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                # Full, not read-only. The threat is OTHER local accounts; the
                # account that owns the key has to be able to rewrite it, and
                # ":R" made reissuing the CA fail with PermissionError - which
                # is the exact path the name-constraints migration depends on.
                f"{username}:F",
                f"*{SID_SYSTEM}:F",
                f"*{SID_ADMINISTRATORS}:F",
            ],
            capture_output=True,
            creationflags=_NO_WINDOW,
            timeout=ICACLS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error(
            "could not restrict permissions on %s (%s) - the CA private key "
            "may be readable by other local accounts",
            path,
            exc,
        )
        return

    # Injected runners in tests return None or a bare stub, so every field is
    # read defensively - a reporting path must not become its own failure.
    returncode = getattr(result, "returncode", 0)
    if returncode != 0:
        stderr = getattr(result, "stderr", b"") or b""
        if isinstance(stderr, bytes):
            stderr = _decode_console(stderr)
        logger.error(
            "icacls failed on %s (rc=%s): %s - the CA private key may be "
            "readable by other local accounts",
            path,
            returncode,
            str(stderr).strip(),
        )


# What this CA is allowed to vouch for, enforced by the OS rather than by
# trusting that we only ever sign localhost.
#
# This CA is installed machine-wide into the Trusted Root store, and its
# private key sits unencrypted next to a portable install. Without this
# extension, anyone who reads that key can mint a trusted certificate for ANY
# domain and MITM every TLS connection this machine makes. With it, the same
# stolen key is worth exactly what the bridge needs it for and nothing else -
# Windows' CryptoAPI enforces name constraints during chain validation.
#
# Only DNS and IP are constrained, and that is the whole threat: server-auth
# chain validation matches a certificate to a host by its DNS/IP SANs, so a
# forged certificate that cannot carry a DNS or IP name outside this list
# cannot impersonate a site. RFC 5280 leaves other name types (email, URI,
# directory) unconstrained here, which is deliberate - they play no part in
# the TLS server-auth path, and cryptography rejects the empty-value form
# that would be needed to exclude them wholesale.
CA_PERMITTED_NAMES = [
    x509.DNSName("localhost"),
    x509.IPAddress(ipaddress.IPv4Network("127.0.0.0/8")),
    x509.IPAddress(ipaddress.IPv6Network("::1/128")),
]


def _has_extensions(cert_path: Path, *wanted) -> bool:
    """Whether the certificate at `cert_path` carries all of `wanted`.

    Used to decide whether an existing certificate is still good enough to
    reuse. A certificate issued by an older BridgeBox is missing extensions
    that are now required, and reusing it silently is how a machine ends up
    serving something that no longer validates."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        for extension in wanted:
            cert.extensions.get_extension_for_class(extension)
        return True
    except (x509.ExtensionNotFound, ValueError, OSError):
        return False


def generate_ca(cert_dir: str | Path, *, force: bool = False, runner=subprocess.run) -> CertPaths:
    """Generate (or reuse) the BridgeBox root CA in cert_dir.

    Reissues an existing CA that has no NameConstraints extension - see
    CA_PERMITTED_NAMES. Callers that install the CA into the trust store must
    notice this and reinstall; runtime_core does that by way of the
    .ca-installed marker, which is cleared here."""
    cert_dir = Path(cert_dir)
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / CA_CERT_FILENAME
    key_path = cert_dir / CA_KEY_FILENAME

    if not force and cert_path.exists() and key_path.exists():
        if _has_extensions(cert_path, x509.NameConstraints, x509.SubjectKeyIdentifier):
            return CertPaths(cert_path, key_path)
        logger.warning(
            "existing CA at %s predates the name constraints and/or the key "
            "identifier - reissuing it",
            cert_path,
        )
        # The leaf is signed by the CA being replaced, so it has to go too, or
        # generate_leaf_cert would happily reuse a leaf that no longer chains.
        for stale in (cert_dir / LEAF_CERT_FILENAME, cert_dir / LEAF_KEY_FILENAME):
            stale.unlink(missing_ok=True)
        # Cleared so runtime_core reinstalls the new CA instead of trusting the
        # marker and leaving the machine with only the old one trusted.
        (cert_dir / CA_INSTALLED_MARKER).unlink(missing_ok=True)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
    now = datetime.now(timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - NOT_BEFORE_SKEW)
        .not_valid_after(now + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.NameConstraints(
                permitted_subtrees=CA_PERMITTED_NAMES,
                excluded_subtrees=None,
            ),
            critical=True,
        )
        # RFC 5280 4.2.1.2: a CA MUST carry this, and it is not decoration -
        # it is what lets a verifier tie an issued certificate back to this
        # exact key. OpenSSL 3 refuses the chain outright without the matching
        # pair (measured: "certificate verify failed: Missing Authority Key
        # Identifier"), which is what broke the connection test once it began
        # verifying instead of running with CERT_NONE.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    _write_private_key(key_path, key)
    _harden_key_permissions(key_path, runner=runner)
    _write_cert(cert_path, cert)
    return CertPaths(cert_path, key_path)


def generate_leaf_cert(cert_dir: str | Path, *, force: bool = False, runner=subprocess.run) -> CertPaths:
    """Generate (or reuse) a localhost leaf certificate signed by the BridgeBox CA."""
    cert_dir = Path(cert_dir)
    ca_paths = generate_ca(cert_dir, runner=runner)
    cert_path = cert_dir / LEAF_CERT_FILENAME
    key_path = cert_dir / LEAF_KEY_FILENAME

    # A leaf from an older BridgeBox has no AuthorityKeyIdentifier, and OpenSSL
    # 3 will not build a chain to the CA without one - so it is reissued rather
    # than reused, exactly as generate_ca does for its own missing extensions.
    if (
        not force
        and cert_path.exists()
        and key_path.exists()
        and _has_extensions(cert_path, x509.AuthorityKeyIdentifier)
    ):
        return CertPaths(cert_path, key_path)

    ca_key = serialization.load_pem_private_key(ca_paths.key.read_bytes(), password=None)
    ca_cert = x509.load_pem_x509_certificate(ca_paths.cert.read_bytes())

    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - NOT_BEFORE_SKEW)
        .not_valid_after(now + LEAF_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv6Address("::1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(leaf_key.public_key()),
            critical=False,
        )
        # The other half of the pair described on the CA's own SKI above.
        # Without it OpenSSL 3 cannot connect this certificate to the CA that
        # signed it and fails the whole chain.
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _write_private_key(key_path, leaf_key)
    _harden_key_permissions(key_path, runner=runner)
    _write_cert(cert_path, cert)
    return CertPaths(cert_path, key_path)


def install_ca_windows(ca_cert_path: str | Path, *, runner=subprocess.run) -> bool:
    """Install the BridgeBox root CA into the Windows 'ROOT' (Trusted Root) store.

    Assumes the current process is already elevated (BridgeBox requires admin
    at startup for Zapret/WinDivert), so this issues certutil directly rather
    than triggering its own separate UAC prompt.

    Any earlier BridgeBox CA is removed from the store first. Without that, an
    old unconstrained CA (see CA_PERMITTED_NAMES) would stay trusted forever
    alongside the new one, and reissuing would harden nothing at all - the
    weaker certificate is the one an attacker would use. They share a subject
    name, so one -delstore covers every previous generation.
    """
    try:
        removed = runner(
            ["certutil", "-delstore", "ROOT", CA_COMMON_NAME],
            capture_output=True,
            creationflags=_NO_WINDOW,
            timeout=CERTUTIL_TIMEOUT_S,
        )
        if getattr(removed, "returncode", 1) == 0:
            logger.info("removed a previously trusted %s from the ROOT store", CA_COMMON_NAME)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Nothing to remove is the normal case and certutil reports it as a
        # non-zero exit, not an exception - only a genuinely broken certutil
        # lands here, and that is the -addstore below's problem to report.
        logger.debug("certutil -delstore did not run: %s", exc)

    try:
        result = runner(
            ["certutil", "-addstore", "-f", "ROOT", str(ca_cert_path)],
            capture_output=True,
            creationflags=_NO_WINDOW,
            timeout=CERTUTIL_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("certutil could not install the CA: %s", exc)
        return False
    return result.returncode == 0
