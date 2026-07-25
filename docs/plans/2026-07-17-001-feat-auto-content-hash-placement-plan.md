---
title: "feat: Automatic content-hash file placement (move orphans / hardlink owned)"
type: feat
status: completed
date: 2026-07-17
origin: conversation (Control Shell hardlink UI → automatic placement)
---

# Automatic content-hash file placement

## In plain terms

Manual “find hardlinks” in the UI was the wrong tool for large libraries. The goal was quiet placement: if the bytes already exist under allowlisted folders, put them where this torrent expects them.

- Same bytes, not owned by another torrent → **move** into place  
- Same bytes, owned by another torrent → **hardlink** (same filesystem only)  
- Then ask qBittorrent to recheck (budgeted)

Manual size rematch (`qbx match` / Matching panel) stays available and is never mixed into this auto path. Auto placement stays off until you enable matcher + auto_placement and set folders.

## Design choices we kept

- Equivalence by full-file content hash, not size alone  
- Search only allowlisted roots (matcher folders + category paths)  
- Skip cross-device copies by default  
- Never block the interceptor sync loop  

See `matcher` in [CONFIGURATION.md](../CONFIGURATION.md) for the knobs.
