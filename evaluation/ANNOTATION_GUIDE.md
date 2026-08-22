# Matching evaluation annotation guide

Use this importer only with approved, de-identified recruiter-panel evidence. The launch gate requires at least 30 jobs spanning both India (`IN`) and the United States (`US`), with at least 20 independently reviewed candidate judgments per job.

- Replace names with stable candidate aliases and remove email, phone, profile URLs, raw job descriptions, and free-text notes that identify a person or customer.
- Set `hard_gate_eligible` from the confirmed scorecard, before looking at the model result.
- Set `relevant` from the recruiter's role-fit judgment. Do not infer it from CRM stage or model score.
- Set `expected_classification` for mandatory hard-gate fixtures. Any later classification change fails the release gate.
- Store only an HMAC-SHA256 keyed digest in `annotator_hash`, formatted as `hmac-sha256:` followed by 64 lowercase hexadecimal characters. Keep the panel roster and consent/approval evidence outside this repository.
- Keep `annotation_version` stable for one annotation protocol. Put only the SHA-256 digest of the immutable, access-controlled evidence artifact in `panel_reference`, formatted as `sha256:` followed by 64 lowercase hexadecimal characters. URLs, emails, names, and storage paths are not accepted.
- An authorized reviewer must create the `recruiter-panel-approval-v1` manifest outside the repository. It binds the exact dataset SHA-256 to an approval reference and timestamp. Store the manifest JSON and its SHA-256 in the protected `recruiter-panel-approval` CI environment as `RECRUITER_PANEL_APPROVAL_MANIFEST_JSON` and `RECRUITER_PANEL_APPROVAL_MANIFEST_SHA256`; pull requests and repository fixtures cannot supply either value.
- Ties are ordered by the de-identified `candidate_key`, so fixture ordering cannot change NDCG.

The committed `synthetic_*` files test evaluator mechanics only. Never copy them into `jobs.jsonl` or `judgments.jsonl` to satisfy the launch gate.
