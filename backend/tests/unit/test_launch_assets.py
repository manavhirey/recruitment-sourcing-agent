from pathlib import Path

import yaml


def test_snapshot_expiry_failure_alert_fires_before_event_ages_out() -> None:
    alerts_path = Path(__file__).parents[3] / "ops" / "prometheus" / "alerts.yml"
    document = yaml.safe_load(alerts_path.read_text())
    rules = document["groups"][0]["rules"]
    snapshot_rule = next(
        rule for rule in rules if rule.get("alert") == "SnapshotExpiryFailure"
    )

    assert snapshot_rule["for"] == "0m"
