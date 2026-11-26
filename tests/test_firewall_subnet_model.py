"""MODEL (python re-statement) of the argument validation in
verification/desktop_native_prep/scripts/windows/desktop-firewall.ps1 @ e60b73f. NOT the script:
no PowerShell was run. It pins what the script's arithmetic should accept/refuse if the model is faithful."""
import ipaddress, re, pytest

def model(subnet: str, prefix: int):
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", subnet): return "pattern"
    if not 16 <= prefix <= 30: return "prefix"
    try: a = ipaddress.IPv4Address(subnet)
    except ValueError: return "parse"
    b = a.packed
    private = b[0] == 10 or (b[0] == 172 and 16 <= b[1] <= 31) or (b[0] == 192 and b[1] == 168)
    if not private: return "not-private"
    n = int(a)
    if n & ((1 << (32 - prefix)) - 1): return "not-network-address"
    return "ok"

@pytest.mark.parametrize("s,p,want", [
    ("192.168.1.0", 24, "ok"), ("192.168.0.0", 16, "ok"), ("10.20.0.0", 16, "ok"), ("10.0.0.0", 16, "ok"),
    ("172.16.0.0", 16, "ok"), ("172.31.255.0", 24, "ok"), ("192.168.1.128", 25, "ok"), ("192.168.1.252", 30, "ok"),
    ("192.168.1.5", 24, "not-network-address"), ("192.168.1.130", 25, "not-network-address"),
    ("192.168.1.0", 8, "prefix"), ("192.168.1.0", 15, "prefix"), ("192.168.1.0", 31, "prefix"), ("192.168.1.0", 32, "prefix"),
    ("10.0.0.0", 8, "prefix"),  # whole 10/8 cannot be allowed by this script
    ("172.32.0.0", 16, "not-private"), ("172.15.0.0", 16, "not-private"), ("8.8.8.0", 24, "not-private"),
    ("100.64.0.0", 16, "not-private"), ("169.254.0.0", 16, "not-private"), ("127.0.0.0", 24, "not-private"),
    ("0.0.0.0", 16, "not-private"), ("192.169.1.0", 24, "not-private"),
    ("192.168.1", 24, "pattern"), ("::1", 24, "pattern"), ("192.168.1.0/24", 24, "pattern"), ("a.b.c.d", 24, "pattern"),
    ("192.168.1.256", 24, "parse"),
])
def test_model(s, p, want):
    assert model(s, p) == want

def test_every_ok_network_is_inside_rfc1918():
    priv = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    for s, p in (("10.5.0.0", 16), ("172.20.4.0", 22), ("192.168.9.0", 24)):
        assert model(s, p) == "ok" and any(ipaddress.ip_network(f"{s}/{p}").subnet_of(n) for n in priv)
