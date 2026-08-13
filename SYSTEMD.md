# Running as a service

**Do not run this from an open terminal in production.** A Claude Code session,
an SSH window, or a `tmux` pane is not a production runtime — when it closes,
monitoring stops, and monitoring that stops silently is worse than no
monitoring, because you will believe you are covered.

The runtime must survive a Python exception, an API failure, an internet
interruption, a server reboot, a Claude outage, a website timeout, a malformed
API response, and recoverable state corruption. The code handles the first
seven; systemd handles the reboot and the process-level restart.

---

## 1. Service user

Run as a dedicated unprivileged user that owns nothing else.

```bash
sudo useradd --system --create-home --home-dir /opt/thb-seo --shell /usr/sbin/nologin thb
sudo mkdir -p /opt/thb-seo
sudo chown -R thb:thb /opt/thb-seo
```

## 2. Install

```bash
sudo -u thb git clone https://github.com/rosanes-cmyk/THB-AI-SEO-Operations-System.git /opt/thb-seo/app
cd /opt/thb-seo/app

sudo -u thb python3 -m venv /opt/thb-seo/venv
sudo -u thb /opt/thb-seo/venv/bin/pip install -r requirements.txt

# Chromium for the vision engine. Without it, vision reports "unavailable"
# and every other agent keeps running.
sudo -u thb /opt/thb-seo/venv/bin/playwright install chromium
sudo /opt/thb-seo/venv/bin/playwright install-deps chromium
```

## 3. Configuration and secrets

```bash
sudo -u thb cp config.example.yaml config.yaml
sudo -u thb $EDITOR config.yaml          # set real money pages + revenue weights

sudo -u thb cp .env.example .env
sudo -u thb $EDITOR .env                 # API keys, webhook URL
sudo chmod 600 /opt/thb-seo/app/.env     # the environment file is a secret
```

Verify before starting the service:

```bash
sudo -u thb /opt/thb-seo/venv/bin/python main.py config-check
sudo -u thb /opt/thb-seo/venv/bin/python main.py selftest
```

`config-check` reports which credentials are present without printing any of
them. `selftest` proves failure isolation offline — it makes no network calls.

## 4. Unit file

`/etc/systemd/system/thb-seo.service`:

```ini
[Unit]
Description=THB AI SEO Operations System
Documentation=https://github.com/rosanes-cmyk/THB-AI-SEO-Operations-System
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=thb
Group=thb
WorkingDirectory=/opt/thb-seo/app
EnvironmentFile=/opt/thb-seo/app/.env
ExecStart=/opt/thb-seo/venv/bin/python /opt/thb-seo/app/runner.py

# --- Never-stop behavior -------------------------------------------------
Restart=always
RestartSec=10
# Do not give up: a site outage plus a crash loop must not disable monitoring.
StartLimitIntervalSec=0

# SIGTERM lets the runner finish its cycle and flush state atomically.
KillSignal=SIGTERM
TimeoutStopSec=60

# --- Hardening -----------------------------------------------------------
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/thb-seo/app/data /opt/thb-seo/app/reports /opt/thb-seo/app/logs
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

# --- Resource ceilings ---------------------------------------------------
# Chromium is memory-hungry; a bounded OOM kill plus restart beats swapping
# the box to death.
MemoryMax=2G
TasksMax=512

StandardOutput=journal
StandardError=journal
SyslogIdentifier=thb-seo

[Install]
WantedBy=multi-user.target
```

`ProtectSystem=strict` makes the whole filesystem read-only except the three
`ReadWritePaths`. If you move `THB_DATA_DIR`, `THB_REPORTS_DIR`, or
`THB_LOG_DIR` in `.env`, add the new paths here or the service will fail to
write and restart in a loop.

## 5. Enable and start

```bash
sudo systemctl daemon-reload
sudo systemctl enable thb-seo      # start on boot
sudo systemctl start thb-seo
sudo systemctl status thb-seo
```

## 6. Operating it

| Task | Command |
|---|---|
| Status | `systemctl status thb-seo` |
| Live logs | `journalctl -u thb-seo -f` |
| Errors only | `journalctl -u thb-seo -p err --since "24 hours ago"` |
| Restart | `sudo systemctl restart thb-seo` |
| Stop | `sudo systemctl stop thb-seo` |
| Health snapshot | `sudo -u thb /opt/thb-seo/venv/bin/python main.py health` |
| One heartbeat | `sudo -u thb /opt/thb-seo/venv/bin/python main.py heartbeat` |
| Force a digest | `sudo -u thb /opt/thb-seo/venv/bin/python main.py digest` |
| Risk policy | `sudo -u thb /opt/thb-seo/venv/bin/python main.py policy` |

Application logs also go to `logs/thb.log` (rotating, 10 MB × 5). All logs pass
through a redaction filter, so API keys and the Chat webhook URL never reach
either journald or disk.

## 7. Dead-man switch

Every cycle writes `data/heartbeat.json`. If its mtime stops advancing, the
process is wedged even though systemd still shows it "active".

```bash
# Alert if the heartbeat is more than 15 minutes stale.
find /opt/thb-seo/app/data/heartbeat.json -mmin +15 -print
```

For external supervision, set `THB_HEALTHCHECK_PING_URL` in `.env` to a
healthchecks.io (or equivalent) ping URL. The runner pings it every cycle. A
failed ping is logged and never fatal.

## 8. Upgrade

```bash
cd /opt/thb-seo/app
sudo -u thb git fetch origin
sudo -u thb git log --oneline HEAD..origin/main       # review before pulling
sudo -u thb git rev-parse HEAD > /tmp/thb-previous    # record the rollback point
sudo -u thb git pull

sudo -u thb /opt/thb-seo/venv/bin/pip install -r requirements.txt
sudo -u thb /opt/thb-seo/venv/bin/python -m pytest tests/ -q
sudo -u thb /opt/thb-seo/venv/bin/python main.py config-check
sudo -u thb /opt/thb-seo/venv/bin/python main.py selftest

sudo systemctl restart thb-seo
journalctl -u thb-seo -n 50
```

Do not restart if the tests or `config-check` fail. A running old version beats
a broken new one.

## 9. Rollback

```bash
cd /opt/thb-seo/app
sudo -u thb git checkout "$(cat /tmp/thb-previous)"
sudo -u thb /opt/thb-seo/venv/bin/pip install -r requirements.txt
sudo systemctl restart thb-seo
```

State is forward-compatible within a schema version: `data/state.json` carries a
`version` field, and an unreadable state file is quarantined as
`state.json.corrupt-<timestamp>` rather than deleted. Rolling back therefore
loses no incident history. If you need a clean slate:

```bash
sudo systemctl stop thb-seo
sudo -u thb mv data/state.json data/state.json.manual-backup
sudo systemctl start thb-seo
```

## 10. Disaster recovery

Back up `data/` — it holds incident history, the dead-man heartbeat, the Chat
outbox, and `audit_journal.jsonl` (the append-only record of every change the
system prepared or executed). `reports/` is reproducible and can be dropped.

```bash
sudo tar czf "thb-backup-$(date +%F).tar.gz" -C /opt/thb-seo/app data config.yaml
```

Never back up `.env` to the same place as the code. It is the credential store.

## 11. What this service will not do

Production WordPress writes are disabled by default and are gated behind four
independent checks (adapter flag, risk tier, recorded approver, captured
rollback data). Setting `THB_WORDPRESS_WRITES_ENABLED=true` satisfies exactly
one of the four. Leave it `false` until Stage 8 has proven each action type
individually — see `actions/policy.py` for the machine-readable policy.
