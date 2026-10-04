"""Boiler transport factory: pick the driver from app config.

The real gateway drivers (``OTGWTransportDrv`` / ``OTDirectTransportDrv``)
land later; until then each backend maps to its debug dummy, which mirrors
the real gateway's commands and behaviour (AGENTS.md, OTGW section). When a
real driver is written, swap it in here -- nothing else changes.
"""


def make_transport(config):
    """Return a ``BoilerTransport`` driver for ``config["transport"]``.

    ``"otgw"`` (the default) is the OTGW dummy with its re-assert cadence
    taken from ``cs_reassert_s``; ``"direct_ot"`` is the direct-OpenTherm
    dummy (no re-assert obligation).
    """
    name = config.get("transport")
    if name == "direct_ot":
        from transport.dummy_directot import DummyDirectOT
        return DummyDirectOT()
    from transport.dummy_otgw import DummyOTGW
    return DummyOTGW(reassert_s=config.get("cs_reassert_s", 30))
