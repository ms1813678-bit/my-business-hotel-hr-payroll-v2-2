"""Opt-in HOnY desktop/service settings. No database or GUI imports."""
import ipaddress
import json
import re
import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DesktopConfig:
    origin: str
    bind_ipv4: str
    certificate: Path
    private_key: Path
    project_root: Path

    @property
    def hostname(self) -> str:
        return urlsplit(self.origin).hostname or ""

    @property
    def port(self) -> int:
        return urlsplit(self.origin).port or 443

    @classmethod
    def load_origin(cls, path: str | Path) -> str:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return cls.validate_origin(value["origin"])

    @staticmethod
    def validate_origin(origin: str) -> str:
        parsed = urlsplit(origin)
        hostname = parsed.hostname or ""
        if (parsed.scheme != "https" or not hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or hostname.endswith(".")
                or hostname.lower() != hostname or origin != f"https://{parsed.netloc}"):
            raise ValueError("One canonical HTTPS origin without path or credentials is required.")
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            raise ValueError("The canonical origin must use a DNS hostname, not an IP.")
        if hostname == "localhost" or "." not in hostname:
            raise ValueError("A certificate-covered DNS hostname is required.")
        if parsed.port == 443:
            raise ValueError("Omit the default HTTPS port 443 from the canonical origin.")
        if parsed.port == 0 or not 1 <= (parsed.port or 443) <= 65535:
            raise ValueError("Invalid TLS port.")
        if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname):
            raise ValueError("Invalid canonical DNS name.")
        expected = f"https://{hostname}"
        if parsed.port is not None:
            expected += f":{parsed.port}"
        if origin != expected:
            raise ValueError("Origin must be in exact canonical lowercase form.")
        origin.encode("ascii")
        return origin

    @classmethod
    def load(cls, path: str | Path) -> "DesktopConfig":
        config_path = Path(path).resolve(strict=True)
        value = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict) or set(value) != {
            "origin", "bind_ipv4", "certificate", "private_key", "project_root"
        }:
            raise ValueError("Desktop configuration fields do not match the schema.")
        if not all(isinstance(item, str) and item for item in value.values()):
            raise ValueError("Desktop configuration values must be nonempty strings.")
        origin = cls.validate_origin(value["origin"])
        address = ipaddress.IPv4Address(value["bind_ipv4"])
        # RFC1918 only: is_private also includes special-use ranges.
        networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        if not any(address in ipaddress.IPv4Network(net) for net in networks):
            raise ValueError("The service must bind one RFC1918 hotel LAN IPv4 address.")
        paths = {}
        for key in ("certificate", "private_key", "project_root"):
            candidate = Path(value[key])
            if not candidate.is_absolute():
                raise ValueError("Runtime paths must be absolute.")
            paths[key] = candidate.resolve(strict=True)
        if not paths["project_root"].is_dir() or not (paths["project_root"] / ".env").is_file():
            raise ValueError("Project root must contain the existing .env configuration.")
        if not paths["certificate"].is_file() or not paths["private_key"].is_file():
            raise ValueError("Certificate and private key must be files.")
        if paths["certificate"] == paths["private_key"]:
            raise ValueError("Certificate and private key must have separate paths.")
        return cls(origin, str(address), paths["certificate"], paths["private_key"], paths["project_root"])

    def validate_key_pair(self) -> None:
        """Check the files match before starting TLS, without ignoring client trust."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(self.certificate), str(self.private_key))
