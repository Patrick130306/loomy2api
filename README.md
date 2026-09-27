# loomy2api

Turn the model quota of your **Loomy** (iFlytek / 科大讯飞 desktop AI assistant)
account into a plain, self-hosted **OpenAI- and Anthropic-compatible API** —
with a **multi-account pool** for rotation, failover and automatic session
renewal.

* **Zero dependencies** — pure Python standard library (3.9+). No `pip install`
  needed beyond the project itself.
* **No desktop client required** — the gateway logs in server-side with a phone
  number + password and keeps the 14-day session renewed on its own.
* **Multi-account** — add as many accounts as you like; requests are routed by
  balance / round-robin / LRU, dead sessions are cooled down and retried on the
  next account automatically.
* **Both API dialects** — `/v1/chat/completions` for OpenAI clients and
  `/v1/messages` for Claude Code & friends (streaming, tools and reasoning all
  translated).

[中文说明 / Chinese README](README.zh-CN.md) · [Reversed protocol reference](docs/PROTOCOL.md)

---

## Features

| | |
|---|---|
| OpenAI API | `POST /v1/chat/completions` (stream + non-stream), `GET /v1/models`, `POST /v1/embeddings`, `POST /v1/images/generations` |
| Anthropic API | `POST /v1/messages` (stream + non-stream), thinking blocks, tool use / tool results |
| Web panel | `http://127.0.0.1:17890/panel` — quota per account, add / remove / disable, force renew, rebind device identity, live log tail |
| Accounts | pool with `balance` · `round_robin` · `lru` strategies, per-account cooldown, automatic retry on another account, quota tracking |
| Identity | every account gets its own bound device identity (`devid` + promotions device id) generated at first login and reused on every renewal |
| Sessions | password login, SMS login, desktop-client session import, automatic renewal before the 14-day expiry |
| Ops | `/health`, `/v1/points`, `/v1/admin/accounts`, request log with model / tokens / `points_consumed` / latency |
| Security | optional API-key gate for the gateway itself; secrets stay out of git |

## Requirements

* Python **3.9+** (tested on 3.9 / 3.11 / 3.13, Windows and Linux)
* A Loomy account (phone number + a password set in the iFlytek account centre)
* Outbound HTTPS to `account.xfinfr.com` and `loomyad.xunfei.cn`

## Quick start

```bash
git clone https://github.com/Patrick130306/loomy2api.git
cd loomy2api

cp config.example.json config.json        # optional, defaults are sane
cp accounts.example.json accounts.json    # put your accounts here

# add an account and log it in (writes the session into accounts.json)
python -m loomy2api add main --phone 13800000000 --password 'your-password'
python -m loomy2api accounts              # check sessions + quota

python -m loomy2api serve                 # http://127.0.0.1:17890
```

Point any OpenAI-compatible client at it:

```bash
curl http://127.0.0.1:17890/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-v4-flash-0731",
       "messages": [{"role": "user", "content": "hello"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:17890/v1", api_key="not-needed")
print(client.chat.completions.create(
    model="deepseek-v4-flash-0731",            # see GET /v1/models
    messages=[{"role": "user", "content": "hi"}],
).choices[0].message.content)
```

Claude Code / any Anthropic client:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:17890
export ANTHROPIC_API_KEY=not-needed
```

> The base URL for the Anthropic dialect is the bare host — the gateway serves
> `/v1/messages` at the root, just like the real API.

## Multi-account pool

`accounts.json` (gitignored) looks like this:

```json
{
  "accounts": [
    { "name": "main",   "loginid": "13800000000", "password": "…", "enabled": true },
    { "name": "backup", "loginid": "13900000000", "password": "…", "enabled": true },
    { "name": "shared", "session": "<32-char session>", "userid": "…", "expireAt": 0 }
  ]
}
```

* **Password accounts** renew themselves. A background thread checks every
  `quota_refresh_minutes` and re-logs in whenever a session has less than
  `session_renew_before_days` (default 3) left — a pool runs unattended.
* **Session-only accounts** work too (handy for sharing accounts with someone
  else), but they expire after 14 days and need `loomy2api login` again.
* **Client import** — if the Loomy desktop app is installed and logged in, its
  session is picked up automatically as an extra account
  (`sessions_from_client: true`).

Routing:

| strategy | behaviour |
|---|---|
| `balance` (default) | use the account with the most available points |
| `round_robin` | cycle in order |
| `lru` | least recently used first |

On `401/403` the session is dropped, on `402`/quota exhaustion the account is
cooled down for `cooldown_seconds`, and the request is retried on the next
account (`max_retries`). Everything is visible in `GET /v1/admin/accounts`.

## Web control panel

Open <http://127.0.0.1:17890/panel> (the bare host also serves it):

* every account with its points (`available = balance + daily grant`), session
  days left, request/points counters, cooldown state and last error
* add an account (phone + password → logged in immediately, or paste a session)
* force renew, disable/enable, delete
* **rebind device identity** — see below
* live tail of the gateway log, auto-refresh

If `api_keys` is set, `/` and `/panel` are a login form until you sign in with
that key. The browser keeps an HttpOnly session cookie — not the key, and not
in `localStorage`. That cookie unlocks `/api/panel/*` only. Model clients still
send `Authorization: Bearer` or `x-api-key`; a panel session is rejected on
`/v1/*`. With no `api_keys`, the panel stays open for localhost use.

## Account identity (device fingerprint)

At first login each account is given its own **device identity** and that
identity is bound to the account in `accounts.json`:

```json
"identity": {
  "devid": "web-0ca44246df704952",
  "ua": "Loomy|Desktop|Electron|macOS",
  "modelid": "Web", "version": "1.0.0",
  "campus_device_id": "loomy-campus-0cbce23d-6ec2-4ee5-9591-f43408d23896",
  "created_at": 1790471588
}
```

Every later login and every request reuses it, so one account always looks like
one consistent device instead of every account announcing `devid: web`.

Be clear about what this is and is not: the protocol only carries four
device-ish fields (`devid`, `ua`, `modelid`/`version` plus a per-request random
`traceid`), and the promotions device id is only sent with
`/points/activation` and `/points/first-login` bodies — never with chat
requests. Binding a distinct identity keeps accounts separated in those fields;
it does **not** change your network origin (IP), which is what most risk
control actually keys on. Treat it as account isolation, not as a ban
guarantee.

`identity_mode` in `config.json`:

| value | behaviour |
|---|---|
| `per_account` (default) | distinct `devid` (`web-<16 hex>`) and campus device id per account; `ua`/`modelid`/`version` stay the real client's values |
| `client` | mirror the shipped client byte-for-byte (`devid: web`) |

Rebind from the panel ("换标识") or with
`POST /api/panel/accounts/identity {"name": "...", "regenerate": true}`, which
generates a fresh identity **and** logs the account in again under it.

## Model catalogue

Whatever the upstream returns from `/models` is exposed as-is — model ids are
used **verbatim**, there is no alias/mapping table (only a `provider/` prefix is
stripped). Verified behaviour to be aware of: the upstream silently falls back
to `deepseek-v4-flash-0731` when it does not recognise an id — `gpt-4o-mini`
answers HTTP 200 as `deepseek-v4-flash-0731` — so always read the `model` field
of the response. Typical catalogue (the multiplier is the points cost factor —
`spark-x` at `x0.1` is by far the cheapest):

| id | multiplier | notes |
|---|---|---|
| `spark-x` | x0.1 | Spark X2.5, text + reasoning |
| `GLM-5.3-Flash` | x0.8 | vision / video / tools |
| `qwen3.8-flash` | x0.8 | vision / video / tools |
| `deepseek-v4-flash-0731` | x3.0 | 1M context, tools |
| `mimo-v2.5` | x3.3 | audio / image / video |
| `MiniMax-M3` | x4.0 | vision / video |
| `Kimi-k2.6` | x6.5 | vision / video |
| `qwen-3.8-max` | x12.0 | strongest, priciest |
| `Hy-Image-3.5-preview`, `doubao-seedream-5-lite`, `qwen-image-3.0-pro` | — | image generation |

## Configuration

`config.json` (all keys optional) — see `config.example.json` for comments.
Environment variables override it:

| env | meaning |
|---|---|
| `LOOMY_HOST` / `LOOMY_PORT` | listen address |
| `LOOMY_UPSTREAM` | model gateway base URL |
| `LOOMY_ACCOUNT_BASE` | account service base URL |
| `LOOMY_AK_ID` / `LOOMY_AK_SECRET` | override the client-shipped signing keys |
| `LOOMY_API_KEYS` | comma-separated gateway keys (`[]` = no auth) |
| `LOOMY_DEFAULT_MODEL` | fallback model |
| `LOOMY_ACCOUNTS_FILE` / `LOOMY_LOG_DIR` | state locations |
| `LOOMY_PROXY` | e.g. `http://127.0.0.1:7877` (default: direct) |
| `LOOMY_STRATEGY` | `balance` / `round_robin` / `lru` |

### Protecting the gateway

```json
{ "api_keys": ["sk-local-whatever"] }
```

Clients then send `Authorization: Bearer sk-local-whatever` or
`x-api-key: sk-local-whatever`. The same key is what you type into the panel
login page. `/health` stays public for probes.

## Docker

```bash
docker build -t loomy2api .
docker run -d --name loomy2api -p 17890:17890 -v $PWD/data:/data loomy2api
# put accounts.json in ./data (mounted as /data/accounts.json)
```

or with Compose:

```bash
mkdir -p data && cp accounts.example.json data/accounts.json
# edit data/accounts.json, then:
docker compose up -d --build
docker compose logs -f
```

## Deployment guide

### 0. Before anything: give the account a password

The desktop client has no "set password" screen — set it once in the **iFlytek
account centre** (web or mobile), then this project can log in unattended
forever. No password? Use the SMS path instead:

```bash
python -m loomy2api sms 13800000000          # sends a code
python -m loomy2api verify main 13800000000 <code> <msgid>
```

### 1. Install (Windows / Linux / macOS)

```bash
git clone https://github.com/Patrick130306/loomy2api.git
cd loomy2api

cp config.example.json config.json      # optional — defaults work
cp accounts.example.json accounts.json  # your accounts go here
chmod 600 accounts.json config.json     # it holds passwords, keep it private

python -m loomy2api add main --phone 13800000000 --password 'your-password'
python -m loomy2api accounts            # verify: quota + session days left

python -m loomy2api serve               # http://127.0.0.1:17890
```

* Python **3.9+**, **no dependencies** (pure standard library).
* `--port 9000` overrides the port; `-c /path/config.json` picks another config.
* Panel: <http://127.0.0.1:17890/panel>

### 2. Keep it running — Linux (systemd)

```ini
# /etc/systemd/system/loomy2api.service
[Unit]
Description=loomy2api gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=loomy
WorkingDirectory=/opt/loomy2api
ExecStart=/usr/bin/python3 -m loomy2api serve
Restart=always
RestartSec=5
UMask=0077                     # accounts.json holds plaintext passwords
Environment=LOOMY_HOST=127.0.0.1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now loomy2api
journalctl -u loomy2api -f          # follow the log
```

### 3. Keep it running — Windows

Task Scheduler (built in, no extra software):

```powershell
# start.cmd
@echo off
cd /d D:\loomy2api
python -m loomy2api serve >> logs\console.log 2>&1

schtasks /create /tn loomy2api /sc onstart /rl highest /tr "D:\loomy2api\start.cmd" /f
schtasks /run /tn loomy2api
```

or NSSM (installs it as a real Windows service):

```powershell
nssm install loomy2api "C:\Python312\python.exe" "-m loomy2api serve"
nssm set loomy2api AppDirectory D:\loomy2api
nssm set loomy2api AppStdout D:\loomy2api\logs\service.log
nssm start loomy2api
```

### 4. Docker / Compose

```bash
mkdir -p data && cp accounts.example.json data/accounts.json   # then edit it
docker compose up -d --build
docker compose logs -f
```

The image is `python:3.12-slim` plus your source — nothing else to install.
State (`accounts.json`, `logs/`) lives in `./data`, so upgrades are
`git pull && docker compose up -d --build`.

### 5. Exposing it (optional, read this)

By default the gateway binds `127.0.0.1` — only your machine can reach it.
To share it on your LAN or the internet:

1. **Set an API key first.** Without `api_keys`, anyone who can reach the port
   spends your account's points, and the panel has no login page:

   ```json
   { "api_keys": ["sk-something-long-and-random"] }
   ```

   Opening the site then shows a login form. The key is checked and discarded;
   it is not stored in the browser.
2. Then `LOOMY_HOST=0.0.0.0` (or `"host": "0.0.0.0"` in `config.json`).
3. Put a reverse proxy with TLS in front if it's public. Caddy example:

   ```
   api.example.com {
       reverse_proxy 127.0.0.1:17890
   }
   ```

Clients then use `https://api.example.com/v1` as the base URL.

> Prefer a VPN/Tailscale/WireGuard over public exposure. This endpoint has no
> rate limiting of its own, and the upstream account is yours to lose.

### 6. Point your clients at it

| client | setting |
|---|---|
| OpenAI SDK / LangChain | `base_url="http://127.0.0.1:17890/v1"`, any `api_key` (or the key you set) |
| Cherry Studio / LobeChat / NextChat / Open WebUI | provider "OpenAI compatible", base URL above |
| 沉浸式翻译 / bilingual reader | custom OpenAI endpoint, same base URL |
| Claude Code | `ANTHROPIC_BASE_URL=http://127.0.0.1:17890` `ANTHROPIC_API_KEY=<any>` |
| curl | see the quick start above |

Model names: use the ids from `/v1/models` (`deepseek-v4-flash-0731`,
`spark-x`, `Kimi-k2.6`, …). The gateway does not remap anything; the upstream
falls back to its default model for an unknown id, so check the `model` field
in the response if something looks off.

### 7. Upgrading & backup

```bash
git pull && sudo systemctl restart loomy2api     # or: docker compose up -d --build
```

Back up `accounts.json` — it holds the accounts, their sessions and (if you
added them) the passwords, plus the bound device identity of each account.
`config.json` holds your settings. Both are gitignored.

### 8. Health checks & logs

```bash
curl -s http://127.0.0.1:17890/health | python -m json.tool   # status + panel URL
python -m loomy2api accounts                                  # quota per account
tail -f logs/gateway.log                                      # model/tokens/points per call
```

The panel (<http://127.0.0.1:17890/panel>) shows the same live, with per-account
quota and buttons for renew / rebind identity / disable / delete.

## CLI

```
loomy2api serve                 start the gateway
loomy2api accounts              pool status: quota, session days left, cooldown
loomy2api add <name> --phone … --password …
loomy2api remove <name>
loomy2api login [name …]        log in / force-refresh sessions
loomy2api sms <phone>           send an SMS code (SMS login path)
loomy2api verify <name> <phone> <code> <msgid>
loomy2api identity <name> [--rebind]   show / rebind the device identity
loomy2api models                list the upstream catalogue
loomy2api quota                 per-account points
loomy2api chat "prompt"         one-shot request through a pooled account
```

## How it works

The Loomy desktop client is an Electron app whose **main-process sources ship
unencrypted** in `resources/app.asar.unpacked/electron/`, and whose provider is
configured with `useSessionAuth: true` — i.e. it stores no API key and simply
sends the login session as the bearer token. The iFlytek account service is a
plain signed HTTP API (HMAC-SHA1), and its password login turns out to be fully
scriptable because the `rcode` handed out with the RSA key is a server nonce,
not a CAPTCHA.

So this project is: log in over the account service → hold the 14-day session →
forward OpenAI/Anthropic requests to the model gateway with that session.

The complete write-up (endpoints, signing string, error fingerprints, quota
ledger, client config encryption) is in **[docs/PROTOCOL.md](docs/PROTOCOL.md)**.

## Tests

```bash
python -m unittest discover -s tests -t . -v
```

Hermetic: a local fake upstream stands in for both the account service and the
model gateway, so the suite never touches a real account and burns no points.
CI runs it on Linux and Windows across Python 3.9/3.11/3.13.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `账号池里没有可用账号` | `accounts.json` empty, all accounts disabled, or all in cooldown |
| `... session 不可用且没有账号密码` | add `loginid` + `password`, or run `loomy2api sms` + `loomy2api verify` |
| `getPuKey ... HMAC signature does not match` | your `access_key_secret` is wrong/truncated — leave it empty to use the built-in client constants |
| upstream hangs until timeout | something stripped the `traceparent` header |
| `402` / point exhaustion | top the account up, or add another account to the pool |
| two instances fighting over one port (Windows) | Windows allows duplicate `SO_REUSEADDR` binds — check `netstat -ano \| findstr 17890` and kill the stale PID |

## Notes & risks

* Every request is billed to the account's points (`balance` + a daily grant),
  exactly like the official client. Watch `GET /v1/points`.
* The upstream account system is real: don't hammer the login endpoint, and
  avoid scripts that retry logins aggressively.
* Use it on accounts you own. Sharing one account across many people increases
  the chance of rate limiting or a ban.

## Disclaimer

This project is unaffiliated with iFlytek / 科大讯飞. It exists for
interoperability and personal use with your own account. The signing constants
in `loomy2api/constants.py` are the ones the official client ships in every
installation; they are used only to talk to the account service. Do not use
this project to abuse, resell or overload the upstream service.

## License

[MIT](LICENSE)
