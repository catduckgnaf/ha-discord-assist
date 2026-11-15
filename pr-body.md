## Summary

Permissions phase A for #12: access profiles replace the flat
user_map/allowlist decision path in the router. A frozen `Profile`
dataclass carries the HA identity, channel allowlist, DM rule, quiet
hours, rate limit and per-profile agent, plus `areas` / `domains` /
`confirm` lists that are stored now and documented in code as enforced
only in phases B and C. Assignment is by Discord user id or role id,
gatekeeping (channel, DM, quiet hours, rate limit) runs before any
agent call, and every handled request is audited.

Deny model (simplest that satisfies "deny beats allow"): a profile
with no HA user is a deny profile. `profile_for` checks every reached
profile (user mapping and role mappings) for a deny first, then a user
mapping beats role mappings. No match returns None and the request is
ignored, fail closed as before.

Migration: no config entry version bump and no `async_migrate_entry`.
Entries with only the legacy `user_map` / `allowlist` /
`fallback_ha_user` options synthesize an implicit "default" profile at
runtime, so they behave exactly as today and no entry can be stranded
by a migration.

## Changes

- `conversation_router.py`: frozen `Profile` dataclass (with
  `in_quiet_hours` supporting windows that wrap past midnight),
  `profiles_from_options` parsing, `ConversationRouter.profile_for`
  (user beats role, deny beats allow, legacy fallback),
  `ha_user_for` kept for existing callers and delegating to
  `profile_for`, gatekeeping in `should_respond` / `process` (channel
  allowlist, DM rule, quiet hours, per-profile rate limit of N requests
  per minute), per-profile agent passed to the converse call, and
  audit: fires `discord_assist_request` (Discord user, profile name,
  channel, agent, outcome) and writes a logbook entry via
  `logbook.async_log_entry` with the request `Context` for every
  handled request (answered, blocked, denied, timeout, error).
  Blocked requests stay silent at the `should_respond` layer rather
  than posting a refusal.
- `bot.py`: role ids come from `message.author.roles` when the author
  is a `discord.Member`; in DMs there are no roles. Role ids and the
  DM flag are passed to `should_respond` and `process`.
- `config_flow.py`: options menu gains add / edit / remove profile
  steps and add / remove profile assignment steps (user id or role id
  to profile), following the existing room map step patterns. Removing
  a profile also drops assignments that pointed at it.
- `const.py`: `CONF_PROFILES`, `CONF_PROFILE_USER_MAP`,
  `CONF_PROFILE_ROLE_MAP`, `EVENT_REQUEST`.
- `strings.json` / `translations/en.json`: identical strings for the
  new steps, errors and aborts.
- Tests: new `tests/test_profiles.py`; profile CRUD and mapping tests
  in `tests/test_options_flow.py` (its menu test now expects the five
  new steps); a member-roles test in `tests/test_bot.py`.

## Tests

- `uv run ruff check .` - all checks passed
- `uv run ruff format --check .` - 21 files already formatted
- `uv run pytest` - 128 passed (baseline 90, +38 new), from the
  finished local log on the final head
- Coverage of the acceptance criteria: profile CRUD in options, user
  beats role, deny beats allow (both directions), unmatched users
  ignored with the agent not called, channel / DM / quiet-hour blocks
  with the agent not called, per-profile agent used, audit event plus
  logbook entry (with the request Context) on answered and blocked
  requests, legacy entries synthesizing the default profile with the
  existing router tests unchanged and passing, and no identity-less
  Context (unauthorized and denied requests raise before any converse
  call).

## Remainder / dependencies

- Per-profile agent resolution is implemented self-contained here.
  Issue #18 is a separate lane for agent-per-channel/user selection;
  expect a small rebase or merge reconciliation where the two meet in
  `ConversationRouter.process` / `_converse`.
- Reading `Member.roles` reliably needs the Server Members privileged
  intent, which is #37's lane; this lane only consumes the roles
  discord.py already provides and does not change intents.
- `areas` / `domains` / `confirm` are stored on profiles but not
  enforced, by design: enforcement is phases B and C.
- The standalone, general rate limiter is #28's lane; this lane
  implements only the small per-profile per-minute limit that falls
  out of the profile field.

Related to #12
