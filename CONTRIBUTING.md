# Contributing

Tests run under `pytest-homeassistant-custom-component`, which needs Linux or
macOS and Python 3.14.

```sh
uv sync
uv run ruff check . && uv run ruff format --check . && uv run pytest
```

GitHub Actions runs the same checks plus `hassfest` and the HACS validator on
every push.

## Releases

Bump `version` in both `pyproject.toml` and
`custom_components/discord_assist/manifest.json`, add a `CHANGELOG.md` entry,
then tag `vX.Y.Z` on `main` and push the tag. The `release` workflow publishes
the GitHub release that HACS reads.
