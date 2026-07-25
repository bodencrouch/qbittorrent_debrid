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

# One policy cycle per qBittorrent event batch

## Problem

Sync updates, duplicate handling, and debrid decisions used to feel like separate sparks. The UI looked flat, and it was hard to follow “this stalled torrent → this action.”

## What we changed

- Carry a stable `event_batch_id` through sync, policy passes, duplicates, and debrid work  
- Create a batch id when events arrive without one  
- Manual scans force duplicate checks  
- Group UI feedback for real policy passes; keep unrelated noise in a “loose reactions” lane  

## How to keep it healthy

- Wire new event handlers through the same batch id  
- Add tests when you add a new policy path  
- Keep queue-frontier rules conservative so active torrents are not jumped  
