#!/usr/bin/env python3
"""Resolve and validate the local artifact server's Docker publish address."""

import ipaddress
import json
import subprocess
import sys
from urllib.parse import urlsplit


def resolve_endpoint(url):
    if any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError("artifact_server_url must not contain whitespace or control characters")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or "?" in url
        or "#" in url
    ):
        raise ValueError("artifact_server_url must be http://IPv4[:PORT] without credentials or a path")
    address = ipaddress.IPv4Address(parsed.hostname)
    if address.is_unspecified or address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        raise ValueError("artifact_server_url must name a specific local IPv4 address")
    port = parsed.port
    if parsed.netloc.endswith(":") or port == 0:
        raise ValueError("artifact_server_url port must be between 1 and 65535")
    port = 80 if port is None else port

    result = subprocess.run(
        ["ip", "-j", "-4", "address", "show"],
        check=True, capture_output=True, text=True,
    )
    addresses = {
        entry.get("local")
        for interface in json.loads(result.stdout)
        for entry in interface.get("addr_info", [])
    }
    if str(address) not in addresses:
        raise ValueError(
            f"{address} is not assigned locally; run 'task networks-up' first "
            "and check artifact_server_url against networks.mgmt.bridge_ip"
        )
    return f"{address}:{port}"


if __name__ == "__main__":
    try:
        print(resolve_endpoint(sys.argv[1]))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit(f"Error resolving artifact_server_url: {error}")
