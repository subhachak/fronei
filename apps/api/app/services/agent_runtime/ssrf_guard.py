from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class SSRFViolation(Exception):
    """Raised when a URL targets a non-public network address."""


def check_url_public(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SSRFViolation("URL must use http or https")
    host = parsed.hostname
    if not host:
        raise SSRFViolation("URL is missing a hostname")
    candidates = _resolve_host(host)
    if not candidates:
        candidates = [host]
    for candidate in candidates:
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            if candidate.lower() in {"localhost", "metadata.google.internal"}:
                raise SSRFViolation("URL hostname is not public")
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise SSRFViolation("URL resolves to a non-public address")


def _resolve_host(host: str) -> list[str]:
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]
    except Exception:
        return []
