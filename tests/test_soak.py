import json
from pathlib import Path

import pytest

from mailsentry.soak import SoakConfig, run_soak


def test_accelerated_ten_account_soak_reconciles_across_restart(
    tmp_path: Path,
) -> None:
    metrics_path = tmp_path / "metrics.jsonl"

    report = run_soak(
        SoakConfig(
            database_path=tmp_path / "soak.db",
            metrics_path=metrics_path,
            duration_seconds=12,
            event_interval_seconds=3,
            seed=20260915,
            accelerated=True,
        )
    )

    assert report.passed is True
    assert report.release_evidence is False
    assert report.accounts == 10
    assert report.generated == 40
    assert report.observed == 41
    assert report.persisted == 40
    assert report.duplicate_events == 0
    assert report.disconnects == 1
    assert report.idle_renewals >= 10
    assert report.uidvalidity_changes == 1
    assert report.service_restarts == 1
    assert report.notification_failures == 1
    assert report.notifications_pending == 0
    assert report.cursor_mismatches == 0
    assert report.privacy_marker_found is False
    assert report.failures == ()

    samples = [json.loads(line) for line in metrics_path.read_text().splitlines()]
    assert samples[-1]["kind"] == "final"
    assert samples[-1]["passed"] is True


def test_accelerated_soak_is_reproducible_for_same_seed(tmp_path: Path) -> None:
    reports = []
    for run_number in (1, 2):
        reports.append(
            run_soak(
                SoakConfig(
                    database_path=tmp_path / f"soak-{run_number}.db",
                    metrics_path=tmp_path / f"metrics-{run_number}.jsonl",
                    duration_seconds=10,
                    event_interval_seconds=2,
                    seed=77,
                    accelerated=True,
                )
            )
        )

    deterministic_fields = (
        "accounts",
        "generated",
        "observed",
        "persisted",
        "duplicate_events",
        "disconnects",
        "uidvalidity_changes",
        "service_restarts",
        "notification_failures",
        "notifications_pending",
        "cursor_mismatches",
        "privacy_marker_found",
        "failures",
    )
    assert {
        field: getattr(reports[0], field) for field in deterministic_fields
    } == {field: getattr(reports[1], field) for field in deterministic_fields}


def test_soak_fails_when_peak_rss_exceeds_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("mailsentry.soak._rss_mib", lambda: (64.0, 513.0))

    report = run_soak(
        SoakConfig(
            database_path=tmp_path / "soak.db",
            metrics_path=tmp_path / "metrics.jsonl",
            duration_seconds=4,
            event_interval_seconds=2,
            accelerated=True,
        )
    )

    assert report.passed is False
    assert report.release_evidence is False
    assert report.failures == (
        "peak RSS exceeded limit: peak=513.00MiB limit=512MiB",
    )


def test_soak_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    database_path = tmp_path / "production.db"
    database_path.write_bytes(b"do not overwrite")

    with pytest.raises(ValueError, match="refusing to overwrite"):
        run_soak(
            SoakConfig(
                database_path=database_path,
                metrics_path=tmp_path / "metrics.jsonl",
                duration_seconds=4,
                event_interval_seconds=2,
                accelerated=True,
            )
        )

    assert database_path.read_bytes() == b"do not overwrite"
