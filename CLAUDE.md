# CLAUDE.md

Guidance for Claude Code working in this repository.

- Run `uv run ruff check . && uv run ruff format --check . && uv run pytest`
  before pushing. The test harness does not run on Windows; use a Linux host.
- `strings.json` and `translations/en.json` must stay identical
  (`tests/test_translations.py` enforces it).
- Access control fails closed (see `ConversationRouter.ha_user_for`). Never
  call the conversation agent with a `Context()` that has no `user_id`.
- Agent output is untrusted: the client sets `AllowedMentions.none()`.
- No em dashes in commit messages.
