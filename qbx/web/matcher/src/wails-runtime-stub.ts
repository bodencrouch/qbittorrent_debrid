/** Minimal stub replacing @wailsio/runtime Dialogs used by MatchingPanel. */
export const Dialogs = {
  async OpenFile(_opts?: unknown): Promise<string | null> {
    return prompt("Enter directory path on the qbx host:") || null;
  },
};
