---
title: "feat: Reliable Control Shell and desktop lifecycle"
status: completed
date: 2026-07-24
type: feat
---

# Control Shell packaging and desktop lifecycle

## In plain terms

We stopped shipping installs that looked fine but opened a blank “Control Shell not built” page. Installers now build the UI first (or fail), and the built files ship inside the Python wheel.

We also copied the useful desktop habits from ThirdFlare One: show the app version, **check** GitHub for updates (never auto-install), send a few desktop notifications, and let users turn on tray-at-login with a real XDG autostart file.

## What shipped

- `install.sh` / `scripts/install-local.sh` require a built Control Shell
- Hatch force-includes `qbx/web/matcher/dist`
- `/api/version`, `/api/update/check`, health includes `version`
- Desktop notifications (allowlisted events) + `POST /api/config/tray-autostart`
- Settings → Application UI for updates / notify / tray

## Deliberately not in scope

AppImage/deb pipelines and in-app binary replacement. Local installs get release links and reinstall commands only.
