# Updates

qbx only **checks** GitHub Releases. It does not replace your install.

1. Set `updates.source_owner` / `updates.source_repo` (or fill them in Settings).  
2. Choose `stable` or `beta`.  
3. Use **Check for updates** or let the shell check once at startup.  

You get a release link and short reinstall commands. Typical source upgrade:

```bash
git fetch --tags && git checkout vX.Y.Z
./scripts/install-local.sh
```

Details: repository file `docs/UPDATES.md`.
