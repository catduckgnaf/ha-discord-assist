# Changelog

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
