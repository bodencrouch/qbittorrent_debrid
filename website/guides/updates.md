# Updates

qbx only **checks** GitHub Releases. It does not replace your install.

1. In Settings → Application, pick owner/repo (defaults to upstream; forks are listed) and channel.
2. Choose a release version — install commands appear for that tag.
3. Or use **Check for latest on channel** to compare against what you have installed.

`stable` excludes prereleases; `beta` includes alphas/betas/rcs. Typical source upgrade:

```bash
git fetch --tags && git checkout vX.Y.Z
./scripts/install-local.sh
```

Details: repository file `docs/UPDATES.md`.
