"""Fake Discord test kit: in-memory stand-ins for discord.py objects.

Tests mock Discord with ad hoc stubs today, which cannot drive the real
``discord.ui.View`` / component machinery that buttons, defers and edits
need. This kit records what production code *does* to Discord instead:

- :class:`FakeTextChannel`, :class:`FakeDMChannel` and :class:`FakeThread`
  are real ``discord`` channel types (so ``isinstance`` checks in the bot
  behave as in production) whose ``send`` records instead of networking.
- :class:`FakeMessage` records ``reply``, ``edit`` and ``delete`` and
  builds real :class:`discord.MessageReference` objects.
- :class:`FakeInteraction` records ``response.defer``,
  ``response.send_message``, ``response.edit_message`` and
  ``followup.send``, with monotonic timestamps so tests can assert an
  interaction was "deferred within N ms".
- Views and components are the *real* discord.py objects. Nothing here
  reimplements them: a message sent with ``view=...`` keeps the actual
  ``discord.ui.View``, and :func:`press` dispatches a component press
  through discord.py's own view dispatch, so ``interaction_check``,
  per-item checks and callbacks run exactly as Discord would run them.

No network is touched anywhere in this module.

Typical use::

    channel = FakeTextChannel()
    user = FakeUser(id=7)
    message = channel.receive_message("hi", author=user)
    interaction = await press(sent_message, "confirm:yes", user)
    interaction.assert_deferred_within(100)
    channel.assert_first_send_is_reply()
    sent_message.assert_buttons_removed()
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import discord

_id_counter = itertools.count(1000)


def _next_id() -> int:
    return next(_id_counter)


@dataclass
class Call:
    """One recorded call against a fake object.

    ``kind`` is the verb (``"send"``, ``"reply"``, ``"edit"``,
    ``"defer"``, ``"followup.send"`` ...), ``at`` a monotonic timestamp
    for ordering and elapsed-time assertions, and ``message`` the
    :class:`FakeMessage` the call produced, when it produced one.
    """

    kind: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.monotonic)
    message: FakeMessage | None = None

    @property
    def content(self) -> Any:
        """The content argument, whether passed positionally or by name."""
        if self.args:
            return self.args[0]
        return self.kwargs.get("content")


class FakeUser:
    """A Discord user. Equality and hashing are by id, like discord.py."""

    def __init__(
        self,
        id: int | None = None,
        *,
        name: str = "catduck",
        display_name: str | None = None,
        bot: bool = False,
    ) -> None:
        self.id = _next_id() if id is None else id
        self.name = name
        self.display_name = name if display_name is None else display_name
        self.bot = bot

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    def __eq__(self, other: object) -> bool:
        other_id = getattr(other, "id", None)
        return other_id is not None and other_id == self.id

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return f"FakeUser(id={self.id}, name={self.name!r}, bot={self.bot})"


class FakeGuild:
    def __init__(self, id: int | None = None, *, name: str = "Fake Guild") -> None:
        self.id = _next_id() if id is None else id
        self.name = name

    def __repr__(self) -> str:
        return f"FakeGuild(id={self.id}, name={self.name!r})"


class FakeTyping:
    """Async context manager returned by ``channel.typing()``.

    Records enter/exit on the channel and raises the channel's configured
    ``typing_enter_error`` / ``typing_exit_error`` when set, so tests can
    prove a typing-indicator failure never blocks the real reply.
    """

    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel

    async def __aenter__(self) -> FakeTyping:
        self.channel.calls.append(Call("typing.enter"))
        self.channel.typing_calls += 1
        if self.channel.typing_enter_error is not None:
            raise self.channel.typing_enter_error
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self.channel.calls.append(Call("typing.exit"))
        if self.channel.typing_exit_error is not None:
            raise self.channel.typing_exit_error
        return False


class FakeChannel:
    """Shared recording behavior for the fake channel types.

    ``sent`` holds every outgoing message in order (channel sends, message
    replies, interaction responses and followups), ``messages`` also
    includes incoming messages made with :meth:`receive_message`, and
    ``calls`` is the full ordered call log.
    """

    id: int
    name: str | None

    # ``discord.DMChannel.guild`` is a read-only property, so the guild is
    # stored privately and surfaced through this property on every fake
    # channel type.
    @property
    def guild(self) -> FakeGuild | None:
        return self._fake_guild

    @guild.setter
    def guild(self, value: FakeGuild | None) -> None:
        self._fake_guild = value

    def _init_channel(
        self, id: int | None, name: str | None, guild: FakeGuild | None
    ) -> None:
        self.id = _next_id() if id is None else id
        self.name = name
        self.guild = guild
        self.calls: list[Call] = []
        self.sent: list[FakeMessage] = []
        self.messages: list[FakeMessage] = []
        self.typing_calls = 0
        # Failure injection: set one of these to an exception instance to
        # make the matching operation raise it, as Discord would.
        self.send_error: Exception | None = None
        self.typing_enter_error: Exception | None = None
        self.typing_exit_error: Exception | None = None

    def typing(self) -> FakeTyping:
        return FakeTyping(self)

    async def send(self, content: Any = None, **kwargs: Any) -> FakeMessage:
        if self.send_error is not None:
            self.calls.append(Call("send", (content,), kwargs))
            raise self.send_error
        message = FakeMessage(
            content, channel=self, author=None, view=kwargs.get("view")
        )
        message.reference = kwargs.get("reference")
        call = Call("send", (content,), kwargs, message=message)
        self.calls.append(call)
        self.sent.append(message)
        return message

    def receive_message(
        self,
        content: str,
        *,
        author: FakeUser | None = None,
        mentions: list[FakeUser] | None = None,
        **kwargs: Any,
    ) -> FakeMessage:
        """Create an incoming message without recording a send."""
        return FakeMessage(
            content,
            channel=self,
            author=author or FakeUser(),
            mentions=mentions,
            **kwargs,
        )

    @property
    def send_calls(self) -> list[Call]:
        return [c for c in self.calls if c.kind in ("send", "reply")]

    def assert_first_send_is_reply(self, to: FakeMessage | None = None) -> Call:
        """Assert the first outgoing send replied to a message.

        A send counts as a reply when it went through ``message.reply()``
        or carried a ``reference`` to the target message, which is how the
        bot sends its replies.
        """
        sends = self.send_calls
        assert sends, "channel recorded no sends"
        first = sends[0]
        reference = first.kwargs.get("reference")
        if first.kind != "reply" and reference is None:
            raise AssertionError(f"first send was a plain send, not a reply: {first!r}")
        if to is not None:
            ref_id = getattr(reference, "message_id", None)
            if first.kind == "reply":
                ref_id = first.kwargs.get("_reply_to_id", ref_id)
            assert ref_id == to.id, (
                f"first send replied to message {ref_id}, not {to.id}"
            )
        return first

    def __repr__(self) -> str:
        return f"{type(self).__name__}(id={self.id}, name={self.name!r})"


class FakeTextChannel(FakeChannel, discord.TextChannel):
    """A ``discord.TextChannel`` that records instead of networking."""

    def __init__(
        self,
        id: int | None = None,
        *,
        name: str | None = "general",
        guild: FakeGuild | None = None,
    ) -> None:
        self._init_channel(id, name, guild)


class FakeDMChannel(FakeChannel, discord.DMChannel):
    """A ``discord.DMChannel`` that records instead of networking."""

    def __init__(
        self,
        id: int | None = None,
        *,
        recipient: FakeUser | None = None,
        guild: None = None,
    ) -> None:
        self._init_channel(id, None, None)
        # DMChannel.recipient is a read-only property in discord.py.
        self._fake_recipient = recipient or FakeUser()


class FakeThread(FakeChannel, discord.Thread):
    """A ``discord.Thread`` that records instead of networking."""

    def __init__(
        self,
        id: int | None = None,
        *,
        name: str | None = "thread",
        parent_id: int | None = None,
        guild: FakeGuild | None = None,
    ) -> None:
        self._init_channel(id, name, guild)
        self.parent_id = parent_id


class FakeMessage:
    """A Discord message recording ``reply``, ``edit`` and ``delete``."""

    def __init__(
        self,
        content: str | None = None,
        *,
        id: int | None = None,
        channel: FakeChannel,
        author: FakeUser | None = None,
        mentions: list[FakeUser] | None = None,
        view: discord.ui.View | None = None,
        reference: Any = None,
    ) -> None:
        self.id = _next_id() if id is None else id
        self.content = content
        self.channel = channel
        self.author = author if author is not None else FakeUser(bot=True)
        self.mentions = list(mentions or [])
        self.view = view
        self.reference = reference
        self.created_at = datetime.now(UTC)
        self.edited_at: datetime | None = None
        self.calls: list[Call] = []
        self.edits: list[Call] = []
        self.replies: list[FakeMessage] = []
        self.to_reference_calls: list[dict[str, Any]] = []
        self.deleted = False
        channel.messages.append(self)

    @property
    def guild(self) -> FakeGuild | None:
        return self.channel.guild

    @property
    def jump_url(self) -> str:
        return f"https://discord.com/channels/fake/{self.channel.id}/{self.id}"

    def to_reference(
        self, *, fail_if_not_exists: bool = True, **kwargs: Any
    ) -> discord.MessageReference:
        """A real MessageReference, as discord.py builds one."""
        self.to_reference_calls.append(
            {"fail_if_not_exists": fail_if_not_exists, **kwargs}
        )
        return discord.MessageReference(
            message_id=self.id,
            channel_id=self.channel.id,
            fail_if_not_exists=fail_if_not_exists,
        )

    async def reply(self, content: Any = None, **kwargs: Any) -> FakeMessage:
        kwargs.setdefault("reference", self.to_reference(fail_if_not_exists=False))
        kwargs.setdefault("mention_author", False)
        message = FakeMessage(
            content, channel=self.channel, author=None, view=kwargs.get("view")
        )
        message.reference = kwargs["reference"]
        call = Call(
            "reply", (content,), {**kwargs, "_reply_to_id": self.id}, message=message
        )
        self.calls.append(call)
        self.channel.calls.append(call)
        self.channel.sent.append(message)
        self.replies.append(message)
        return message

    async def edit(self, **kwargs: Any) -> FakeMessage:
        call = Call("edit", (), kwargs)
        self.calls.append(call)
        self.edits.append(call)
        if "content" in kwargs:
            self.content = kwargs["content"]
        if "view" in kwargs:
            self.view = kwargs["view"]
        self.edited_at = datetime.now(UTC)
        return self

    async def delete(self, **kwargs: Any) -> None:
        self.calls.append(Call("delete", (), kwargs))
        self.deleted = True

    def assert_buttons_removed(self) -> None:
        """Assert no live components remain on this message.

        Buttons are removed by editing with ``view=None`` or with a view
        that has no children left, which is what the final edit (or the
        current view state) must show.
        """
        if self.view is not None and self.view.children:
            raise AssertionError(
                f"message still has buttons: "
                f"{[c.custom_id for c in self.view.children]}"
            )

    def __repr__(self) -> str:
        return f"FakeMessage(id={self.id}, content={self.content!r})"


class FakeInteractionResponse:
    """Records ``defer`` / ``send_message`` / ``edit_message``.

    Like the real thing, responding twice raises
    :class:`discord.InteractionResponded`.
    """

    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction
        self.calls: list[Call] = []
        self.deferred_at: float | None = None
        self.message: FakeMessage | None = None
        self._done = False

    def is_done(self) -> bool:
        return self._done

    def _claim(self, kind: str) -> None:
        if self._done:
            raise discord.InteractionResponded(self._interaction)  # type: ignore[arg-type]

    async def defer(self, **kwargs: Any) -> None:
        self._claim("defer")
        self._done = True
        self.deferred_at = time.monotonic()
        self.calls.append(Call("response.defer", (), kwargs, at=self.deferred_at))

    async def send_message(self, content: Any = None, **kwargs: Any) -> FakeMessage:
        self._claim("send_message")
        self._done = True
        channel = self._interaction.channel
        message = FakeMessage(
            content, channel=channel, author=None, view=kwargs.get("view")
        )
        call = Call("response.send_message", (content,), kwargs, message=message)
        self.calls.append(call)
        channel.calls.append(call)
        channel.sent.append(message)
        self.message = message
        return message

    async def edit_message(self, **kwargs: Any) -> FakeMessage:
        self._claim("edit_message")
        self._done = True
        self.calls.append(Call("response.edit_message", (), kwargs))
        target = self._interaction.message
        assert target is not None, "edit_message needs an interaction message"
        await target.edit(**kwargs)
        self.message = target
        return target


class FakeFollowup:
    """The followup webhook: usable only after the interaction responded."""

    def __init__(self, interaction: FakeInteraction) -> None:
        self._interaction = interaction
        self.calls: list[Call] = []
        self.sent: list[FakeMessage] = []

    async def send(self, content: Any = None, **kwargs: Any) -> FakeMessage:
        if not self._interaction.response.is_done():
            raise RuntimeError(
                "followup.send before the interaction was deferred or answered"
            )
        channel = self._interaction.channel
        message = FakeMessage(
            content, channel=channel, author=None, view=kwargs.get("view")
        )
        call = Call("followup.send", (content,), kwargs, message=message)
        self.calls.append(call)
        channel.calls.append(call)
        channel.sent.append(message)
        self.sent.append(message)
        return message

    async def edit_message(self, message_id: int, **kwargs: Any) -> FakeMessage:
        target = next(
            m for m in self._interaction.channel.messages if m.id == message_id
        )
        self.calls.append(Call("followup.edit_message", (), kwargs))
        await target.edit(**kwargs)
        return target


class FakeInteraction:
    """A component interaction against a fake message.

    ``created_monotonic`` is taken at construction; :func:`press` builds
    the interaction immediately before dispatch, so
    :meth:`assert_deferred_within` measures the callback's defer latency
    the way Discord's 3 second acknowledgement window would.
    """

    type = discord.InteractionType.component

    def __init__(
        self,
        *,
        message: FakeMessage | None = None,
        user: FakeUser | None = None,
        channel: FakeChannel | None = None,
        data: dict[str, Any] | None = None,
        guild: FakeGuild | None = None,
    ) -> None:
        self.id = _next_id()
        self.message = message
        self.user = user or FakeUser()
        self.channel = (
            channel
            if channel is not None
            else (message.channel if message is not None else FakeTextChannel())
        )
        self.guild = guild or self.channel.guild
        self.guild_id = self.guild.id if self.guild is not None else None
        self.channel_id = self.channel.id
        self.data: dict[str, Any] = dict(data or {})
        self.created_at = datetime.now(UTC)
        self.created_monotonic = time.monotonic()
        self.token = "fake-interaction-token"
        self.application_id = _next_id()
        self.response = FakeInteractionResponse(self)
        self.followup = FakeFollowup(self)
        self._original_response: FakeMessage | None = None

    @property
    def custom_id(self) -> str | None:
        return self.data.get("custom_id")

    async def original_response(self) -> FakeMessage:
        if self.response.message is not None:
            return self.response.message
        if self.message is not None:
            return self.message
        raise RuntimeError("interaction has no original response")

    async def edit_original_response(self, **kwargs: Any) -> FakeMessage:
        target = await self.original_response()
        await target.edit(**kwargs)
        return target

    async def delete_original_response(self) -> None:
        target = await self.original_response()
        await target.delete()

    def is_expired(self) -> bool:
        return False

    def assert_deferred_within(self, milliseconds: float) -> None:
        """Assert ``response.defer`` landed within ``milliseconds``."""
        deferred_at = self.response.deferred_at
        assert deferred_at is not None, "interaction was never deferred"
        elapsed_ms = (deferred_at - self.created_monotonic) * 1000
        assert elapsed_ms <= milliseconds, (
            f"deferred after {elapsed_ms:.1f} ms, over the {milliseconds} ms budget"
        )

    def __repr__(self) -> str:
        return f"FakeInteraction(id={self.id}, custom_id={self.custom_id!r})"


def assert_deferred_within(interaction: FakeInteraction, milliseconds: float) -> None:
    """Module-level form of :meth:`FakeInteraction.assert_deferred_within`."""
    interaction.assert_deferred_within(milliseconds)


def assert_first_send_is_reply(
    channel: FakeChannel, to: FakeMessage | None = None
) -> Call:
    """Module-level form of :meth:`FakeChannel.assert_first_send_is_reply`."""
    return channel.assert_first_send_is_reply(to)


def assert_buttons_removed(message: FakeMessage) -> None:
    """Module-level form of :meth:`FakeMessage.assert_buttons_removed`."""
    message.assert_buttons_removed()


def _walk_items(view: discord.ui.View) -> list[Any]:
    items: list[Any] = []

    def visit(item: Any) -> None:
        items.append(item)
        for child in getattr(item, "children", []) or []:
            visit(child)

    for child in view.children:
        visit(child)
    return items


def find_component(view: discord.ui.View, custom_id: str) -> Any:
    """Find a view component by custom_id, or raise listing what exists."""
    items = _walk_items(view)
    for item in items:
        if getattr(item, "custom_id", None) == custom_id:
            return item
    available = [getattr(i, "custom_id", None) for i in items]
    raise AssertionError(
        f"no component with custom_id {custom_id!r}; available: {available}"
    )


async def press(
    message: FakeMessage,
    custom_id: str,
    user: FakeUser | None = None,
    *,
    values: list[str] | None = None,
) -> FakeInteraction:
    """Press the component ``custom_id`` on ``message`` as ``user``.

    Builds a :class:`FakeInteraction` and hands it to discord.py's own
    view dispatch (``View._dispatch_item``), so the view's
    ``interaction_check``, the item's checks and the item callback all run
    for real, in a task, exactly as a gateway component interaction would
    drive them. Returns the interaction so tests can assert on what the
    callback deferred, sent, followed up and edited.
    """
    view = message.view
    assert view is not None, f"message {message.id} has no view to press"
    item = find_component(view, custom_id)
    if getattr(item, "disabled", False):
        raise AssertionError(f"component {custom_id!r} is disabled")
    data: dict[str, Any] = {
        "custom_id": custom_id,
        "component_type": int(item.type),
    }
    if values is not None:
        data["values"] = list(values)
    elif hasattr(item, "options"):  # a select with no explicit choice
        data["values"] = []
    interaction = FakeInteraction(message=message, user=user, data=data)
    task = view._dispatch_item(item, interaction)  # noqa: SLF001 - the dispatch entry point
    if task is None:
        raise AssertionError(
            f"view did not dispatch {custom_id!r} (stopped or finished)"
        )
    await task
    return interaction
