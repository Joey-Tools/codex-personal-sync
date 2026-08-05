---
id: 20260805-macos-scheduler-enable-before-bootstrap
title: macOS Scheduler Enable Before Bootstrap
status: completed
created: 2026-08-05
updated: 2026-08-05
branch: wip/recover-private-control
pr:
supersedes: []
superseded_by:
---

# macOS Scheduler Enable Before Bootstrap

## Summary

- Host acceptance exposed a deterministic activation failure: canonical `launchctl bootstrap user/$UID <plist>` returned service-disabled after the installer had disabled the same label through `gui/$UID`.
- launchd's disabled state for the label is shared across those domains, so activation now enables the exact `user/$UID/<label>` before bootstrap and retains the post-bootstrap enable as an idempotent repair.
- Linux scheduler activation and macOS uninstall ordering are unchanged.

## Recovery Contract

- A failed bootstrap continues to retain the canonical activation-incomplete marker and audited plist.
- Exact retry preserves the plist object and interval, enables the background-domain label before bootstrap, bootstraps the retained plist, repeats enable, and only then commits the marker.
- All native boundaries retain the existing descriptor-bound checks for canonical and legacy plist identity, bytes, access policy, parent chain, and exact absence.

## Delivery Boundary

- Repository code, regression tests, documentation, source lock, fresh review, and signed PR checkpoint are in scope.
- Host scheduler recovery remains an explicit operator action and is not performed by this workstream.

## Validation

- Focused regression covers both the failed first bootstrap with retained activation marker and exact retry argv ordering.
- Guardian regression retains the production process-group fence and replaces an immediate second FIFO read with a `select(2)`-driven absolute-deadline EOF gate. A real inherited-writer negative control proves that closing only the parent writer cannot satisfy the gate; bounded CPU-load reproduction confirmed that the old assertion could observe `EAGAIN` before EOF.
- Python 3.13 and system Python 3.9 each passed the final 38-test focused matrix. Under an eight-worker CPU load, 50 positive guardian checks plus one negative control passed; a separate 100-iteration probe observed two immediate `EAGAIN` windows and a maximum return-to-EOF delay of 0.001958709 seconds.
- The final sandbox-external Python 3.13 suite passed 1115 tests in 1317.494 seconds with 3 skips and no failures or errors. Source-lock refresh/check, dual-runtime `py_compile`, Ruff, diff-check, and project-journal validation also passed before delivery review.
