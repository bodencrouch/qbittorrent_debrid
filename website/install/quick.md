# Quick start

## Clone and install

```bash
git clone https://github.com/oldrepublicwizard/qbittorrent_debrid.git
cd qbittorrent_debrid
./install.sh
```

Or for an editable checkout:

```bash
pip install -e ".[dev]"
cd qbx/web/matcher && npm ci && npm run build
```

## Configure and run

```bash
qbx setup
qbx check
qbx serve
```

Open **http://127.0.0.1:8484**.

On Linux desktops, prefer [Desktop install](./desktop) so you get Kickoff entries and the tray launcher.
