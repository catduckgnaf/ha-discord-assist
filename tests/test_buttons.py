import copy

import pytest

from custom_components.discord_assist.buttons import (
    MAX_CUSTOM_ID_LENGTH,
    PREFIX,
    ButtonStore,
    make_custom_id,
    parse_custom_id,
)


class FakeStore:
    """Simple in-memory stand-in for HA's Store (async_load / async_save)."""

    def __init__(self, data=None):
        self.data = copy.deepcopy(data)
        self.saves = 0

    async def async_load(self):
        return copy.deepcopy(self.data)

    async def async_save(self, data):
        self.saves += 1
        self.data = copy.deepcopy(data)


class Clock:
    def __init__(self, start=1_000.0):
        self.now = start

    def __call__(self):
        return self.now


def _store(backend=None, clock=None, ttl=60):
    return ButtonStore(
        store=backend or FakeStore(),
        ttl_seconds=ttl,
        now=clock or Clock(),
    )


def test_custom_id_shape_and_parse_roundtrip():
    custom_id = make_custom_id("confirm", "abc123")
    assert custom_id == f"{PREFIX}:confirm:abc123"
    assert parse_custom_id(custom_id) == ("confirm", "abc123")


@pytest.mark.parametrize(
    "custom_id",
    [
        None,
        "",
        "other:confirm:abc123",
        "da",
        "da:confirm",
        "da::abc123",
        "da:confirm:",
        "da:bad action:abc123",
        "da:confirm:bad:id",
        123,
    ],
)
def test_parse_custom_id_ignores_foreign_and_malformed(custom_id):
    assert parse_custom_id(custom_id) is None


def test_custom_id_over_100_chars_is_rejected():
    with pytest.raises((AssertionError, ValueError)):
        make_custom_id("a" * 60, "b" * 60)


async def test_record_asserts_custom_id_length():
    store = _store()
    with pytest.raises((AssertionError, ValueError)):
        await store.record("a" * 60, record_id="b" * 60)
    assert len(store) == 0


async def test_record_generates_short_id_and_persists():
    backend = FakeStore()
    store = _store(backend)
    record = await store.record(
        "confirm", payload={"entity_id": "light.kitchen"}, allowed_user_ids=(7,)
    )
    assert record.custom_id.startswith("da:confirm:")
    assert len(record.custom_id) <= MAX_CUSTOM_ID_LENGTH
    # Ids stay short and never embed entity ids or text.
    assert "light" not in record.record_id
    assert backend.data["records"][record.record_id]["action"] == "confirm"
    assert backend.data["records"][record.record_id]["payload"] == {
        "entity_id": "light.kitchen"
    }


async def test_button_survives_restart():
    """A new ButtonStore over the same persisted data can claim the record."""
    backend = FakeStore()
    clock = Clock()
    first = _store(backend, clock)
    record = await first.record("confirm", allowed_user_ids=(7,))

    restarted = _store(backend, clock)
    peeked = await restarted.peek(record.record_id)
    assert peeked is not None
    assert peeked.custom_id == record.custom_id
    taken = await restarted.take(record.record_id)
    assert taken is not None
    assert taken.record_id == record.record_id


async def test_peek_does_not_consume_and_take_is_single_use():
    store = _store()
    record = await store.record("confirm")
    assert await store.peek(record.record_id) is not None
    assert await store.peek(record.record_id) is not None
    assert await store.take(record.record_id) is not None
    assert await store.take(record.record_id) is None
    assert await store.peek(record.record_id) is None


async def test_expired_record_is_refused_and_pruned():
    backend = FakeStore()
    clock = Clock()
    store = _store(backend, clock, ttl=10)
    record = await store.record("confirm")
    clock.now += 11
    assert await store.peek(record.record_id) is None
    assert await store.take(record.record_id) is None
    assert backend.data["records"] == {}


async def test_expired_records_pruned_on_load():
    clock = Clock()
    backend = FakeStore(
        {
            "records": {
                "old": {
                    "record_id": "old",
                    "action": "confirm",
                    "created_at": 0.0,
                    "expires_at": 10.0,
                    "allowed_user_ids": [],
                    "payload": {},
                },
                "fresh": {
                    "record_id": "fresh",
                    "action": "confirm",
                    "created_at": 900.0,
                    "expires_at": 2000.0,
                    "allowed_user_ids": [],
                    "payload": {},
                },
            }
        }
    )
    store = _store(backend, clock)
    await store.async_load()
    assert await store.peek("old") is None
    assert await store.peek("fresh") is not None
    assert set(backend.data["records"]) == {"fresh"}


async def test_malformed_persisted_rows_are_dropped():
    backend = FakeStore({"records": {"bad": {"nope": True}, "junk": 42}})
    store = _store(backend)
    await store.async_load()
    assert len(store) == 0


def test_record_allows_checks_named_pressers():
    from custom_components.discord_assist.buttons import ButtonRecord

    restricted = ButtonRecord(
        record_id="r1",
        action="confirm",
        created_at=0.0,
        expires_at=10.0,
        allowed_user_ids=(7,),
    )
    assert restricted.allows(7) is True
    assert restricted.allows(8) is False
    open_record = ButtonRecord(
        record_id="r2", action="confirm", created_at=0.0, expires_at=10.0
    )
    assert open_record.allows(8) is True
