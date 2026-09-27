# Reversed protocol reference

Everything here was derived from the official Loomy desktop client (Electron,
version `0.9.38`) — its main-process sources ship **unencrypted** in
`resources/app.asar.unpacked/electron/`, and its `resources/.env.prod` is
obfuscated with a passphrase that is hard-coded in
`electron/utils/env-file-crypto.js` of the same package.

This document is for interoperability and study. It is not affiliated with
iFlytek.

---

## 1. Components

| Component | Base URL | Purpose |
|---|---|---|
| Model gateway | `https://loomyad.xunfei.cn/api/v1` | OpenAI-compatible model access (`/chat/completions`, `/models`, `/images/generations`, `/embeddings`, `/search/tencent`, `/points/records`) |
| Account service | `https://account.xfinfr.com` | Login, session issuing, userinfo |
| Trade service | `https://trade.xfinfr.com` | Payment (not used here) |
| Nexus gateway | `wss://dispatch-nexus.xfinfr.com/ws/loomy` | Remote-control channel (not used here) |

Client data lives under `C:\Users\Public\Loomy\<sha256(username)[:12]>\` with
`userData/`, `config/`, `opencode/`, `state/`, `share/`. The login session is a
plain-text field in `userData/auth-session.json`.

## 2. Authentication model

The client's `opencode.json` declares:

```json
"provider": { "imodel": { "options": {
    "baseURL": "https://loomyad.xunfei.cn/api/v1",
    "useSessionAuth": true } } }
```

`useSessionAuth: true` means **no API key is stored** — the login session is
sent as the bearer token. That single fact is why a gateway can be built
without the client: obtain the session, and the upstream accepts you.

Required headers on every model request:

```
Authorization: Bearer <session>
token: <session>                      # both spellings are accepted/sent
traceparent: 00-<32 hex>-<16 hex>-01  # without it the upstream hangs until timeout
loomy-version: 0.9.38                 # presence-checked only
Content-Type: application/json
```

Optional attribution headers (used for points bookkeeping server-side):
`ChatId`, `MsgId`, `TurnId`, `X-Loomy-Request-Purpose`
(one of 17 values: `chat.message`, `chat.title`, `kb.vision`, `kb.embed`,
`image.generate`, `memory.extract`, `search.web`, …).

### Session lifetime

`14 * 24 * 3600` seconds, requested explicitly at login. There is **no refresh
token**: when it expires you must log in again. Password login is fully
automatic, so a stored password is enough for unattended operation.

## 3. Account service: signing

`stringToSign` (9 lines, the last two are empty strings when no `x-*` headers
are sent — they must not be omitted):

```
METHOD                                   # uppercase, e.g. POST
ESCAPED_PATH                             # per-segment RFC3986 (encodeURIComponent + !'()*)
ESCAPED_QUERY                            # "" when there is no query
Content-MD5                              # base64(md5(raw body string)), "" when no body
Content-Type                             # application/json
Date                                     # e.g. Sat, 26 Sep 2026 12:00:00 GMT
Nonce                                    # a UUID
SignedHeaders                            # sorted lowercase x-* names joined by ";" ("" here)
CanonicalizedHeaders                     # sorted "k:v\n" lines, trailing newline stripped
```

```
signature     = base64(HMAC-SHA1(accessKeySecret, stringToSign))
Authorization = "account <accessKeyId>:<signature>"
```

Access key pair: `2thryby66wxi53sk` / (32-char secret) — shipped in the
client's `.env.prod`. Requests only serve to obtain a session; the model
gateway itself does not use this signature.

Error fingerprints:

| Response | Meaning |
|---|---|
| `HMAC signature does not match` | key known, but the canonical string is wrong |
| `HMAC signature cannot be verified` | this access key is not recognised (wrong environment/key) |

## 4. Account service: login flows

Common body envelope: `{"base": {...}, "param": {...}}` where

```json
{"base": {"appid": "GM3LOOMY", "modelid": "Web", "version": "1.0.0",
          "devid": "web", "ua": "Loomy|Desktop|Electron|macOS",
          "traceid": "<32 hex>"}}
```

### 4.1 Password login (fully automatable)

1. `POST /login/account/getPuKey` → `{pukey: "<DER RSA-1024 public key, base64>",
   rcode: "<32 hex>"}`.
   **`rcode` is a server-issued nonce, not a CAPTCHA** — this is what makes
   password login scriptable.
2. `POST /login/account/byPwd` with
   `{loginid, password: base64(RSA_PKCS1v15(plaintext)), rcode, type: 1, expire: 1209600}`
   → `{session, userid, phone}`

### 4.2 SMS login

1. `POST /login/phone/sendMsgCode` `{ccode: "86", phone, expire: 300}` → `{msgid}`
2. `POST /login/phone/checkCode` `{ccode, phone, mcode, msgid, expire: 1209600}`
   → `{session, userid, phone}`

### 4.3 Other endpoints observed

`/login/account/logout`, `/userinfo/query/baseInfo`,
`/userinfo/phone/sendMsgCode`, `/userinfo/thirdAccount/{bind,unbind}`,
`/userinfo/query/thirdInfo`, `/userinfo/bind/mergeAccount`,
`/userinfo/audit/update`, `/register/phone/submit`,
`/login/thirdAccount/{bind/auth,bind/sendMsg,bind/checkCode,bind/skip}` (WeChat).
There is **no password set/change endpoint** — set the password in the
iFlytek account centre once, then reuse it forever.

## 5. Model gateway surface

| Endpoint | Notes |
|---|---|
| `GET /models` | full catalogue: `id`, `name` (contains the points multiplier), `context_length`, `max_output_tokens`, `capabilities{reasoning,vision,function_calling,streaming}`, `reasoning_efforts` |
| `POST /chat/completions` | OpenAI-compatible. `stream: true` supported; the final SSE chunk carries `usage`, including `points_consumed` |
| `POST /images/generations` | text-to-image and image-to-image for the image models |
| `POST /embeddings` | `{model, input: [..]}` |
| `POST /search/tencent` | built-in web search (`loomy_websearch`) |
| `GET /points/records` | `data.{balance, dailyBalance, availableBalance, list[]}` — ledger with per-call `pointsActual`, `modelName`, `dailyCycleDate` |
| `GET /team-points/balance` | team points (requires being switched to a team) |
| `POST /points/first-login`, `GET/POST /points/activation` | onboarding bonus / invitation codes |

### 5.1 Device identity fields (verified)

The only device-scoped values in the protocol are:

| field | where | client behaviour |
|---|---|---|
| `devid` | account-service request envelope (`base`) | constant string `web` |
| `ua` | same envelope | constant `Loomy\|Desktop\|Electron\|macOS` |
| `modelid` / `version` | same envelope | `Web` / `1.0.0` |
| `traceid` | same envelope | fresh random 32-hex per request |
| `deviceId` (promotions) | body of `/points/activation` and `/points/first-login` **only** | `loomy-campus-<uuid>` or `loomy-campus-fp-<sha256(machineId)>` |

Note what is *not* there: `/chat/completions` carries no device field at all,
and the promotions device id never leaves the two points endpoints. So
"per-account device identity" can only ever change these four envelope values —
it does not change the network origin, which is what most risk control keys on.

Quota model: `availableBalance = balance (permanent) + dailyBalance (free daily
grant, consumed first, resets each day)`. Model multipliers (`x0.1` … `x12.0`)
scale how many points a request costs; `spark-x` at `x0.1` is the cheapest.

Reasoning models expose `reasoning_content` alongside `content`; requests may
carry `enable_thinking: false` / `chat_template_kwargs.enable_thinking` and the
`/no_think` message prefix (the client uses all three to suppress reasoning on
short prompts).

## 6. Client-side configuration encryption (informational)

`resources/.env.prod` is `LOOMYENC1:` + `base64(salt16 ‖ iv12 ‖ tag16 ‖
ciphertext)`, AES-256-GCM with `key = scrypt(passphrase, salt, 32)`. The
passphrase is hard-coded in `electron/utils/env-file-crypto.js`. The client's
own comment states plainly that this is obfuscation, not key secrecy:

> 解密口令必须随客户端一起分发，本质是「混淆」而非真正的密钥保密……真正的机密应放到服务端代理。

Two practical gotchas when decrypting in Python: the ciphertext's base64 is
often missing padding (Node tolerates it, `base64.b64decode` does not), and the
standard library has no AES (install `cryptography`, or shell out to any Node
runtime).
