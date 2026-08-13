/**
 * Navigation that works everywhere qbx runs.
 *
 * Never use window.open for internal navigation. The PyQt6 tray shell hosts the
 * UI in a QWebEngineView whose default createWindow() returns null, so
 * window.open and target="_blank" are dropped silently — no window, no error.
 * That is what made the injected "Open qbx" button look dead.
 */

/** Navigate the current view to an app URL. */
export function openHostUrl(url: string): void {
  window.location.assign(url)
}

/** Open an external URL, which legitimately belongs in the desktop browser. */
export function openExternalUrl(url: string): void {
  // The tray shell intercepts this and hands it to QDesktopServices; in a real
  // browser it opens a tab as usual.
  window.open(url, "_blank", "noopener,noreferrer")
}
