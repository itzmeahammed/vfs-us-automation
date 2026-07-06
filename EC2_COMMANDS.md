# EC2 Commands Cheat-Sheet

Quick reference for operating the VFS slot checker on the EC2 box. Run these in
your SSH session unless noted otherwise.

Project dir: `/opt/vfs-malta-slot-checker` — `cd` there first and you can drop the
full path from most commands.

```bash
cd /opt/vfs-malta-slot-checker
```

---

## Quick diagnostic commands (copy-paste)

**Show active routes parsed by the bot**
```bash
.venv/bin/python -c "from src.utils.config_reader import initialize_config as i; i(); from src.supervisor import _all_routes; print(_all_routes())"
```

**Preview hourly rotation schedule**
```bash
.venv/bin/python -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils import credentials as c; [print(h,'cred'+str(x),e) for h,x,e in c.rotation_schedule()]"
```

---

## 1. Logs

| When to use | Command |
|---|---|
| **Watch a run live** (e.g. wait for the next :29/:59) | `tail -f app.log` |
| Live, milestones only (less noise) | `tail -f app.log \| grep --line-buffered -E "Route\|Using credential\|Reached dashboard\|Slot report\|succeeded\|FAILED\|Geo-blocked\|skipping\|Telegram"` |
| See the last run | `tail -n 60 app.log` |
| Only failures/errors | `grep -E "FAILED\|Geo-blocked\|ERROR" app.log \| tail -20` |
| How big is the log | `ls -lh app.log` |

> `Ctrl+C` stops watching (`tail -f`) — it does NOT stop the bot.

---

## 2. Run the bot manually

| When to use | Command |
|---|---|
| **Test a run now** (don't wait for cron) | `./run_ec2.sh` |
| Run + immediately see the result | `./run_ec2.sh; tail -n 60 app.log` |
| Run only ONE route | `xvfb-run -a .venv/bin/python -m src.supervisor -sc AE -dc MT` |

> Requires the home-IP tunnel to be up (windows A + B on your PC). See
> `TUNNEL_RESTART_GUIDE.md`.

---

## 3. Stop things

| When to use | Command |
|---|---|
| **Kill the currently-running bot** (mid-run) | `pkill -f "vfs-malta-slot-checker"` |
| Also kill leftover Chrome | `pkill -f "google-chrome"` |
| Also kill the xvfb wrapper | `pkill -f "xvfb-run"` |
| Release the lock after killing | `rm -f /tmp/vfs-slot-checker.lock` |
| Confirm nothing is running | `ps aux \| grep -E "supervisor\|run_ec2\|chrome" \| grep -v grep` |

**Stop ALL of it (current run + clean up):**
```bash
pkill -f "vfs-malta-slot-checker"; pkill -f google-chrome; pkill -f xvfb-run; rm -f /tmp/vfs-slot-checker.lock
```

---

## 4. Cron (the schedule)

| When to use | Command |
|---|---|
| **Edit the schedule** (add/change/disable) | `crontab -e` |
| See the current schedule | `crontab -l` |
| Confirm cron fired the job | `grep CRON /var/log/syslog \| grep run_ec2 \| tail -5` |
| Is the cron service running | `systemctl status cron \| head -5` |
| Restart cron (after timezone change) | `sudo systemctl restart cron` |

**The schedule line** (twice/hour, 6am–midnight Dubai):
```cron
29,59 6-23 * * * /opt/vfs-malta-slot-checker/run_ec2.sh >> /opt/vfs-malta-slot-checker/app.log 2>&1
```

**Pause the bot** (stop future runs): `crontab -e`, put `#` at the start of the line.
**Resume:** remove the `#`.

---

## 5. URLs / routes

| When to use | Command |
|---|---|
| **View configured URLs** | `cat config/vfs_urls.ini` |
| **Edit URLs** (add/remove/disable a route) | `nano config/vfs_urls.ini` |
| See which routes the bot will actually run | `.venv/bin/python -c "from src.utils.config_reader import initialize_config as i; i(); from src.supervisor import _all_routes; print(_all_routes())"` |
| List the route JSON files (each URL needs one) | `ls config/routes/` |

Editing `vfs_urls.ini`: add `SRC-DEST = <login-url>`; disable with a leading `;`;
remove by deleting the line. Save in nano: `Ctrl+O`, Enter, `Ctrl+X`.

---

## 6. Credentials (multiple-account rotation)

| When to use | Command |
|---|---|
| **Edit accounts** | `nano config/credentials.local.ini` |
| Preview which email runs each hour | `.venv/bin/python -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils import credentials as c; [print(h,'cred'+str(x),e) for h,x,e in c.rotation_schedule()]"` |

> Each `[credN]` section needs exactly ONE `email` and ONE `password`. A malformed
> file breaks the WHOLE bot (config load fails) — always run the preview after
> editing to confirm it's valid.

---

## 7. Telegram test

| When to use | Command |
|---|---|
| **Send a test message** | `.venv/bin/python -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils import telegram; telegram.send_message('Test from EC2')"` |
| Check Telegram is configured | `.venv/bin/python -c "from src.utils.config_reader import initialize_config as i; i(); from src.utils import telegram; print('configured:', telegram.is_configured())"` |

---

## 8. Update the code (deploy changes)

| When to use | Command |
|---|---|
| **Pull latest code from GitHub** | `git pull` |
| If `git pull` conflicts with local EC2 edits | `git stash; git pull; git stash pop` |
| Reinstall deps (after requirements change) | `.venv/bin/pip install -r requirements.txt` |

> Code changes take effect on the **next run** automatically — no restart needed.
> `config/config.local.ini` and `config/credentials.local.ini` are gitignored, so
> `git pull` never touches your secrets.

---

## 9. Tunnel / IP (home-IP routing)

| When to use | Command |
|---|---|
| **Verify EC2 exits through your UAE IP** | `curl --socks5-hostname 127.0.0.1:1080 -s -m 20 https://ipinfo.io/country` |
| Check what EC2's own IP geolocates to | `curl -s https://ipinfo.io/country` |
| Is the tunnel port listening | `ss -tlnp \| grep 1080` |

> `AE` = tunnel working (VFS sees your UAE IP). Full tunnel setup in
> `TUNNEL_RESTART_GUIDE.md`.

---

## 10. Timezone

| When to use | Command |
|---|---|
| Check current time/zone | `timedatectl` |
| Set to Dubai | `sudo timedatectl set-timezone Asia/Dubai` then `sudo systemctl restart cron` |

---

## Common situations → what to run

- **"Did the last run work?"** → `tail -n 60 app.log`
- **"Watch it run now"** → `./run_ec2.sh` in one window, `tail -f app.log` in another
- **"Stop everything"** → the stop-all one-liner in section 3 + comment the crontab line
- **"It's failing / not running"** → `crontab -l`, then `grep CRON /var/log/syslog | grep run_ec2 | tail -5`, then `tail -n 40 app.log`
- **"Add a new portal"** → edit `vfs_urls.ini` + add `config/routes/SRC-DEST.json`
- **"Change accounts"** → `nano config/credentials.local.ini` + run the preview
