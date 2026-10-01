---
id: 20260813-rci001
title: Required CI Reusable Entry
status: completed
created: 2026-08-13
updated: 2026-08-13
branch: codex/daily-skill-friction-20260813-codex-personal-sync-codex-review-v2
pr:
supersedes: []
superseded_by:
---

# Required CI Reusable Entry

## Summary
- Added a reusable required-CI entry that preserves the existing required Linux source-lock, compile, and private-temporary test closure without promoting the diagnostic macOS matrix.

## Current State
- `.github/workflows/required-ci.yml` is callable only through `workflow_call`, uses read-only contents permission, and retains the canonical source-lock and private temporary-directory contracts.
- The existing event-driven `.github/workflows/ci.yml` remains unchanged for rollout canaries.

## Next Steps
- None in this repository slice.

## Evidence
- `python3 -m unittest tests.test_required_ci_workflow`
- `bash scripts/run_ci_tests_with_private_tmp.sh -- python3 -m unittest discover -s tests`
