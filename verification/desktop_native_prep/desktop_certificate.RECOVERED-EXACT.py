"""Offline validation and optional strict health probe for certificate activation."""
import argparse
import ssl
import time
import urllib.error
from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from app.desktop_config import DesktopConfig
from app.desktop_server import config_path


def validate_certificate(config: DesktopConfig, now=None, min_remaining_days=7) -> None:
    config.validate_key_pair()
    certificate = x509.load_pem_x509_certificate(config.certificate.read_bytes())
    now = now or datetime.now(UTC)
    if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc - timedelta(days=min_remaining_days):
        raise ValueError(f"Certificate is not currently valid with the required {min_remaining_days}-day remaining lifetime.")
    try:
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound:
        usage = None
    if usage is not None and ExtendedKeyUsageOID.SERVER_AUTH not in usage:
        raise ValueError("Certificate extended key usage does not allow TLS server authentication.")
    try:
        key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        key_usage = None
    if key_usage is not None and not key_usage.digital_signature:
        raise ValueError("Certificate key usage does not allow modern TLS signatures.")
    try:
        constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        constraints = None
    if constraints is not None and constraints.ca:
        raise ValueError("A CA certificate cannot be used as the server leaf.")
    # ACME certificates for this deployment must have the exact DNS SAN. No CN fallback.
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as exc:
        raise ValueError("Certificate has no DNS SAN extension.") from exc
    if config.hostname not in names.get_values_for_type(x509.DNSName):
        raise ValueError("Certificate DNS SAN does not contain the canonical hostname.")


def wait_for_backend(origin: str, seconds: int, *, probe=None, clock=None, sleep=None):
    """Retry transient readiness only. TLS verification failures are final."""
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 0 <= seconds <= 60:
        raise ValueError("Health readiness wait must be 0 to 60 seconds.")
    if probe is None:
        from app.desktop_window import check_backend
        probe = check_backend
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    deadline = clock() + seconds
    while True:
        try:
            probe(origin)
            return
        except (OSError, RuntimeError) as error:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            if isinstance(reason, ssl.SSLCertVerificationError) or clock() >= deadline:
                raise
            sleep(min(1, max(0, deadline - clock())))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(config_path()))
    parser.add_argument('--health', action='store_true')
    parser.add_argument('--structure-only', action='store_true')
    parser.add_argument('--wait-seconds', type=int, default=0)
    args = parser.parse_args()
    config = DesktopConfig.load(args.config)
    if args.structure_only:
        if args.health:
            raise ValueError("Structural inspection is not a health check.")
        config.validate_key_pair()
        return
    validate_certificate(config, min_remaining_days=0 if args.health else 7)
    if args.health:
        wait_for_backend(config.origin, args.wait_seconds)


if __name__ == '__main__':
    main()
