# ngrok tunnel — wiring the local API to a public URL

```
Web app ──HTTPS──▶ ngrok edge ──tunnel──▶ 127.0.0.1:8000 ──spawn──▶ waitlist job
        X-Webhook-Secret-Token           (loopback only)     (background)
```

| File | Purpose |
|---|---|
| `ngrok.yml` | Tunnel definition. **No secrets** — safe in git. |
| `traffic-policy.yml` | Edge filtering (paths, methods, header presence, body size). |
| `start_tunnel.ps1` | Starts and *verifies* the chain. Use this rather than raw `ngrok http`. |
| `test_remote.py` | Proves the tunnel from outside, the way your web app calls it. |

The authtoken lives in ngrok's own global config
(`%LOCALAPPDATA%\ngrok\ngrok.yml`), deliberately **outside the repo**.

---

## One-time setup

### 1. Install the agent

```powershell
winget install ngrok.ngrok
```

> **Two gotchas hit on this machine, both real:**
>
> **a) winget ships ngrok 3.3.1**, which only understands config schema v1/v2.
> The `version: "3"` / `endpoints:` syntax needs agent 3.5+ and fails with
> `unknown version '3'`. `ngrok.yml` here is written in **v2** so it works on
> both. The cost: `traffic_policy` is not supported by 3.3.1, so the edge
> policy only takes effect once you upgrade.
>
> **b) Windows Defender quarantines a manually-downloaded `ngrok.exe`** as
> PUA (`PUAProtection = 2` on this machine). Tunneling agents are flagged by
> heuristic because attackers use them too. If you want the current agent:
>
> ```powershell
> # Run as Administrator. This is YOUR call — it lowers a real protection.
> Add-MpPreference -ExclusionPath "C:\ngrok"
> Remove-Item C:\ngrok\ngrok.exe -Force -ErrorAction SilentlyContinue
> # then re-download:
> Invoke-WebRequest "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip" `
>   -OutFile "$env:TEMP\ngrok.zip" -UseBasicParsing
> Expand-Archive "$env:TEMP\ngrok.zip" -DestinationPath C:\ngrok -Force
> C:\ngrok\ngrok.exe version
> ```
>
> The winget 3.3.1 build works fine for tunneling; you only need the upgrade
> for edge Traffic Policy.

### 2. Register your authtoken

Sign up at <https://dashboard.ngrok.com/signup>, copy the token from
<https://dashboard.ngrok.com/get-started/your-authtoken>, then:

```powershell
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
```

Verify: `ngrok config check`

---

## Every time

```powershell
# 1. API (leave running)
python -m src.api

# 2. Tunnel, in a second terminal
.\ngrok\start_tunnel.ps1
```

`start_tunnel.ps1` refuses to start if the API is down (a tunnel to a dead port
just returns 502s with no obvious cause), then prints the public URL and runs a
real authenticated request through it — including asserting that an
**unauthenticated** request is refused.

Add `-StartApi` to have it launch the API too.

### Verify from outside

Run this from a *different* machine or network — loopback working proves nothing
about reachability:

```powershell
python ngrok/test_remote.py https://YOUR-URL.ngrok-free.app
python ngrok/test_remote.py https://YOUR-URL.ngrok-free.app --trigger   # + dry run
```

---

## The admin console over a free tunnel — read this first

**`/console` does not work over a free `*.ngrok-free.dev` URL.** Verified, not
assumed. API clients work fine; browsers do not.

ngrok shows an HTML interstitial (`ERR_NGROK_6024`) to browser-looking requests
and skips it when `ngrok-skip-browser-warning: true` is present. `curl` can send
that header — **a browser loading a page cannot.** So `/console` returns ngrok's
warning page instead of your console.

Things that do **not** fix it (all tested):

| Attempt | Result |
|---|---|
| `add-headers` action in the traffic policy | No effect — the interstitial is decided **before** policy runs, on the **inbound** User-Agent |
| Clicking "Visit Site" | Loads once, but the bypass does not survive the next navigation |
| An inbound header-rewrite flag | `ngrok start` has none |

**What actually works:**

1. **Use the console locally** — `http://127.0.0.1:8000/console`. No
   interstitial, no tunnel involved. This is the recommended path.
2. **Reserve a domain** (paid, ~$10/mo). Custom domains do not get the
   interstitial. Then uncomment `url:` in `ngrok.yml`.
3. **Use a different tunnel** — Cloudflare Quick Tunnels have no interstitial.
   See Appendix A in `API_TUNNEL_SETUP.md`. Note they also have no edge auth,
   so the API token carries the whole load.

The tunnel remains fully working for **API traffic** — which is what your web
app actually needs. `python ngrok/test_remote.py <url>` passes 7/7.

---

## Calling it from your web app

```javascript
// SERVER-SIDE ONLY. This token must never reach a browser bundle —
// anyone holding it can run jobs on your machine.
const res = await fetch(`${process.env.VFS_WEBHOOK_URL}/trigger/waitlist`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-Webhook-Secret-Token": process.env.VFS_WEBHOOK_SECRET,
    "ngrok-skip-browser-warning": "true",
  },
  body: JSON.stringify({ route: "AE-CHE", dry_run: true }),
});
```

**The free-tier URL changes on every restart.** Keep it configurable
(`VFS_WEBHOOK_URL`), never hardcoded. A reserved domain fixes this — uncomment
`domain:` in `ngrok.yml`.

Always send `ngrok-skip-browser-warning: true`, or free ngrok returns an HTML
interstitial where your app expects JSON.

---

## Security posture, stated plainly

**The FastAPI token is the real boundary.** On the free tier the tunnel adds
HTTPS and (on a current agent) path filtering — it does not authenticate.

| Control | Free | Paid |
|---|:--:|:--:|
| HTTPS at the edge | yes | yes |
| Traffic Policy (needs agent 3.5+) | yes | yes |
| Static domain | no | yes |
| IP allowlist | no | yes |
| **App token (`X-Webhook-Secret-Token`)** | yes | yes |

So: keep `.env.api` out of git (it is gitignored), and rotate the token if it is
ever pasted anywhere. Anyone with URL + token can spawn a job on your desktop.

`traffic-policy.yml` only checks that the token header is *present*, not its
value — the ngrok config is a plain file on disk and the secret does not belong
in it. Value comparison stays in `src/api/security.py`, in constant time.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `unknown version '3'` | Old agent. `ngrok.yml` here is v2 already; you edited it to v3. |
| `ERR_NGROK_4018` | No authtoken. `ngrok config add-authtoken ...` |
| Agent won't run, "contains a virus" | Defender PUA. See the exclusion note above. |
| HTML instead of JSON | Send `ngrok-skip-browser-warning: true`. |
| `502` from ngrok | The API is down. `curl http://127.0.0.1:8000/health` |
| `401` with a token sent | Header must be exactly `X-Webhook-Secret-Token`. Check for a trailing newline from copy-paste. |
| `409` on trigger | A job is already running (single-flight). `GET /jobs` |
| URL changed after restart | Free-tier behaviour. Reserve a domain or keep it configurable. |
| Tunnel up but nothing arrives | Watch live traffic at <http://127.0.0.1:4040> |
