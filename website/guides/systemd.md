# systemd

Units live under `packaging/systemd/`.

```bash
sudo cp packaging/systemd/qbx.service packaging/systemd/qbx-nudge@.service /etc/systemd/system/
sudo cp packaging/systemd/qbx.env.example /etc/qbx/qbx.env
sudo systemctl daemon-reload
sudo systemctl enable --now qbx.service
```

Optional: point qBittorrent **Run external program** at:

```text
/usr/bin/systemctl start qbx-nudge@%I.service
```

That oneshot calls `qbx nudge --hash …`. Sync polling remains the backup if hooks are missing.

There is also a **user** unit template at `packaging/systemd/qbx.user.service` for `~/.local` installs. Do not stack that with tray autostart in a confusing way — pick one “keep the daemon alive” approach.
