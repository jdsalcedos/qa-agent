"""Acceptance-only network fence, automatically inherited by Python children.

Never load this helper in production. It prevents even accidental LLM traffic.
Unix-domain sockets used by asyncio are not HTTP destinations.
"""
import ipaddress
import os
import sys


def _loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guard(event, args):
    denied = False
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple):
            denied = os.environ.get("QA_ACCEPTANCE_NETWORK") == "none" or not _loopback(address[0])
    elif event == "socket.getaddrinfo":
        denied = os.environ.get("QA_ACCEPTANCE_NETWORK") == "none" or not _loopback(args[0])
    if denied:
        log = os.environ.get("QA_ACCEPTANCE_NETWORK_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write("Denied network event: " + event + "\n")
        raise PermissionError("Acceptance prohibits external networking / offline-close networking")


sys.addaudithook(_guard)
