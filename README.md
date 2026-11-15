<p align="center"><img src="custom_components/discord_assist/brand/icon@2x.png" alt="Discord Assist icon" width="128"></p>

# Discord Assist

Talk to Home Assistant's Assist from Discord.

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=catduckgnaf&repository=ha-discord-assist&category=integration)

A Home Assistant custom integration that exposes the Assist conversation agent over a Discord bot. Users can send messages in configured channels or DMs, and the bot replies using whichever conversation agent you select (e.g. a local LLM, OpenAI, or the built-in Home Assistant Assist agent).

## Features

- Responds in configured guild text channels
- Responds to direct messages (optional)
- Responds to @mentions in any channel the bot can see (optional)
- Slash commands: `/assist`, `/state` (exposed-entity autocomplete), `/reset`, `/remember`, `/notes`, `/forget`, `/whoami`
- Per-conversation context window with configurable idle timeout
- Per-conversation context window with configurable idle timeout, plus per-channel idle overrides
- Send exactly `reset` to drop the current conversation and start fresh in that channel or DM
- Channel conversations shared by everyone in the channel by default, with an optional per-user history scope
- Replies in threads under the channels it listens to
- Tells the agent who is speaking, from which channel, and the local time
- Room channels: a channel can stand for a room, so "close the blinds" in `#den` means the den
- Optional local-first mode: Home Assistant's built-in commands answer instantly, the LLM handles the rest
- One turn at a time per conversation, with a timeout instead of typing forever
- Answers as Discord replies, and long code blocks split cleanly across messages
- Camera snapshots: `snapshot <camera>` (or `show me <camera>`) attaches a current snapshot of an exposed camera you are allowed to view
- Standing preferences: each user can save short notes ("all the lights means all lights") that are passed to the agent on every turn
- Maps Discord user IDs to Home Assistant users so commands run as the right person
- Registers a scoped LLM API ("Discord Assist") that limits an LLM agent's tools and entities per Home Assistant user
- Fails closed: Discord users it cannot run as a Home Assistant user are ignored
- Replies can never ping `@everyone`, roles, or users
- Retries setup when Discord is unreachable and reloads if the gateway dies
- Actionable notification buttons that fire a `discord_assist_action` event when pressed
- `Discord gateway` connectivity sensor for alerting
- One `notify` entity per configured channel, so `notify.send_message` can post to Discord from automations
- `discord_assist_message` event for every accepted message, so automations can react without the agent (with an option to exclude the message text for privacy)
- Repair issues for "nobody is authorized" and a missing Message Content intent
- One-click import from the Discord Conversation integration it was forked from

## Security model

**The conversation agent can control your home.** Discord Assist only answers a Discord user when it knows which Home Assistant user to run the request as:

- A Discord user with a **user mapping** acts as the mapped Home Assistant user.
- A Discord user on the **allowlist** without a mapping acts as the **fallback Home Assistant user**. With no fallback user set, allowlisted users are ignored.
- Everyone else is ignored, even in channels the bot listens to.

Requests never run without a Home Assistant user, so permissions always apply. Use a dedicated, least-privilege account as the fallback user, and limit `channels` to places you trust. If nobody is authorized, a repair issue appears under **Settings → System → Repairs**.

Conversation history in a channel is shared by default: everyone who talks to the bot in a channel continues one conversation, so anything one person tells the agent is part of the history other channel members can continue, including tool results and context that never appear as Discord messages. If that is too open for a channel, set **Conversation scope** to **User** in the options so each person gets their own history in each channel; direct messages always have a per-user history. Either way, history expires after the configured idle minutes.

## Installation

### Prerequisites

- Home Assistant 2026.6.0 or later
- A Discord bot token (see [Discord setup](#discord-setup))

### Home Assistant 2026.11 and later (Marketplace)

1. Click the **Open in Marketplace** badge above, or open the Marketplace in Home Assistant, open its menu, choose **Custom repositories**, and add `https://github.com/catduckgnaf/ha-discord-assist` as an **Integration**.
2. Install **Discord Assist**.
3. Restart Home Assistant.

### Older versions (HACS)

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/catduckgnaf/ha-discord-assist` with category **Integration**.
3. Install **Discord Assist** and restart Home Assistant.

## Discord setup

### Create a Discord application and bot

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
2. Name the application (e.g. "Home Assistant") and click **Create**.
3. In the left sidebar, click **Bot**.
4. Click **Add Bot** → **Yes, do it!**
5. Under **Token**, click **Reset Token** and copy the token; you will need it during integration setup.
6. Under **Privileged Gateway Intents**, enable **Message Content Intent**. Without this, the bot cannot read message text in guild channels.

### Invite the bot to your server

1. In the Developer Portal, go to **OAuth2 → URL Generator**.
2. Under **Scopes**, select `bot` and `applications.commands`. The `applications.commands` scope is required for the slash commands below; if you invited the bot before slash commands existed, re-invite it with the same URL so the scope is granted.
3. Under **Bot Permissions**, select **Read Messages/View Channels**, **Send Messages**, and **Read Message History**.
4. Copy the generated URL, open it in your browser, and invite the bot to your server.

## Slash commands

The bot registers slash commands in every server it is in (synced per server the first time it connects, and when it joins a new server, so they appear right away). They are server commands only and are not offered in DMs; use plain messages there.

| Command | Description |
|---------|-------------|
| `/assist <text>` | Ask Home Assistant Assist. Shares the channel's conversation and its turn lock with plain messages. |
| `/state <entity>` | Read one entity's current state (ephemeral: only you see it). Only entities exposed to Assist that your mapped Home Assistant user may read are offered or read. |
| `/reset` | Same as sending `reset`: the next message in this channel starts a new conversation. |
| `/remember <note>` | Same as `remember: <note>`: save a standing preference (ephemeral). |
| `/notes` | List your saved notes (ephemeral). |
| `/forget <number>` | Remove the note with that number from `/notes` (ephemeral). |
| `/whoami` | Show which Home Assistant user you are mapped to (ephemeral). |

Commands follow the same rules as plain messages: the channel must be one the bot listens in, and the user must be mapped to a Home Assistant user. Anyone else gets an ephemeral refusal and nothing runs. Allowed commands acknowledge first, then work, so a slow agent answer does not make the interaction fail.

## Migrating from Discord Conversation

If the Discord Conversation integration is already set up, adding Discord Assist offers **Import from Discord Conversation**. That copies the bot token and every setting, then disables (does not delete) the old entry so the bot is not driven twice. Once you are happy, remove Discord Conversation from the Marketplace (or HACS). Unmapped, non-allowlisted users who could use the old integration are ignored here.

## Getting good answers

Discord Assist passes your message to the conversation agent you pick and posts its reply. Most "dumb" answers come from the agent's setup, not from Discord. If you use an LLM agent, check these first:

- **Confirmation prompts.** Some prompt templates (including Extended OpenAI Conversation's default) tell the model to confirm every action. Remove that line, or limit it to locks, garage doors and alarms.
- **Too many exposed entities.** Every exposed entity is sent with every message. A few hundred entities can push the conversation history out of the context window, so the agent forgets what you said a minute ago. Expose rooms and groups, not every bulb and sensor.
- **Areas.** "Close the blinds" only works reliably when each blind has an area and a clear name.
- **Tools.** If the agent can only call one function per message, it cannot look something up and then act. Allow several, and give it tools for announcements and timers if you want those.
- **Speed and cost.** For plain commands like "turn off the kitchen lights", the built-in Home Assistant agent is instant and free.

See [docs/ROADMAP.md](docs/ROADMAP.md) for what is planned to make this easier.

## Configuration

1. In Home Assistant, go to **Settings → Devices & Services → Add Integration** and search for **Discord Assist**.
2. Enter the bot token.
3. Select a conversation agent (e.g. `conversation.home_assistant`).
4. Select the channels the bot should read. Only text channels the bot can see are listed.
5. Adjust the remaining options:

| Option | Default | Description |
|--------|---------|-------------|
| Channels to listen in | (none) | Guild text channels where the bot responds to all messages |
| Respond to direct messages | Yes | Whether to respond when users DM the bot |
| Respond to @mentions anywhere | Yes | Whether to respond when the bot is @mentioned in any channel |
| Idle minutes | 15 | Minutes of inactivity before Discord Assist drops the conversation and starts a new one. A per-channel override can be set in options under **Set a per-channel idle reset**. See [Conversation memory and `reset`](#conversation-memory-and-reset) for the 5 minute Home Assistant limit this cannot override |
| Language | (HA default) | BCP-47 language tag passed to the conversation agent |
| Allowed Discord user IDs | (none) | Discord user IDs allowed to use the bot as the fallback user; mapped users are always allowed |
| Speaker context | Yes | Sends the agent the speaker's Discord name and Home Assistant user, the channel, the room (for room channels), and the local time, as extra system prompt text. Agents that ignore extra prompt text are unaffected |
| Exclude message text from events | No | Omits the `text` field from the `discord_assist_message` event (see below). Event data is stored by the recorder, so turn this on if you do not want message text in your history |
| Built-in commands first | No | Tries `conversation.home_assistant` first and only asks your agent when the built-in agent does not understand the request or finds no matching device. A command it matched but failed is not retried, so nothing runs twice |
| Timer mention | No | Mentions the Discord user who set a timer when that timer finishes. Off by default: finished timers are announced in the channel without mentioning anyone |
| Agent timeout | 60 s | Gives up and says so instead of typing forever |
| Conversation scope | Channel | `Channel` shares one conversation history among everyone talking in a channel; `User` gives each person their own history in each channel. Direct messages always have a per-user history |

## Automations from Discord messages

For every message the bot accepts (after the access and channel rules above pass; rejected messages, including unmapped users, never produce one), Discord Assist fires a `discord_assist_message` event on the Home Assistant event bus. Automations can react to keywords without involving the conversation agent at all.

The event data is:

| Field | Description |
|-------|-------------|
| `text` | The message text, with the bot's own mention stripped. Omitted entirely when **Exclude message text from events** is on |
| `channel_id` | Discord channel (or DM) ID, as a string |
| `channel_name` | Channel name, or `null` for direct messages |
| `is_dm` | Whether the message was a direct message |
| `discord_user_id` | Discord user ID of the sender, as a string |
| `ha_user_id` | Home Assistant user the sender acts as; the event's context carries the same user |
| `message_id` | Discord message ID, as a string |
| `config_entry_id` | The Discord Assist config entry that received the message |

Example: turn on a light when someone posts "movie time" in a channel, without asking the agent:

```yaml
automation:
  - alias: "Movie time from Discord"
    triggers:
      - trigger: event
        event_type: discord_assist_message
        event_data:
          channel_id: "123456789012345678"
    conditions:
      - condition: template
        value_template: "{{ 'movie time' in trigger.event.data.text | lower }}"
    actions:
      - action: light.turn_on
        target:
          entity_id: light.living_room
```

**Privacy:** event data is stored by the recorder and can appear in your history and logbook. If you do not want message text stored, turn on **Exclude message text from events** in the integration options; automations can still match on the channel, user, and other fields, just not on `text`.

## Conversation memory and `reset`

Discord Assist keeps one conversation per channel (or per user, in DMs) and reuses its conversation id until the conversation has been idle for the configured idle minutes (15 by default, with an optional per-channel override in options).

**The 5 minute limit:** Home Assistant itself expires a chat session after 5 idle minutes and deletes its chat log. For agents built on Home Assistant's chat log (all core LLM agents, and Extended OpenAI Conversation), the history is gone after 5 idle minutes no matter what the idle setting here says. A longer idle value does not give those agents longer memory; it only changes when Discord Assist stops reusing the conversation id, which still matters for agents that keep their own history keyed by that id.

Discord Assist does not keep its own copy of your conversations to replay to the agent later. Longer memory is planned as standing preferences (facts you ask it to remember) instead of transcripts; see [docs/ROADMAP.md](docs/ROADMAP.md) for the decision.

To start fresh on purpose, send a message that is exactly:

```text
reset
```

The bot drops the cached conversation for that channel or DM, replies "Started a fresh conversation.", and your next message begins a new conversation. A message that only contains the word, like "please reset the lights", is not a command and goes to the agent as usual.

## Standing preferences (notes)

Each authorized Discord user can save short standing preferences that the bot passes to the conversation agent on every turn, so corrections do not have to be repeated after a conversation resets. Send these as plain messages wherever the bot answers you; they are handled by the bot itself and never reach the agent as requests:

- `remember: <note>` saves a note, for example `remember: all the lights means all lights`.
- `notes` lists your own saved notes back, numbered.
- `forget note <number>` removes one note by its number from that list.

Notes are capped at 10 per user and 200 characters each, and control characters are stripped the same way Discord display names are cleaned. They persist across restarts in Home Assistant storage, per integration entry. In the integration options, **View and remove standing preferences (notes)** lists every saved note and removes the one you pick.

How they reach the agent, and the limits of that:

- Notes are **user-supplied text placed in a system prompt** (`extra_system_prompt`), as a `Standing preferences from <speaker>:` bullet list after the speaker context. Only the speaking user's own notes are included; nobody else's notes are ever sent.
- They only work with agents that read `extra_system_prompt`; agents that ignore extra prompt text are unaffected.
- Notes still apply when the **Speaker context** option is off: that option controls the speaker, channel, room and time facts, not your saved preferences.
- Notes are **never permissions**. A note cannot authorize anyone, change which Home Assistant user a request runs as, or widen what that user may do. Treat note text as a preference you stated, not as an instruction the home must obey over its normal safeguards.

## Camera snapshots

In a channel the bot listens to (or a DM, if enabled), type `snapshot driveway` or `show me the driveway`, using the camera's entity ID or friendly name. The bot attaches a current snapshot as `snapshot.jpg`; very large images are requested downscaled by width.

This is a deterministic command, not an agent feature, and it is gated twice: the camera must be exposed to Assist (the conversation assistant), and the Home Assistant user you are mapped to must have read permission on that camera. Non-exposed and unreadable cameras are refused, and their existence is not revealed. The conversation agent can never cause an attachment: replies from the agent are always sent as plain text, whatever file paths, URLs, or markers the reply contains. Only a snapshot the bot itself fetched for an allowed camera in that turn is ever attached.

## Timers

"Set a 5 minute timer" works in any channel the bot answers in. Home Assistant's timer intents need a device with a registered timer handler, so each Discord Assist entry has its own device in the device registry, and channels that are not mapped to a room send their requests as that device. When a timer finishes, the bot posts in the channel where it was set (for example "Kitchen timer done"). With the **Timer mention** option on, that post also mentions the Discord user who set the timer; it is the only mention the bot makes, and it is off by default.

Room channels are the exception: they keep sending their room device, so room preference still applies there. A timer set in a room channel is handled by that device's own timer handler if it has one (a voice satellite, for example); the integration device is not assigned to a room area, because an unmapped channel has no knowable room.

## Room channels

A voice satellite knows which room it is in; a Discord channel does not, unless you tell it. In options, choose **Treat a channel as a room**, pick the channel, and pick any device in that room's area. Messages in that channel and its threads are sent with that device, so the built-in agent and LLM agents using Home Assistant's Assist API treat the device's area as the default room. With speaker context on, the room name is also in the prompt for agents that read it.

## Actionable notifications

Buttons on outbound messages can fire a `discord_assist_action` event when pressed, like the mobile app's actionable notifications. For example, a doorbell automation can post a snapshot with "Unlock front door" and "Ignore" buttons; pressing one fires:

```yaml
event_type: discord_assist_action
event_data:
  action: unlock_front_door   # the id the automation gave the button
  label: Unlock front door
  message_id: "1234567890"    # Discord ids are strings
  channel_id: "111222333"
  discord_user_id: "444555666"
  discord_user_name: Andrew
  ha_user_id: <the presser's mapped Home Assistant user id>
  config_entry_id: <the Discord Assist entry>
```

The event is fired with the presser's Home Assistant user as its context, so automations can check who pressed. Buttons are single-use by default: pressing one fires the event, removes the buttons from the message and adds a "Andrew pressed Unlock front door" line; a second press, or a press of a sibling button on the same message, fires nothing. A button can instead be multi-use (its buttons stay), and can name the Discord users allowed to press it. A presser must always be a Discord user the bot can run as a Home Assistant user; anyone else is refused, fires no event and does not use up the button. Button records are stored persistently, so buttons posted before a restart still work after it. The logbook shows presses as "Andrew pressed Unlock front door".

> **Warning:** the automation does the action, so Home Assistant permissions apply to the automation, not the presser. An "Unlock front door" button is exactly as safe as the automation's own conditions. Check the presser's `ha_user_id` in a condition when it matters, and do not treat a button press by itself as approval for a sensitive action.

Buttons are posted through the `discord_assist.send_message` action; each button is a record in the integration's persistent button store, posted with a `da:act:<record_id>` custom id that the bot's button dispatcher routes to this event.
## Per-user and per-channel agents

One agent does not have to serve everyone. In options, choose **Use a specific agent in a channel** or **Use a specific agent for a Discord user** and pick a conversation agent, for example local-only Assist for a kids' channel and an LLM for the family channel. Precedence is: the user's agent, then the channel's agent, then the agent chosen in the main settings. Threads use their parent channel's agent. Each agent keeps its own conversation in a channel, so switching agents never mixes conversation history. "Built-in commands first" still applies on top of the chosen agent.

## User mapping

Map a Discord user ID to a specific Home Assistant user so commands run with that user's permissions.

### Find a Discord user ID

1. In Discord, open **User Settings → Advanced** and enable **Developer Mode**.
2. Right-click any user and click **Copy User ID**.

### Add a mapping

1. Go to **Settings → Devices & Services**, find the Discord Assist entry, and click **Configure**.
2. Choose **Add a Discord-to-Home-Assistant user mapping**.
3. Enter the Discord user ID and select the Home Assistant user.

### Fallback user

Set **Fallback Home Assistant user** in options to control which HA user is used for Discord users who have no explicit mapping. Use a dedicated, least-privilege account for this.

## Scoped LLM API

Entity exposure to Assist is global: every exposed entity is visible to every user an agent runs as, and a prompt like "do not touch the locks" is a suggestion, not security. Discord Assist therefore registers its own Home Assistant LLM API, **Discord Assist**, which scopes what an LLM agent can see and do to the speaker.

For each request, the API builds the tool list and the entity list from the Home Assistant user the request runs as:

- Only entities exposed to the assistant, inside the user's scope, and readable by that user (their Home Assistant entity permissions) are listed in the prompt.
- The tools are deliberately small: read one entity's state, turn an entity on, turn an entity off, and the current date and time.
- Every tool call is re-checked against the scope and the user's permissions when it runs, not just when the list is built. A tool the model was never shown is still refused if it is called by name.
- A read-only scope gets no state-changing tools at all.

### Agent setup

In your conversation agent's settings, select **Discord Assist** as the LLM API, and select **only** Discord Assist. Home Assistant merges every selected API into one tool set, so also selecting the built-in Assist API hands the model the full, unscoped Assist tools and defeats the scoping.

Only agents that use Home Assistant LLM APIs can use this API. Agents that build their own tools (for example Extended OpenAI Conversation) cannot; with them, only the user mapping above and the agent's own settings limit what a speaker can do.

### Scopes

Scopes are stored in the integration's `llm_scopes` option, keyed by Home Assistant user ID:

```json
{
  "llm_scopes": {
    "<ha_user_id>": {
      "areas": ["kitchen", "living_room"],
      "domains": ["light", "switch"],
      "entities": ["light.kitchen"],
      "deny_entities": ["lock.front_door"],
      "read_only": false
    }
  }
}
```

- `areas`, `domains` and `entities` are allow lists of area IDs, entity domains and entity IDs. Leaving a key out means "no restriction on that dimension"; a present list, even an empty one, means "only these".
- `deny_entities` always wins over the allow lists.
- `read_only: true` removes the turn on/off tools entirely.
- A malformed scope fails closed: it matches nothing.
- A user with no scope entry is bounded only by entity exposure and their own Home Assistant entity permissions, which are always enforced underneath any scope.
- Scopes are keyed by Home Assistant user, so every Discord user who acts as the fallback user shares the fallback user's scope.

There is no options screen for scopes yet: until the access profiles from the permissions roadmap land, `llm_scopes` has to be written into the entry's options by hand, and without it nobody is scoped beyond exposure and their Home Assistant permissions (which allow every entity for a default non-admin user).

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

Discord Assist is a hard fork of [Discord Conversation](https://github.com/aarontc/home-assistant-discord-conversation) by Aaron ([@aarontc](https://github.com/aarontc)), used under the MIT license.
