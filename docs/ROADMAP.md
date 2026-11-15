# Discord Assist roadmap

Working notes for 0.3.1 and beyond. Written 2026-11-15 after reviewing real
conversations, the live Home Assistant setup, and the Print Handy Discord bot.

## Why it feels dumb

Most of the bad answers come from the conversation agent's configuration, not
from Discord Assist. The integration passes the text through and posts the
reply; the agent decides what to do.

The live agent is Extended OpenAI Conversation ("Luna", model `luna-latest`)
running its stock template:

| Symptom seen in Discord | Cause |
| --- | --- |
| "Do you want me to turn off all lights, including the outdoor lights?" | The stock prompt says "Do not execute service without user's confirmation." |
| "Next time don't ask" is forgotten | 414 entities are exposed and pasted into every prompt as CSV. `context_threshold` is 13000 with strategy `clear`, so history is wiped almost every turn. Discord Assist also drops context after 15 idle minutes. |
| "Close the blinds" closes the den one day and everything another | Two covers match (`cover.tuya_blinds` with no area, `cover.den_blinds`). Discord has no room, so the agent guesses. |
| "I don't have access to the announcement system" | The only tool is `execute_services`. `script.broadcast` is exposed but nothing tells the model it exists or what fields it takes. `max_function_calls_per_conversation` is 1, so it cannot look up and then act. |
| Kitchen timer / Google display not found | No timer entities and no Google display are exposed. |
| One-sentence answers | Prompt says "in one sentence"; `max_tokens` is 150. |
| Agent does not know who is talking | `attach_username` is off, and Discord Assist sends no speaker context. |

### Fix now, no code (agent settings)

1. Remove "Do not execute service without user's confirmation" from the prompt.
   Replace with: confirm only for locks, garage doors, alarm, and anything
   irreversible.
2. Raise `max_function_calls_per_conversation` to 4 and `max_tokens` to 500.
3. Cut exposed entities from 414 to what people actually ask about. Sensors and
   binary sensors (107) and per-bulb lights are the bulk. Expose groups and
   rooms, not every bulb. This also fixes the context wipe.
4. Give every exposed entity an area, and give `cover.tuya_blinds` a name and
   area that says which blinds it is.
5. Add tool specs for `script.broadcast` (with its message field) and for
   timers, or expose timer helpers.
6. Turn on `attach_username`.

## Suggestions

Grouped by theme. Effort: S (hours), M (a day or two), L (a week).

### Smarter answers

1. **Speaker and place context (S).** Pass `extra_system_prompt` to
   `async_converse` with the speaker's display name, mapped HA user, channel
   name, and local time. Works with any agent that honours it.
2. **Room channels (S).** Map a Discord channel to a Home Assistant area or a
   device, passed as `device_id`, so "close the blinds" in `#den` means the den,
   the same way a voice satellite works.
3. **Local commands first (M).** Try Home Assistant's built-in agent
   (`conversation.home_assistant`) first and only fall back to the LLM when no
   intent matches. "Turn off all the lights" becomes instant, free, and
   deterministic. Mirrors Print Handy's deterministic-first routing.
4. **Longer memory (S).** Done in part, with a limit: see the decision below.
   The `reset` text command and a per-channel idle override shipped; the idle
   default was not raised, because Home Assistant core expires chat sessions
   after 5 idle minutes (below). The `/reset` slash command is part of the
   slash commands work (#19).
5. **Standing preferences (M).** "Remember: all the lights means all lights."
   Store per-user notes in the integration's store and inject them via
   `extra_system_prompt`. Editable in options and via a `/remember` command.
6. **Clarify with buttons (M).** When the agent asks a question, post the
   likely answers as buttons (Yes / No, or the matching entities) instead of
   making people type "Yes".
7. **Agent per channel or user (S).** Kids' channel gets local-only Assist;
   family channel gets the LLM.
8. **Reply as a reply (S).** Use `message.reply()` with
   `fail_if_not_exists=False` so answers thread visually.

### Decision: longer memory means preferences, not transcripts (#15)

Home Assistant core expires a chat session after 5 idle minutes
(`CONVERSATION_TIMEOUT` in `helpers/chat_session.py`), and the chat log is
deleted with the session. For every agent built on `ChatLog` (all core LLM
agents, and Extended OpenAI Conversation), the history is therefore gone
after 5 idle minutes no matter what conversation id Discord Assist caches.
Raising Discord Assist's idle default above 5 minutes does nothing for those
agents, so the default stays at 15 minutes and the limit is stated plainly in
the README instead.

Decision: Discord Assist will **not** keep its own transcript and re-supply
it to the agent (for example via `extra_system_prompt`). Re-supplying would
duplicate history the agent already manages, spend tokens on every turn, and
risk stale or conflicting context. Longer memory instead comes from standing
preferences (suggestion 5, #16): short, deliberate facts the user asked to
keep, injected on every turn.

What the idle settings do control is when Discord Assist drops the cached
conversation id: the global idle minutes, plus a per-channel override in
options for channels that should reset sooner or later. That matters for
agents that keep their own history keyed by conversation id, and for keeping
busy channels from resuming stale threads. Sending a message that is exactly
`reset` (any case) drops the cached id for that channel or DM on demand and
replies "Started a fresh conversation."; the `/reset` slash command is
tracked with the slash commands work (#19).

### Discord features

9. **Buttons that survive restarts (M).** Port Print Handy's design: buttons
   carry `prefix:action:id` custom ids, one `on_interaction` dispatcher, and a
   persisted record with a TTL. `take()` makes each press single-use.
10. **Acknowledge first (S).** `defer()` within Discord's 3 second window, then
    run the Assist call and `edit_original_response`.
11. **Slash commands (M).** `/assist <text>`, `/state <entity>` (autocomplete
    from exposed entities), `/reset`, `/remember`, `/whoami`. Ephemeral
    replies where it makes sense.
12. **Camera snapshots (M).** "Show me the driveway" attaches a
    `camera.snapshot`. A media marker in the reply is made inert so the model
    can never attach arbitrary files.
13. **Edit in place for slow work (S).** Post "Working on it" and edit it with
    the answer.
14. **Typing indicator with timeout (S).** Fire-and-forget, capped at 10 s.
15. **Code-block-safe chunking (S).** Close and reopen fences across 2000
    character splits.

### Outbound and automations

16. **`discord_assist.send_message` action (M).** Post text, an image, and
    buttons to a channel or user from automations.
17. **Actionable notifications (M).** Buttons on outbound messages fire a
    `discord_assist_action` event with the button id and who pressed it, like
    mobile app actionable notifications. Example: doorbell snapshot with
    "Unlock front door" / "Ignore".
18. **Notify entities (S).** One `notify` entity per configured channel so
    Discord shows up in Home Assistant's notify UI.
19. **Inbound event and trigger (S).** Fire `discord_assist_message` for every
    accepted message so automations can react to keywords without the LLM.
20. **Retire stale buttons (S).** When an alert is no longer valid, strip its
    buttons and strike the headline.

### Reliability

21. **One turn at a time per conversation (S).** FIFO `asyncio.Lock` per
    conversation key in a bounded LRU (Print Handy `SessionTurns`).
22. **Agent timeout (S).** Abort after N seconds with a clear message instead
    of typing forever.
23. **Per-user rate limit (S).**
24. **Duplicate login warning (S).** Two READY events on one session means the
    token is running somewhere else (the old integration, a test bot).
25. **Re-read state before acting on a press (S).** Refuse with "Nothing
    changed" if the entity changed since the button was posted.

### Packaging and quality

26. **Brand icon (S).** Done in 0.3.1. Since Home Assistant 2026.3 the icons
    ship in `custom_components/discord_assist/brand/`; the brands repository no
    longer accepts custom integrations.
27. **Marketplace catalog listing (M).** Since 2026.11 HACS is built into Home
    Assistant as the Marketplace, and the catalog is still `hacs/default`.
    Tracked in a GitHub issue.
28. **Diagnostics platform (S).** Redacted options, connection state, recent
    errors.
29. **Fake Discord test kit (M).** Port `fake_discord.py` so tests drive real
    `discord.ui.View` objects and assert on edits and acks.
30. **Docs (S).** Agent tuning guide (the table above), permissions guide,
    automation examples.

## Per-user permissions: scope

### Goal

Control what each Discord user can do: which rooms and devices, which kinds of
actions, where they can ask, and what needs approval.

### What Home Assistant gives us today

- Every request already runs as a mapped Home Assistant user. Home Assistant
  enforces that user's permissions on entity service calls.
- Built-in groups are coarse: admin, user (full control), read-only. Mapping a
  Discord user to a **read-only** Home Assistant user is real enforcement today.
- Fine-grained entity policies exist in Home Assistant's auth system but have
  no UI and are not practical to manage.
- Entity exposure to Assist is global, not per user.
- Anything written into a prompt ("do not touch the locks") is a suggestion to
  the model, not security.

### Proposed model

**Access profiles** defined in the options flow:

```yaml
profiles:
  owner:
    ha_user: andrew            # identity for HA permission checks
    scope: all
  family:
    ha_user: family
    areas: [kitchen, den, living_room, kids_rooms]
    deny_domains: [lock, alarm_control_panel]
    confirm_domains: [cover, climate]
    dms: true
  kids:
    ha_user: kids_readonly
    areas: [emma_bedroom, playroom]
    actions: [light, media_player, fan]
    agent: conversation.home_assistant   # local only, no LLM
    channels: ["#kids"]
    quiet_hours: "21:00-07:00"
  guest:
    ha_user: guest_readonly
    read_only: true
```

**Who gets which profile:** by Discord user id, or by Discord role id (so
giving someone the "Family" role in the server grants the family profile).
User mapping beats role mapping; deny beats allow; no match means ignored
(fail closed, as today).

### Enforcement tiers

1. **Identity (works with any agent, hard).** Profile picks the Home Assistant
   user. Read-only profiles are enforced by Home Assistant itself.
2. **Gatekeeping before the agent (hard).** Channel, DM, quiet hours, rate
   limits, and agent choice are checked by Discord Assist before anything
   reaches an agent.
3. **Scoped tools (hard, with the bundled agent).** Discord Assist's own
   agent builds each request's tool list from the speaker's profile and
   checks every call's arguments: only allowed tools, areas, domains and
   entities are visible or callable. The model cannot call what it is not
   given. Third-party agents such as Extended OpenAI Conversation build their
   own tools, so with them only tiers 1, 2 and 5 apply. See the decision
   below and #40.
4. **Approval buttons for sensitive actions (hard with tier 3).** A call to a
   `confirm` tool or target does not execute; it posts Approve / Deny
   buttons, and only the requester (or an approver) can press them. Approvers
   can be a separate list, as in Print Handy.
5. **Audit (always).** Every Discord-initiated action is logged to the
   logbook with the Discord user, profile, and channel, and fires an event.

### Phasing

| Phase | Ships | Depends on | Status |
| --- | --- | --- | --- |
| A | Profiles in options, user and role mapping, per-profile HA user, channel and DM rules, quiet hours, rate limit, per-profile agent, audit event | nothing | #12, PR #59 open, needs rework (see below) |
| B | Scoped tools per profile (areas, domains, entity allow and deny, read-only tools) | the bundled agent (#40) | Scoped LLM API merged (#34 via #68); bundled agent gate #69 and ha-mcp bridge #70 open, need rework |
| C | Approval buttons for sensitive tools, approver list | B and the button dispatcher | #35, PR #67 open, needs rework; dispatcher merged (#43) |

### Tracking (epic #39)

Status as of 2026-11-15, after the merge pass over the open pull requests.
The lanes were built as independent pull requests from `main`, each with its
own copy of the substrate it needed. Everything outside the permission gate
is merged; the gate itself is not, because review found fail-open paths that
need one design across the phases rather than a fix at merge time. The
findings are on each pull request.

| Work item | Issue | PR | Status |
| --- | --- | --- | --- |
| Phase A: profiles, user and role mapping, gatekeeping, audit | #12 | #59 | Open: DM deny bypass, profile edit wipes restrictions, channel list acts as a listening list |
| Phase B: scoped LLM API | #34 | #68 | Merged; scopes have no options UI until profiles land |
| Phase C: approval buttons, approver list | #35 | #67 | Open: `request_approval` lets a non-admin run any service as the admin; own dispatcher, not yet on #43 |
| Bundled agent with ha-mcp tools (phases B and C) | #40 | #69 | Open: tool gate bypassable (`ha_call_tool`, `ha_call_service` domain/service split, area targets, empty allowlist means all) |
| In-process ha-mcp LLM API for the bundled agent | #41 | #70 | Open: argument check and non-admin tool filter bypassable |
| Decision: which agent Luna runs behind | #36 | n/a | Decided, issue closed (bundled agent, #40) |
| Decision: Server Members intent for role mapping | #37 | #65 | Open, lands with phase A (resolution failure must deny when deny roles exist) |
| Decision: shared-channel history per profile | #38 | #64 | Merged |

Merged supporting work: the persistent button dispatcher and shared send
helper (#13, #43), acknowledge first (#14, #48 and #71), per-user rate limit
(#28, #44), agent per channel or user (#18, #55), re-read state before
acting (#30, #63), retire stale buttons (#27, #61).

Next for this epic: one router path (`resolve` the request's profile, HA
user, agent, device and conversation key, then `gate` it) used by messages,
text commands, slash commands and buttons; one argument parser and scope
check shared by the scoped API, the bundled agent and the ha-mcp bridge;
approvals rebuilt as button handlers on the shared store. Each phase's
checkbox on #39 is ticked when its pull request merges.

### Decision: a bundled agent with ha-mcp tools (2026-11-15)

Tiers 3 and 4 will not depend on a third-party agent. Discord Assist ships its
own conversation agent (OpenAI-compatible model setting, so Luna's model keeps
working) and uses [ha-mcp](https://github.com/homeassistant-ai/ha-mcp) as its
main tool layer. Every tool call passes one gate in Discord Assist: the
speaker's profile decides which tools the model is given, arguments are
checked against the profile's areas, domains and entities, and sensitive calls
wait for Approve / Deny buttons. ha-mcp acts as one administrator account, so
this gate is the security boundary for ha-mcp tools; ha-mcp's own Read Only
Mode, tool toggles and policies stay on as a global floor. Optional: Home
Assistant's Device control API as the speaker's own user. Design, transport
choice (in-process ha-mcp LLM API or MCP client to the app) and open questions
are in issue #40.

### Decision: conversation keying in shared channels (2026-11-15)

Conversations in a channel are keyed per channel by default
(`channel:<id>`), so everyone talking in a channel shares one agent history,
the same way everyone in a room shares a voice satellite's conversation.
Direct messages are always keyed per user (`dm:<id>`). Profiles do not
restrict this by default.

The exposure is stated plainly: in the default channel scope, anything one
speaker tells the agent is part of the shared history that other channel
members can continue, so a guest in the family channel can ask "what did
Andrew just ask you?" and the agent can answer from the earlier turns.
Discord already shows the channel's messages and the bot's replies to every
member; what is additionally shared is what lives only in the agent history,
such as tool results and system prompt content (speaker context, standing
preferences). The exposure is bounded: this integration's conversation cache
expires after the configured idle minutes, and Home Assistant core chat
sessions expire after 5 idle minutes.

For channels where that is not acceptable, the `conversation_scope` option
can be set to `user`: channel keys become `channel:<id>:user:<id>`, so each
person gets their own history in the channel. The cost is the shared-room
feel: "turn it off again" no longer works across people, because a follow-up
only sees the speaker's own turns. Per-profile keys were considered and not
chosen: a profile is an access rule, not a social grouping, and per-user
keys already cover the isolation need.

Switching the scope does not migrate existing conversations. The keys
simply change, so conversations started under the old scope stop being
continued and idle-expire on their own.

### Open questions

- ~~Which agent should Luna run behind?~~ Decided: the bundled agent above (#36, #40).
- (Original question) Tier 3 needs an agent that supports Home
  Assistant LLM APIs; Extended OpenAI Conversation does not.
- Discord roles need the Server Members intent to read member roles
  reliably. Proposed in #37 (open PR #65): no privileged intent; roles come
  from the message's member payload first, with a cached `fetch_member`
  fallback, and a repair issue if role mapping is configured but roles
  cannot be resolved. Lands with phase A; the live-server check of
  partial-member payloads stays open on #37.
- ~~Should profiles also restrict which conversation history a user can see
  in shared channels?~~ Decided (#38, merged in #64): guild channels keep one
  shared conversation by default; a `conversation_scope` option switches
  channels to per-user history. DMs are always per user.

## 0.3.1 (built)

All of the list below is done. Each roadmap item is also tracked as a GitHub
issue.

1. Brand icon and README refresh.
2. Speaker and place context via `extra_system_prompt` (1).
3. Room channels via `device_id` (2).
4. Local commands first, as an option (3).
5. One turn at a time per conversation (21) and agent timeout (22).
6. Reply as a reply (8), code-block-safe chunking (15).
7. Agent tuning guide in the docs (30).

0.4.0: permissions phase A (#12), the button dispatcher (#13, #14), and the
bundled agent with ha-mcp tools (#40), which delivers phases B and C. Live
status for all of it is in the tracking table under "Per-user permissions:
scope" above (epic #39).
