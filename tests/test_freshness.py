"""Staleness check returns True when the prior cycle's last_updated/sha matches."""

from unittest.mock import patch

from src import ingest


def test_is_stale_matches_last_updated():
    with patch.object(ingest, "previous_signature", return_value=(1700000000, "abc")):
        assert ingest.is_stale(1700000000, "different_sha") is True


def test_is_stale_falls_back_to_sha_when_last_updated_unavailable():
    with patch.object(ingest, "previous_signature", return_value=(None, "abc")):
        assert ingest.is_stale(None, "abc") is True
        assert ingest.is_stale(None, "xyz") is False


def test_no_prior_cycle_means_not_stale():
    with patch.object(ingest, "previous_signature", return_value=(None, None)):
        assert ingest.is_stale(1700000000, "abc") is False


def test_changed_last_updated_means_fresh():
    with patch.object(ingest, "previous_signature", return_value=(1700000000, "abc")):
        assert ingest.is_stale(1700000600, "abc") is False
