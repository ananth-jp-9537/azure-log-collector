"""Tests for BlobLogProcessor concurrency-safe checkpoint merge."""

from BlobLogProcessor import _merge_checkpoints


class TestMergeCheckpoints:
    def test_takes_later_timestamp_per_account(self):
        remote = {"sa-east": "2026-04-10T00:00:00+00:00"}
        local = {"sa-east": "2026-04-11T00:00:00+00:00"}
        merged = _merge_checkpoints(remote, local)
        assert merged == {"sa-east": "2026-04-11T00:00:00+00:00"}

    def test_keeps_remote_when_newer(self):
        remote = {"sa-east": "2026-04-12T00:00:00+00:00"}
        local = {"sa-east": "2026-04-11T00:00:00+00:00"}
        merged = _merge_checkpoints(remote, local)
        assert merged == {"sa-east": "2026-04-12T00:00:00+00:00"}

    def test_preserves_accounts_from_both_sides(self):
        remote = {"sa-east": "2026-04-10T00:00:00+00:00"}
        local = {"sa-west": "2026-04-11T00:00:00+00:00"}
        merged = _merge_checkpoints(remote, local)
        assert merged == {
            "sa-east": "2026-04-10T00:00:00+00:00",
            "sa-west": "2026-04-11T00:00:00+00:00",
        }

    def test_local_fills_empty_remote_entry(self):
        remote = {"sa-east": ""}
        local = {"sa-east": "2026-04-11T00:00:00+00:00"}
        merged = _merge_checkpoints(remote, local)
        assert merged == {"sa-east": "2026-04-11T00:00:00+00:00"}

    def test_handles_empty_or_bad_remote(self):
        assert _merge_checkpoints({}, {"sa": "t"}) == {"sa": "t"}
        assert _merge_checkpoints(None, {"sa": "t"}) == {"sa": "t"}
