from pathlib import Path

import yaml


def test_recruiter_panel_gate_only_blocks_explicit_launch_workflow() -> None:
    workflow_path = Path(__file__).parents[3] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(workflow_path.read_text())
    launch_job = workflow["jobs"]["launch-evidence"]

    assert launch_job["if"] == "github.event_name == 'workflow_dispatch'"
    assert launch_job["environment"] == "recruiter-panel-approval"

    materialize_step = next(
        step
        for step in launch_job["steps"]
        if step.get("name") == "Materialize protected recruiter-panel approval"
    )
    assert 'test -n "$APPROVAL_MANIFEST_JSON"' in materialize_step["run"]


def test_snapshot_expiry_failure_alert_fires_before_event_ages_out() -> None:
    alerts_path = Path(__file__).parents[3] / "ops" / "prometheus" / "alerts.yml"
    document = yaml.safe_load(alerts_path.read_text())
    rules = document["groups"][0]["rules"]
    snapshot_rule = next(
        rule for rule in rules if rule.get("alert") == "SnapshotExpiryFailure"
    )

    assert snapshot_rule["for"] == "0m"
