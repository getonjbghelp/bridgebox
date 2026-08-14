import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from bridgebox.tls.ca import (
    CA_COMMON_NAME,
    CA_INSTALLED_MARKER,
    generate_ca,
    generate_leaf_cert,
    install_ca_windows,
)


def _load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_generate_ca_creates_self_signed_ca(tmp_path: Path):
    paths = generate_ca(tmp_path)

    assert paths.cert.exists()
    assert paths.key.exists()

    cert = _load_cert(paths.cert)
    basic_constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic_constraints.value.ca is True
    assert cert.issuer == cert.subject  # self-signed

    # key loads without error
    serialization.load_pem_private_key(paths.key.read_bytes(), password=None)


def test_generate_ca_hardens_private_key_permissions(tmp_path: Path):
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0

        return Result()

    paths = generate_ca(tmp_path, runner=fake_runner)

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0] == "icacls"
    assert cmd[1] == str(paths.key)
    assert "/inheritance:r" in cmd


def test_generate_leaf_cert_hardens_private_key_permissions(tmp_path: Path):
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0

        return Result()

    paths = generate_leaf_cert(tmp_path, runner=fake_runner)

    # one call for the CA key (generated as a dependency) + one for the leaf key
    leaf_calls = [c for c in calls if c[1] == str(paths.key)]
    assert len(leaf_calls) == 1
    assert leaf_calls[0][0] == "icacls"


def test_harden_key_permissions_failure_does_not_raise(tmp_path: Path):
    def failing_runner(cmd, **kwargs):
        raise OSError("icacls not found")

    # Must not raise even if the hardening subprocess itself fails to launch -
    # this is defense-in-depth, not something that should block cert issuance.
    generate_ca(tmp_path, runner=failing_runner)


def test_generate_ca_is_idempotent(tmp_path: Path):
    first = generate_ca(tmp_path)
    first_cert_bytes = first.cert.read_bytes()

    second = generate_ca(tmp_path)

    assert second.cert.read_bytes() == first_cert_bytes


def test_generate_ca_force_regenerates(tmp_path: Path):
    first = generate_ca(tmp_path)
    first_cert_bytes = first.cert.read_bytes()

    second = generate_ca(tmp_path, force=True)

    assert second.cert.read_bytes() != first_cert_bytes


def test_generate_leaf_cert_signed_by_ca(tmp_path: Path):
    ca_paths = generate_ca(tmp_path)
    ca_cert = _load_cert(ca_paths.cert)

    leaf_paths = generate_leaf_cert(tmp_path)
    leaf_cert = _load_cert(leaf_paths.cert)

    assert leaf_cert.issuer == ca_cert.subject

    san = leaf_cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    ips = san.get_values_for_type(x509.IPAddress)
    assert ipaddress.IPv4Address("127.0.0.1") in ips

    basic_constraints = leaf_cert.extensions.get_extension_for_class(x509.BasicConstraints)
    assert basic_constraints.value.ca is False


def test_generate_leaf_cert_is_idempotent(tmp_path: Path):
    first = generate_leaf_cert(tmp_path)
    second = generate_leaf_cert(tmp_path)

    assert first.cert.read_bytes() == second.cert.read_bytes()


def test_install_ca_windows_invokes_certutil(tmp_path: Path):
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0

        return Result()

    ca_cert = tmp_path / "ca.pem"
    ca_cert.write_text("fake cert")

    ok = install_ca_windows(ca_cert, runner=fake_runner)

    assert ok is True
    # The removal has to come FIRST. An older, unconstrained BridgeBox CA left
    # trusted alongside the new one is the certificate an attacker would use,
    # so reissuing with name constraints would harden nothing at all.
    assert calls == [
        ["certutil", "-delstore", "ROOT", CA_COMMON_NAME],
        ["certutil", "-addstore", "-f", "ROOT", str(ca_cert)],
    ]


def test_install_ca_windows_returns_false_on_failure(tmp_path: Path):
    def failing_runner(cmd, **kwargs):
        class Result:
            returncode = 1

        return Result()

    ca_cert = tmp_path / "ca.pem"
    ca_cert.write_text("fake cert")

    ok = install_ca_windows(ca_cert, runner=failing_runner)

    assert ok is False


def test_the_ca_is_name_constrained_to_localhost(tmp_path: Path):
    """A stolen CA key must be worth nothing beyond what the bridge needs it
    for. Without this extension it can mint a trusted certificate for any
    domain on a machine that trusts this CA machine-wide."""
    paths = generate_ca(tmp_path, runner=lambda *a, **k: None)
    constraints = _load_cert(paths.cert).extensions.get_extension_for_class(
        x509.NameConstraints
    )

    assert constraints.critical is True
    permitted = constraints.value.permitted_subtrees
    assert x509.DNSName("localhost") in permitted
    assert x509.IPAddress(ipaddress.IPv4Network("127.0.0.0/8")) in permitted
    assert x509.IPAddress(ipaddress.IPv6Network("::1/128")) in permitted


def test_an_old_unconstrained_ca_is_reissued_and_marked_for_reinstall(tmp_path: Path):
    """Installs predating the name constraints must not keep their old CA:
    it is still trusted machine-wide, so leaving it is the whole vulnerability
    the extension exists to close."""
    runner = lambda *a, **k: None  # noqa: E731
    generate_leaf_cert(tmp_path, runner=runner)
    marker = tmp_path / CA_INSTALLED_MARKER
    marker.write_text("installed")

    # Rebuild the pre-fix CA: same code path, minus the extension.
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
    now = datetime.now(timezone.utc)
    legacy = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    ca_cert = tmp_path / "bridgebox-ca.pem"
    ca_cert.write_bytes(legacy.public_bytes(serialization.Encoding.PEM))
    stale_serial = legacy.serial_number

    regenerated = generate_leaf_cert(tmp_path, runner=runner)

    reissued = _load_cert(tmp_path / "bridgebox-ca.pem")
    assert reissued.serial_number != stale_serial
    reissued.extensions.get_extension_for_class(x509.NameConstraints)
    # Cleared so runtime_core reinstalls, instead of trusting the marker and
    # leaving only the removed CA in the store.
    assert not marker.exists()
    # The leaf must chain to the NEW ca, not the discarded one.
    assert _load_cert(regenerated.cert).issuer == reissued.subject


def test_key_permissions_use_well_known_sids_not_localised_group_names(tmp_path: Path):
    """icacls resolves account names in the SYSTEM locale.

    "Administrators:F" fails with 1332 (ERROR_NONE_MAPPED) on a Russian
    install, where the group is "Администраторы" - measured on the developer's
    own machine, where this had been failing silently since the code was
    written. A SID is locale-independent."""
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0
            stderr = b""

        return Result()

    generate_ca(tmp_path, runner=fake_runner)

    cmd = calls[0]
    assert "*S-1-5-32-544:F" in cmd  # BUILTIN\Administrators
    assert "*S-1-5-18:F" in cmd  # NT AUTHORITY\SYSTEM
    assert not any(part.startswith("Administrators") for part in cmd)
    assert not any(part.startswith("SYSTEM") for part in cmd)


def test_the_owning_account_keeps_write_access_to_its_own_key(tmp_path: Path):
    """Granting the current user ":R" locked BridgeBox out of rewriting its own
    CA - which broke reissuing it, the very path the name-constraints
    migration runs through. The threat is other local accounts, not this one."""
    generate_ca(tmp_path)
    first = (tmp_path / "bridgebox-ca-key.pem").read_bytes()

    second = generate_ca(tmp_path, force=True)

    assert second.key.read_bytes() != first


def test_a_failed_icacls_is_reported_rather_than_swallowed(tmp_path: Path, caplog):
    """The only thing standing between the CA private key and other local
    accounts. A silent failure left the key inheriting the folder's ACL with
    nothing on screen or in the log to say so."""

    def failing_runner(cmd, **kwargs):
        class Result:
            returncode = 1332
            stderr = "Нет сопоставления между именами учётных записей".encode("cp866")

        return Result()

    with caplog.at_level("ERROR"):
        generate_ca(tmp_path, runner=failing_runner)

    assert any("icacls failed" in r.getMessage() for r in caplog.records)


def test_the_chain_carries_matching_key_identifiers(tmp_path: Path):
    """OpenSSL 3 refuses a chain whose leaf has no AuthorityKeyIdentifier -
    "certificate verify failed: Missing Authority Key Identifier". Nothing
    caught it for a long time because the connection test ran with
    CERT_NONE, so no certificate was ever actually verified."""
    leaf_paths = generate_leaf_cert(tmp_path)
    ca = _load_cert(tmp_path / "bridgebox-ca.pem")
    leaf = _load_cert(leaf_paths.cert)

    ca_ski = ca.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    leaf_aki = leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier).value
    leaf.extensions.get_extension_for_class(x509.SubjectKeyIdentifier)

    # The identifiers have to actually match, not merely be present.
    assert leaf_aki.key_identifier == ca_ski.digest


def test_the_leaf_really_verifies_against_the_ca(tmp_path: Path):
    """The end-to-end check the extensions above exist for: serve the leaf and
    validate it the way Api.test_connection now does."""
    import socket
    import ssl
    import threading

    paths = generate_leaf_cert(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(paths.cert), str(paths.key))

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        try:
            conn, _ = listener.accept()
            with server_ctx.wrap_socket(conn, server_side=True):
                pass
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    try:
        client_ctx = ssl.create_default_context(cafile=str(tmp_path / "bridgebox-ca.pem"))
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            with client_ctx.wrap_socket(sock, server_hostname="127.0.0.1") as tls:
                assert tls.version() is not None
    finally:
        listener.close()


def test_a_leaf_without_an_authority_key_identifier_is_reissued(tmp_path: Path):
    """Certificates issued by an older BridgeBox must not be reused: they
    validated against nothing, and keeping them means the bridge keeps serving
    a certificate that no verifier will accept."""
    paths = generate_leaf_cert(tmp_path)
    original = paths.cert.read_bytes()

    # Strip the leaf back to its pre-fix shape by reissuing without the pair.
    stripped = _load_cert(paths.cert)
    assert stripped.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    paths.cert.write_bytes(_reissue_without_aki(tmp_path))

    regenerated = generate_leaf_cert(tmp_path)

    assert regenerated.cert.read_bytes() != original
    _load_cert(regenerated.cert).extensions.get_extension_for_class(
        x509.AuthorityKeyIdentifier
    )


def _reissue_without_aki(cert_dir: Path) -> bytes:
    """A leaf shaped the way BridgeBox used to issue them - no key identifiers."""
    from datetime import datetime, timedelta, timezone

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    ca_key = serialization.load_pem_private_key(
        (cert_dir / "bridgebox-ca-key.pem").read_bytes(), password=None
    )
    ca = _load_cert(cert_dir / "bridgebox-ca.pem")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)
