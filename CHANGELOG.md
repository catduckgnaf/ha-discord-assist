# Changelog

## 0.3.1

### Added

- Speaker context: the agent gets the speaker's Discord name, their Home Assistant user, the channel, the room, and the local time through `extra_system_prompt`. On by default; Discord names are cleaned to one short line.
- Room channels: map a channel to a device in a room. Messages there, and in its threads, are sent with that `device_id`, so requests without a room mean that room.
- Built-in commands first (off by default): tries `conversation.home_assistant`, falling back to the configured agent only when no intent matched or no device was found.
- Agent timeout (60 s default) with a clear reply instead of an endless typing indicator.

### Changed

- One turn at a time per conversation: messages in the same channel or DM queue in order instead of racing on one conversation.
- Answers are posted as Discord replies (without pinging the author). If the original message was deleted, the answer is posted normally.
- Long answers split across messages keep code blocks intact: a block is closed at the end of one message and reopened, with its language, in the next.

### Fixed

- A typing indicator failure after the agent had answered could run the agent a second time, repeating any action it took.
- README listed the idle reset default as 30 minutes; it is 15.

### Packaging and docs

- Add a brand icon in `custom_components/discord_assist/brand/` (Home Assistant 2026.3+ reads it locally; the brands repository no longer takes custom integrations).
- README: Marketplace install instructions and badge (HACS is built into Home Assistant 2026.11 as the Marketplace), plus a "Getting good answers" guide for tuning LLM agents.
- Add `docs/ROADMAP.md` with the improvement list and the per-user permissions scope.
- CI: run the HACS validator with no ignored checks.
- Add the Home Assistant integration categories `ai_voice.voice_assistants` and `messaging_notifications.chat_apps` to the manifest, so the integration is categorised correctly in Home Assistant and the Marketplace catalog. No functional change.

## 0.3.0

Hard fork of Discord Conversation 0.2.1, renamed to Discord Assist
(domain `discord_assist`).

### Security

- **Fails closed.** Only mapped Discord users, or allowlisted users when a
  fallback Home Assistant user is set, can use the bot. Previously an empty
  allowlist with no fallback user let anyone run commands with an
  unrestricted context.
- **No mass pings.** Replies are sent with all mentions disabled, so agent
  output cannot ping `@everyone`, roles, or users.

### Added

- Threads under listened channels are answered without an @mention.
- `Discord gateway` connectivity binary sensor.
- Repair issues when nobody is authorized and when Discord refuses the
  Message Content intent.
- Import from an existing Discord Conversation entry (the old entry is
  disabled, not deleted).

### Changed

- Setup logs in before returning: a bad token starts reauth, and Discord
  being unreachable retries setup with backoff instead of failing silently.
- If the gateway task dies, the entry reloads after 60 seconds.
- Tooling moved from GitLab CI and Dagger to GitHub Actions.

## Discord Conversation history

### 0.2.x (upstream)

### Added

- **Automated release pipeline.** Pushes to `main` are linted, tested, and
  auto-versioned by Dagger: the `tag:auto` GitLab CI job computes the next
  semantic version from `(Major)/(Minor)/(Patch)` commit subjects, commits the
  version bump, and tags via the GitLab API. The GitHub mirror's
  `release.yaml` workflow then publishes a GitHub Release per tag (which is
  what HACS reads to detect new versions).

### Fixed

- **Nightly HACS validation.** The `Validate` workflow on the GitHub mirror
  now skips the `brands` check (the integration is not listed in
  home-assistant/brands), and the GitHub repository carries topics, so the
  scheduled `hacs/action` run passes again.

### 0.1.0 (upstream)

Initial release. Bridges the Home Assistant Assist conversation agent to a
Discord bot: selected channels, `@mentions`, and direct messages route to a
configured conversation agent and the reply is posted back. Includes
per-channel/per-DM conversation context with idle reset, a typing indicator,
Discord-to-Home-Assistant user mapping with a least-privilege fallback user, a
config and options flow, and reauthentication.
