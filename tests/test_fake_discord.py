"""Tests for the fake Discord test kit itself (tests/fake_discord.py)."""

import asyncio

import discord
import pytest
from discord.ui import Button, Select, View

from tests.fake_discord import (
    FakeDMChannel,
    FakeGuild,
    FakeInteraction,
    FakeTextChannel,
    FakeThread,
    FakeUser,
    assert_buttons_removed,
    assert_deferred_within,
    assert_first_send_is_reply,
    find_component,
    press,
)


def test_channels_are_real_discord_channel_types():
    assert isinstance(FakeTextChannel(), discord.TextChannel)
    assert isinstance(FakeDMChannel(), discord.DMChannel)
    thread = FakeThread(parent_id=111)
    assert isinstance(thread, discord.Thread)
    assert thread.parent_id == 111


def test_user_equality_is_by_id():
    assert FakeUser(id=7, name="a") == FakeUser(id=7, name="b")
    assert FakeUser(id=7) != FakeUser(id=8)
    assert FakeUser(id=7).mention == "<@7>"
    assert hash(FakeUser(id=7)) == hash(FakeUser(id=7, name="other"))


async def test_channel_send_records_in_order():
    channel = FakeTextChannel(id=111, name="general")
    first = await channel.send("one")
    second = await channel.send("two", reference=first.to_reference())
    assert channel.sent == [first, second]
    assert [c.content for c in channel.send_calls] == ["one", "two"]
    assert second.reference.message_id == first.id
    assert first.channel is channel


async def test_receive_message_is_not_a_send():
    channel = FakeTextChannel()
    msg = channel.receive_message("incoming", author=FakeUser(id=7))
    assert channel.sent == []
    assert channel.messages == [msg]
    assert msg.author.id == 7


async def test_message_reply_is_recorded_as_reply():
    channel = FakeTextChannel()
    msg = channel.receive_message("question")
    reply = await msg.reply("answer")
    assert reply.content == "answer"
    assert msg.replies == [reply]
    call = assert_first_send_is_reply(channel, to=msg)
    assert call.kind == "reply"
    assert reply.reference.message_id == msg.id


async def test_first_send_is_reply_via_reference():
    channel = FakeTextChannel()
    msg = channel.receive_message("question")
    await channel.send("answer", reference=msg.to_reference(fail_if_not_exists=False))
    assert_first_send_is_reply(channel, to=msg)


async def test_first_send_is_reply_fails_for_plain_send():
    channel = FakeTextChannel()
    await channel.send("not a reply")
    with pytest.raises(AssertionError, match="not a reply"):
        assert_first_send_is_reply(channel)


async def test_to_reference_is_a_real_message_reference():
    channel = FakeTextChannel(id=111)
    msg = channel.receive_message("hi")
    ref = msg.to_reference(fail_if_not_exists=False)
    assert isinstance(ref, discord.MessageReference)
    assert ref.message_id == msg.id
    assert ref.channel_id == 111
    assert ref.fail_if_not_exists is False
    assert msg.to_reference_calls == [{"fail_if_not_exists": False}]


async def test_message_edit_records_and_updates():
    channel = FakeTextChannel()
    msg = await channel.send("before")
    await msg.edit(content="after")
    assert msg.content == "after"
    assert [c.kwargs["content"] for c in msg.edits] == ["after"]
    assert msg.edited_at is not None


async def test_typing_records_and_failure_injection():
    channel = FakeTextChannel()
    async with channel.typing():
        pass
    assert channel.typing_calls == 1
    channel.typing_enter_error = RuntimeError("typing down")
    with pytest.raises(RuntimeError, match="typing down"):
        async with channel.typing():
            pass


async def test_typing_one_shot_trigger():
    channel = FakeTextChannel()
    await channel.typing()
    assert channel.typing_calls == 1
    assert channel.calls[-1].kind == "typing.trigger"
    channel.typing_enter_error = RuntimeError("typing down")
    with pytest.raises(RuntimeError, match="typing down"):
        await channel.typing()


async def test_send_failure_injection():
    channel = FakeTextChannel()
    channel.send_error = RuntimeError("missing permissions")
    with pytest.raises(RuntimeError, match="missing permissions"):
        await channel.send("nope")
    assert channel.sent == []


async def test_interaction_defer_records_timing():
    interaction = FakeInteraction()
    await asyncio.sleep(0.01)
    await interaction.response.defer()
    assert interaction.response.is_done()
    assert_deferred_within(interaction, 5000)
    with pytest.raises(AssertionError, match="over the"):
        assert_deferred_within(interaction, 0.001)


async def test_interaction_never_deferred_fails_assertion():
    interaction = FakeInteraction()
    with pytest.raises(AssertionError, match="never deferred"):
        assert_deferred_within(interaction, 100)


async def test_interaction_response_can_only_be_used_once():
    interaction = FakeInteraction()
    await interaction.response.defer()
    with pytest.raises(discord.InteractionResponded):
        await interaction.response.send_message("too late")


async def test_interaction_response_send_message_records():
    channel = FakeTextChannel()
    interaction = FakeInteraction(channel=channel)
    sent = await interaction.response.send_message("hello")
    assert sent.content == "hello"
    assert channel.sent == [sent]
    assert interaction.response.message is sent
    assert await interaction.original_response() is sent


async def test_followup_requires_response_then_sends():
    channel = FakeTextChannel()
    interaction = FakeInteraction(channel=channel)
    with pytest.raises(RuntimeError, match="followup.send before"):
        await interaction.followup.send("early")
    await interaction.response.defer()
    sent = await interaction.followup.send("later")
    assert sent.content == "later"
    assert interaction.followup.sent == [sent]
    assert channel.sent == [sent]


def _confirm_view(*, on_press=None, check=None):
    view = View(timeout=None)
    button = Button(
        label="Confirm", custom_id="confirm:yes", style=discord.ButtonStyle.primary
    )

    async def callback(interaction):
        if on_press is not None:
            await on_press(interaction)

    button.callback = callback
    view.add_item(button)
    if check is not None:
        view.interaction_check = check
    return view


async def test_press_drives_real_view_and_button():
    channel = FakeTextChannel()
    seen = []

    async def on_press(interaction):
        seen.append(interaction.user.id)
        await interaction.response.defer()
        await interaction.followup.send("done")

    msg = await channel.send("press?", view=_confirm_view(on_press=on_press))
    user = FakeUser(id=42)
    interaction = await press(msg, "confirm:yes", user)
    assert seen == [42]
    assert interaction.user is user
    assert interaction.message is msg
    assert interaction.custom_id == "confirm:yes"
    interaction.assert_deferred_within(1000)
    assert [m.content for m in interaction.followup.sent] == ["done"]


async def test_press_response_edit_removes_buttons():
    channel = FakeTextChannel()

    async def on_press(interaction):
        await interaction.response.edit_message(content="confirmed", view=None)

    msg = await channel.send("press?", view=_confirm_view(on_press=on_press))
    await press(msg, "confirm:yes", FakeUser())
    assert msg.content == "confirmed"
    assert_buttons_removed(msg)


async def test_press_unknown_custom_id_lists_available():
    channel = FakeTextChannel()
    msg = await channel.send("press?", view=_confirm_view())
    with pytest.raises(AssertionError, match="confirm:yes"):
        await press(msg, "confirm:no", FakeUser())


async def test_press_without_view_fails():
    channel = FakeTextChannel()
    msg = await channel.send("no buttons")
    with pytest.raises(AssertionError, match="no view"):
        await press(msg, "confirm:yes", FakeUser())


async def test_press_respects_interaction_check():
    channel = FakeTextChannel()
    seen = []

    async def on_press(interaction):
        seen.append(True)

    async def deny(interaction):
        return False

    msg = await channel.send(
        "press?", view=_confirm_view(on_press=on_press, check=deny)
    )
    await press(msg, "confirm:yes", FakeUser())
    assert seen == []


async def test_press_disabled_button_fails():
    channel = FakeTextChannel()
    view = View(timeout=None)
    view.add_item(Button(label="Off", custom_id="off", disabled=True))
    msg = await channel.send("press?", view=view)
    with pytest.raises(AssertionError, match="disabled"):
        await press(msg, "off", FakeUser())


async def test_press_select_passes_values():
    channel = FakeTextChannel()
    seen = []
    view = View(timeout=None)
    select = Select(
        custom_id="pick:color",
        options=[
            discord.SelectOption(label="Red", value="red"),
            discord.SelectOption(label="Blue", value="blue"),
        ],
    )

    async def callback(interaction):
        seen.append(list(interaction.data["values"]))

    select.callback = callback
    view.add_item(select)
    msg = await channel.send("pick", view=view)
    await press(msg, "pick:color", FakeUser(), values=["blue"])
    assert seen == [["blue"]]


def test_find_component_and_real_view_components():
    view = _confirm_view()
    item = find_component(view, "confirm:yes")
    assert isinstance(item, Button)
    # The components are real discord.py objects: they serialize as such.
    components = view.to_components()
    assert components[0]["components"][0]["custom_id"] == "confirm:yes"


async def test_buttons_removed_assertion_fails_with_live_buttons():
    channel = FakeTextChannel()
    msg = await channel.send("press?", view=_confirm_view())
    with pytest.raises(AssertionError, match="still has buttons"):
        assert_buttons_removed(msg)
    await msg.edit(view=None)
    assert_buttons_removed(msg)


async def test_edit_original_response():
    channel = FakeTextChannel()
    msg = await channel.send("original", view=_confirm_view())
    interaction = FakeInteraction(message=msg, channel=channel)
    await interaction.response.defer()
    edited = await interaction.edit_original_response(content="updated", view=None)
    assert edited is msg
    assert msg.content == "updated"
    assert_buttons_removed(msg)


def test_guild_and_message_guild():
    guild = FakeGuild(id=5, name="Home")
    channel = FakeTextChannel(guild=guild)
    msg = channel.receive_message("hi")
    assert msg.guild is guild
