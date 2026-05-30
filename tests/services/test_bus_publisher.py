"""Tests for BusPublisherMixin — the fail-soft optional-bus emit helper."""

from socialhome.services.bus_publisher import BusPublisherMixin


class _FakeBus:
    def __init__(self):
        self.published = []

    async def publish(self, event):
        self.published.append(event)


class _Svc(BusPublisherMixin):
    __slots__ = ("_bus",)

    def __init__(self, bus):
        self._bus = bus


async def test_emit_publishes_when_bus_present():
    bus = _FakeBus()
    svc = _Svc(bus)
    await svc._emit("event-1")
    await svc._emit("event-2")
    assert bus.published == ["event-1", "event-2"]


async def test_emit_is_noop_when_bus_is_none():
    svc = _Svc(None)
    # Must not raise — a service constructed without a bus simply skips.
    await svc._emit("ignored")


def test_mixin_owns_no_slots_so_it_composes():
    # __slots__ == () means the mixin adds no instance layout, so it can
    # be combined with another slotted mixin without a layout conflict.
    assert BusPublisherMixin.__slots__ == ()

    class OtherSlotted:
        __slots__ = ("_x",)

    class Combined(OtherSlotted, BusPublisherMixin):
        __slots__ = ("_bus",)

    c = Combined()
    c._x = 1
    c._bus = None
    assert not hasattr(c, "__dict__")
