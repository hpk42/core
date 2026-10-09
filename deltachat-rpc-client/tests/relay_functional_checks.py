"""Functional checks against two local relays in incus containers.

Set up the relays once with cmlxc (https://github.com/chatmail/cmlxc):

    cmlxc init
    cmlxc deploy-cmdeploy --source @main relay1
    cmlxc deploy-cmdeploy --source @main relay2

Pytest does not collect this file by itself, run it explicitly:

    pytest -s deltachat-rpc-client/tests/relay_functional_checks.py
"""

import logging
import os
import re
import subprocess
import time

import pytest

from deltachat_rpc_client import EventType

KEYS = ("Trying to connect", "Try number", "failed to send", "disconnecting", "Selected rows", "retry", "exceeded")


class Relay:
    """Relay deployed with `cmlxc deploy-cmdeploy NAME` into the container NAME-localchat."""

    def __init__(self, name):
        self.domain = f"_{name}.localchat"
        self.container = f"{name}-localchat"
        self.soft_bounce = False

    def set_soft_bounce(self, enabled):
        """Make postfix answer 4xx instead of 5xx, so that rejections become temporary."""
        cmd = f"postconf -e soft_bounce={'yes' if enabled else 'no'} && postfix reload"
        subprocess.run(["incus", "exec", self.container, "--", "sh", "-c", cmd], check=True, capture_output=True)
        self.soft_bounce = enabled


@pytest.fixture(autouse=True)
def no_debug_logging():
    logging.disable(logging.DEBUG)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def relay1(monkeypatch):
    relay = Relay("relay1")
    # acf creates its accounts on relay1.
    monkeypatch.setenv("CHATMAIL_DOMAIN", relay.domain)
    yield relay
    if relay.soft_bounce:
        relay.set_soft_bounce(False)


@pytest.fixture
def relay2():
    relay = Relay("relay2")
    yield relay
    if relay.soft_bounce:
        relay.set_soft_bounce(False)


def test_rejected_send_moves_to_next_transport(acf, relay1, relay2, tmp_path):
    # Both relays reject the oversized message, relay1 temporarily, relay2 for good.
    relay1.set_soft_bounce(True)

    alice, bob = acf.get_online_accounts(2)
    alice.add_transport_from_qr(f"dcaccount:{relay2.domain}")
    alice.bring_online()
    print("\ntransports in id order:", [t["addr"] for t in alice.list_transports()])
    chat = alice.create_chat(bob)

    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(25 * 1024 * 1024))
    alice.clear_all_events()
    start = time.monotonic()
    msg = chat.send_message(text="big", file=str(big))

    tried_transports = []
    failures = 0
    while failures < 3:
        ev = alice.wait_for_event()
        t = time.monotonic() - start
        if ev.kind == EventType.INFO and any(k in ev.msg for k in KEYS):
            print(f"{t:6.1f}s INFO {ev.msg}")
            if m := re.search(r"Trying to connect to transport (\d+)\.", ev.msg):
                tried_transports.append(int(m.group(1)))
            if "SMTP failed to send" in ev.msg:
                failures += 1
        elif ev.kind == EventType.SMTP_CONNECTED:
            print(f"{t:6.1f}s SMTP_CONNECTED {ev.msg}")
        elif ev.kind == EventType.MSG_FAILED and ev.msg_id == msg.id:
            print(f"{t:6.1f}s MSG_FAILED {msg.get_snapshot().error}")
            break
    assert len(set(tried_transports)) > 1, f"{failures} failed attempts, all on same transport {tried_transports[0]}"
