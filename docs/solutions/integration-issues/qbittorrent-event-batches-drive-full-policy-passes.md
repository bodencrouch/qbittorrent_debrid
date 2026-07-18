---
title: "Propagate qBittorrent event batches through debrid policy passes"
date: "2026-07-07"
category: "docs/solutions/integration-issues"
module: "qbx.engine.interceptor"
problem_type: "integration_issue"
component: "service_object"
severity: "medium"
root_cause: "missing_workflow_step"
resolution_type: "workflow_improvement"
tags:
  - "qbittorrent"
  - "debrid"
  - "duplicates"
  - "queueing"
  - "event-batches"
---

# Problem
qBittorrent sync activity, duplicate handling, and debrid decisions were not presented as one policy cycle. That made the UI feel flat and made it harder to reason about stalled torrents, queue priority, and duplicate resolution together.

## Symptoms
- Manual scans did not force duplicate management.
- Event handling produced disconnected feedback instead of a single batch trail.
- Startup or loose reactions were mixed in with real policy passes.
- The debrid layer could not show clear lineage from a qBittorrent event to the resulting action.

## Solution
- Thread a stable `event_batch_id` through sync processing, policy passes, duplicate management, and downstream debrid actions.
- Assign a batch id automatically when event updates arrive without one.
- Make manual scans run duplicate checks explicitly with `force_duplicates=True`.
- Emit grouped UI feedback for policy passes and duplicate groups, while keeping unbatched noise in a separate loose-reactions lane.

## Validation
- `pytest -q` passed: `108 passed`.
- `node --check qbx/web/app.js` passed.
- Browser proof confirmed the timeline now renders grouped output, including `Batch #1` and a separate `Loose reactions` section.

## Prevention
- Keep event-batch propagation wired through any new qBittorrent event handler.
- Add tests whenever a new policy path is introduced so duplicate management cannot bypass the batch trail.
- Preserve conservative queue-frontier logic so only truly stalled torrents are debridbed.
