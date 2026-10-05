"""Boiler transport factory: pick the driver from app config.

``"otgw"`` is the debug dummy (``DummyOTGW``) -- the shipped default until the
gateway is physically wired and validated live. ``"otgw_uart"`` selects the
REAL ``OTGWTransportDrv`` on a ``machine.UART`` link (config ``otgw_baud`` /
``otgw_tx_pin`` / ``otgw_rx_pin`` / ``otgw_ack_timeout_s``); ``"direct_ot"``
stays the direct-OpenTherm dummy (its real driver lands later). The ``link``
argument exists for host tests: pass a gateway simulator and no UART is
built (``machine`` is imported only on the device path).
"""


def make_transport(config, link=None):
    """Return a ``BoilerTransport`` driver for ``config["transport"]``.

    ``"otgw"`` (the default) is the OTGW dummy with its re-assert cadence
    taken from ``cs_reassert_s``; ``"otgw_uart"`` is the real driver with the
    same cadence plus the bounded ack wait; ``"direct_ot"`` is the
    direct-OpenTherm dummy (no re-assert obligation).
    """
    name = config.get("transport")
    if name == "direct_ot":
        from transport.dummy_directot import DummyDirectOT
        return DummyDirectOT()
    if name == "otgw_uart":
        from transport.otgw import OTGWTransportDrv
        if link is None:
            link = _make_otgw_uart(config)
        return OTGWTransportDrv(
            link,
            reassert_s=config.get("cs_reassert_s", 30),
            ack_timeout_ms=int(config.get("otgw_ack_timeout_s", 2)) * 1000)
    from transport.dummy_otgw import DummyOTGW
    return DummyOTGW(reassert_s=config.get("cs_reassert_s", 30))


def _make_otgw_uart(config):
    """Build the UART link to the gateway (device-only path).

    8N1; the PIC gateway firmware speaks 9600 (``otgw_baud``). GP0/GP1 are
    UART0's default pair and are free in the arch §3.2 pin plan.
    """
    import machine
    return machine.UART(
        0,
        baudrate=int(config.get("otgw_baud")),
        bits=8, parity=None, stop=1,
        tx=int(config.get("otgw_tx_pin")),
        rx=int(config.get("otgw_rx_pin")),
    )
