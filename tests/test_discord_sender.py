"""Tests for the Discord sender's chunking — Discord's 6000-char limit is
per-MESSAGE-summed, not per-embed. Without the chunker, large weeks blew
past it (today's 2026-06-15 run failed at ~6534 chars total)."""

from src.discord.sender import (
    MAX_CHARS_PER_MESSAGE,
    MAX_EMBEDS_PER_MESSAGE,
    _embed_char_count,
    chunk_embeds,
)


def _embed(title="t", desc="d", footer="f"):
    return {"title": title, "description": desc, "footer": {"text": footer}}


class TestEmbedCharCount:
    def test_counts_title_description_footer(self):
        e = _embed(title="hello", desc="world!!", footer="abc")
        # 5 + 7 + 3 = 15
        assert _embed_char_count(e) == 15

    def test_handles_missing_fields(self):
        assert _embed_char_count({}) == 0
        assert _embed_char_count({"title": "x"}) == 1
        assert _embed_char_count({"description": None}) == 0

    def test_counts_fields_and_author(self):
        e = {
            "title": "T",                              # 1
            "description": "D",                        # 1
            "footer": {"text": "F"},                   # 1
            "author": {"name": "AA"},                  # 2
            "fields": [
                {"name": "n1", "value": "vvv"},        # 2 + 3
                {"name": "nn", "value": "v"},          # 2 + 1
            ],
        }
        # 1+1+1+2+2+3+2+1 = 13
        assert _embed_char_count(e) == 13


class TestChunkEmbeds:
    def test_empty_input_returns_empty(self):
        assert chunk_embeds([]) == []

    def test_under_both_limits_single_message(self):
        embeds = [_embed(desc="x" * 500)] * 3
        batches = chunk_embeds(embeds)
        assert len(batches) == 1
        assert len(batches[0]) == 3

    def test_splits_at_char_limit(self):
        # 3 embeds × 2500 chars = 7500 total → must split
        embeds = [_embed(desc="x" * 2500)] * 3
        batches = chunk_embeds(embeds, max_chars=5800, max_count=10)
        assert len(batches) >= 2
        for batch in batches:
            assert sum(_embed_char_count(e) for e in batch) <= 5800

    def test_splits_at_count_limit(self):
        # 12 tiny embeds → must split into 2 (cap is 10/message)
        embeds = [_embed(desc="x")] * 12
        batches = chunk_embeds(embeds, max_chars=10_000, max_count=10)
        assert len(batches) == 2
        assert len(batches[0]) == 10
        assert len(batches[1]) == 2

    def test_no_dropped_embeds(self):
        embeds = [_embed(desc="x" * 1500) for _ in range(7)]
        batches = chunk_embeds(embeds)
        total = sum(len(b) for b in batches)
        assert total == len(embeds)

    def test_replays_the_2026_06_15_overflow(self):
        """The actual case that caused today's run failure: 9 embeds summing
        to 6534 chars. With chunking, this must split into ≥2 messages."""
        sizes = [1064, 306, 423, 1576, 417, 327, 2216, 45, 160]   # from today's data
        embeds = [_embed(desc="x" * s) for s in sizes]
        batches = chunk_embeds(embeds)
        assert len(batches) >= 2, "must split when total > 6000"
        for i, batch in enumerate(batches):
            chars = sum(_embed_char_count(e) for e in batch)
            assert chars <= MAX_CHARS_PER_MESSAGE, (
                f"batch {i} has {chars} chars, exceeds {MAX_CHARS_PER_MESSAGE}"
            )

    def test_oversize_single_embed_still_yielded(self):
        """A single embed bigger than max_chars is still emitted alone
        (better to try sending than silently drop it; Discord will reject
        and we'll see the failure clearly)."""
        embeds = [_embed(desc="x" * 7000)]
        batches = chunk_embeds(embeds)
        assert len(batches) == 1
        assert len(batches[0]) == 1
