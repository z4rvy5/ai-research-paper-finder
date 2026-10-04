"""Run the whole test suite with every non-loopback network connection blocked.

    uv run python scripts/run_tests_offline.py [pytest arguments]

Any test (or library) that tries to reach a non-local host fails with an error, and the number of
attempts is printed at the end. A clean run reports 0. Loopback is allowed because the event loop
and the in-process test client use it.
"""

import socket
import sys

import pytest

ATTEMPTS: list[str] = []
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _host(address) -> str:
    return str(address[0]) if isinstance(address, tuple) else str(address)


def _guard_connect(original):
    def wrapper(self, address, *args, **kwargs):
        if _host(address) not in LOOPBACK:
            ATTEMPTS.append(f"connect {address}")
            raise OSError(f"NETWORK BLOCKED: {address}")
        return original(self, address, *args, **kwargs)

    return wrapper


def block_network() -> None:
    socket.socket.connect = _guard_connect(socket.socket.connect)
    socket.socket.connect_ex = _guard_connect(socket.socket.connect_ex)
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if host not in LOOPBACK and host not in (None, ""):
            ATTEMPTS.append(f"dns {host}")
            raise OSError(f"NETWORK BLOCKED (dns): {host}")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo


def main() -> int:
    block_network()
    code = pytest.main(["tests", "-q", "-p", "no:cacheprovider", *sys.argv[1:]])
    print(f"\nnon-loopback network attempts during the run: {len(ATTEMPTS)} {ATTEMPTS[:5]}")
    return int(code) or (1 if ATTEMPTS else 0)


if __name__ == "__main__":
    sys.exit(main())
