"""No globally-routable address of the author's own network in a PUBLIC repo.

Caught in the v0.2.0 audit, 2026-09-16, against work committed hours earlier:
scripts/fix_ufw_exposure.sh carried this host's three global IPv6 addresses in
its closing notes, as the target for the external verification step. The repo
is public. That published the exact addresses an attacker would aim at — inside
the script written to close an exposure on those very addresses.

The secrets lane did not catch it because it looked for key-shaped literals and
for Tailscale's 100.64/10 CGNAT range, and a Comcast global v6 address is
neither. This is the missing check, as a test so the lane cannot forget.

Scoped to what is actually sensitive: a globally-routable address belonging to
this network. Documentation examples use RFC 5737 / RFC 3849 reserved ranges
(192.0.2.0/24, 2001:db8::/32) precisely so they can be written down, and
private ranges are not a disclosure.
"""
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The author's ISP-delegated prefix. A global v6 address outside the
# documentation range is the shape that matters; this names the one we know.
_ROUTABLE_V6 = re.compile(r"\b2601:[0-9a-f]{1,4}:[0-9a-f]{1,4}:[0-9a-f]{1,4}:", re.I)

_ALLOWED = {
    # This file quotes the pattern in order to test for it.
    "tests/test_no_routable_addresses_committed.py",
}


def _tracked_text_files():
    out = subprocess.run(["git", "-C", str(REPO), "ls-files"],
                         capture_output=True, text=True, check=True).stdout
    for rel in out.splitlines():
        if rel in _ALLOWED:
            continue
        p = REPO / rel
        if not p.is_file():
            continue
        try:
            yield rel, p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def test_no_isp_delegated_ipv6_address_is_committed():
    hits = [f"{rel}: {_ROUTABLE_V6.search(text).group(0)}…"
            for rel, text in _tracked_text_files() if _ROUTABLE_V6.search(text)]
    assert not hits, (
        "globally-routable IPv6 address of this network committed to a PUBLIC "
        "repo:\n  " + "\n  ".join(hits) +
        "\nLook them up at runtime instead: ip -6 addr show scope global")


def test_the_guard_would_actually_fire():
    # A guard nobody has seen fail is a guard nobody should trust.
    assert _ROUTABLE_V6.search("nc -6 -vz 2601:646:8685:4f70::a5cb 3389")
    # …and does not fire on the reserved documentation range, which exists to
    # be written down.
    assert not _ROUTABLE_V6.search("2001:db8::1")
    assert not _ROUTABLE_V6.search("fd7a:115c:a1e0::5301:c5d0")  # ULA, private
