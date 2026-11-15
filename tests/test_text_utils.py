import pytest

from custom_components.discord_assist.text_utils import (
    chunk_message,
    strip_self_mention,
)


def test_chunk_short_message_single_chunk():
    assert chunk_message("hello") == ["hello"]


def test_chunk_empty_returns_single_empty():
    assert chunk_message("") == [""]


def test_chunk_splits_on_line_boundaries_under_limit():
    text = "\n".join(["line"] * 10)
    chunks = chunk_message(text, limit=12)
    assert all(len(c) <= 12 for c in chunks)
    assert "".join(chunks) == text


def test_chunk_hard_splits_overlong_single_line():
    chunks = chunk_message("x" * 5000, limit=2000)
    assert [len(c) for c in chunks] == [2000, 2000, 1000]
    assert "".join(chunks) == "x" * 5000


def test_chunk_keeps_code_blocks_balanced():
    body = "".join(f"line {i}\n" for i in range(40))
    text = f"intro\n```python\n{body}```\nafter\n"
    chunks = chunk_message(text, limit=80)
    assert len(chunks) > 2
    for chunk in chunks:
        assert len(chunk) <= 80
        assert chunk.count("```") % 2 == 0
    # Every continuation of the block reopens it with the language tag.
    assert all(c.startswith("```python\n") for c in chunks[1:-1])
    assert chunks[-1] == "after\n"


def test_chunk_hard_splits_long_line_inside_code_block():
    text = "```\n" + "y" * 300 + "\n```"
    chunks = chunk_message(text, limit=100)
    assert all(len(c) <= 100 for c in chunks)
    assert all(c.count("```") == 2 for c in chunks)
    joined = "".join(c.replace("```", "").replace("\n", "") for c in chunks)
    assert joined == "y" * 300


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<@123> hello", "hello"),
        ("<@!123> hello", "hello"),
        ("hey <@123> there", "hey  there".strip()),
        ("no mention", "no mention"),
        ("<@999> keep other", "<@999> keep other"),
    ],
)
def test_strip_self_mention(content, expected):
    assert strip_self_mention(content, 123) == expected
