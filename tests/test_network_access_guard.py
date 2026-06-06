from __future__ import annotations

import socket

import pytest


def test_non_network_tests_block_socket_connections() -> None:
    with pytest.raises(AssertionError, match="network access attempted in non-network test"):
        socket.create_connection(("127.0.0.1", 9), timeout=0.1)
