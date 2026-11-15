<p align="center"><img src="custom_components/discord_assist/brand/icon@2x.png" alt="Discord Assist icon" width="128"></p>

# Discord Assist

Talk to Home Assistant's Assist from Discord.

[![Open your Home Assistant instance and open this repository in the Marketplace.](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=catduckgnaf&repository=ha-discord-assist&category=integration)

A Home Assistant custom integration that exposes the Assist conversation agent over a Discord bot. Users can send messages in configured channels or DMs, and the bot replies using whichever conversation agent you select (e.g. a local LLM, OpenAI, or the built-in Home Assistant Assist agent).

## Features

- Responds in configured guild text channels
- Responds to direct messages (optional)
- Responds to @mentions in any channel the bot can see (optional)
- Per-conversation context window with configurable idle timeout
- Channel conversations shared by everyone in the channel by default, with an optional per-user history scope
- Replies in threads under the channels it listens to
- Tells the agent who is speaking, from which channel, and the local time
- Room channels: a channel can stand for a room, so "close the blinds" in `#den` means the den
- Optional local-first mode: Home Assistant's built-in commands answer instantly, the LLM handles the rest
- One turn at a time per conversation, with a timeout instead of typing forever
- Answers as Discord replies, and long code blocks split cleanly across messages
- Maps Discord user IDs to Home Assistant users so commands run as the right person
- Fails closed: Discord users it cannot run as a Home Assistant user are ignored
- Replies can never ping `@everyone`, roles, or users
- Retries setup when Discord is unreachable and reloads if the gateway dies
- `Discord gateway` connectivity sensor for alerting
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
2. Under **Scopes**, select `bot` and `applications.commands`.
3. Under **Bot Permissions**, select **Read Messages/View Channels**, **Send Messages**, and **Read Message History**.
4. Copy the generated URL, open it in your browser, and invite the bot to your server.

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
| Idle minutes | 15 | Minutes of inactivity before conversation context resets |
| Language | (HA default) | BCP-47 language tag passed to the conversation agent |
| Allowed Discord user IDs | (none) | Discord user IDs allowed to use the bot as the fallback user; mapped users are always allowed |
| Speaker context | Yes | Sends the agent the speaker's Discord name and Home Assistant user, the channel, the room (for room channels), and the local time, as extra system prompt text. Agents that ignore extra prompt text are unaffected |
| Built-in commands first | No | Tries `conversation.home_assistant` first and only asks your agent when the built-in agent does not understand the request or finds no matching device. A command it matched but failed is not retried, so nothing runs twice |
| Agent timeout | 60 s | Gives up and says so instead of typing forever |
| Conversation scope | Channel | `Channel` shares one conversation history among everyone talking in a channel; `User` gives each person their own history in each channel. Direct messages always have a per-user history |

## Room channels

A voice satellite knows which room it is in; a Discord channel does not, unless you tell it. In options, choose **Treat a channel as a room**, pick the channel, and pick any device in that room's area. Messages in that channel and its threads are sent with that device, so the built-in agent and LLM agents using Home Assistant's Assist API treat the device's area as the default room. With speaker context on, the room name is also in the prompt for agents that read it.

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

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Credits

Discord Assist is a hard fork of [Discord Conversation](https://github.com/aarontc/home-assistant-discord-conversation) by Aaron ([@aarontc](https://github.com/aarontc)), used under the MIT license.
