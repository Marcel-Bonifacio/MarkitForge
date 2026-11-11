# SPDX-FileCopyrightText: 2026-present MarkitForge contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Network safety helpers for fetching untrusted URLs (SSRF protection)."""

import ipaddress
import socket
from typing import Callable, List
from urllib.parse import urlparse

from ._exceptions import ResourceLimitException


class BlockedURLException(ResourceLimitException):
    """Raised when a URL points at a private, loopback or otherwise internal address."""


Resolver = Callable[[str, int], List[str]]


def _resolve(host: str, port: int) -> List[str]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


def _is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])  # drop IPv6 zone id
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not addr.is_multicast


def check_public_url(url: str, resolver: Resolver = _resolve) -> None:
    """Refuse URLs whose host resolves to any non-public address.

    Every address the name resolves to must be public, so a record set mixing
    a public and a private IP is refused. This is a check-then-connect guard:
    a DNS server that answers differently on the next lookup (rebinding) can
    still win the race, so treat it as a strong filter, not a sandbox.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise BlockedURLException("Only http and https URLs are allowed.")
    host = parsed.hostname
    if not host:
        raise BlockedURLException("URL has no host.")
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        addresses = resolver(host, port)
    except (socket.gaierror, UnicodeError) as exc:
        raise BlockedURLException(f"Could not resolve host {host!r}.") from exc
    if not addresses:
        raise BlockedURLException(f"Could not resolve host {host!r}.")
    for ip in addresses:
        try:
            public = _is_public(ip)
        except ValueError:
            public = False
        if not public:
            raise BlockedURLException(
                f"Refusing to fetch {host!r}: it resolves to a private or "
                "internal address."
            )
