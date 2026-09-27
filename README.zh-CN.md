# loomy2api

把 **Loomy**（科大讯飞桌面 AI 助理）账号的模型额度，变成一个自托管的
**OpenAI / Anthropic 双协议兼容 API**，并且**支持多账号池**——轮换、故障转移、登录态自动续期。

* **零依赖** —— 纯 Python 标准库（3.9+），不需要装任何第三方包。
* **不需要装桌面客户端** —— 网关自己用「手机号 + 密码」在服务端登录，并自动续期 14 天的登录态。
* **多账号** —— 想加几个账号就加几个；按余额 / 轮询 / 最近最少使用三种策略路由，
  登录态失效自动冷却并换下一个账号重试。
* **两种协议都支持** —— OpenAI 客户端走 `/v1/chat/completions`，Claude Code 等走 `/v1/messages`
  （流式、工具调用、思考链都已翻译）。

[English README](README.md) · [逆向协议文档](docs/PROTOCOL.md)

---

## 功能一览

| | |
|---|---|
| OpenAI 协议 | `POST /v1/chat/completions`（流式/非流式）、`GET /v1/models`、`POST /v1/embeddings`、`POST /v1/images/generations` |
| Anthropic 协议 | `POST /v1/messages`（流式/非流式），支持 thinking 块、tool_use / tool_result |
| 网页面板 | `http://127.0.0.1:17890/panel` —— 看各账号积分、添加/删除/禁用、强制续期、重绑设备标识、实时日志 |
| 多账号 | `balance`（默认，按可用积分）· `round_robin`（轮询）· `lru`（最久未用）三种策略；账号级冷却；失败自动换号重试；额度跟踪 |
| 设备标识 | 每个账号在首次登录时随机生成一套独立设备标识并绑定，之后每次续期都复用它 |
| 登录态 | 密码登录、短信登录、导入桌面客户端登录态、到期前自动重登 |
| 运维 | `/health`、`/v1/points`、`/v1/admin/accounts`，请求日志记录模型 / tokens / 扣分 / 耗时 |
| 安全 | 可选给网关自己加 API Key；密码等敏感文件默认不进 git |

## 环境要求

* Python **3.9+**（已在 Windows / Linux 的 3.9、3.11、3.13 上测过）
* 一个 Loomy 账号（手机号 + 在讯飞账号中心设过的密码）
* 能访问 `account.xfinfr.com` 与 `loomyad.xunfei.cn`

> **密码怎么设**：客户端里没有设密码的入口，需要去**讯飞账号中心**（网页或手机端）设置一次，
> 之后本项目的密码登录就能长期自动续期。

## 快速开始

```bash
git clone https://github.com/<you>/loomy2api.git
cd loomy2api

cp config.example.json config.json        # 可选，默认值即可用
cp accounts.example.json accounts.json    # 把你的账号写进去

# 添加账号并登录（登录态会写回 accounts.json）
python -m loomy2api add main --phone 13800000000 --password '你的密码'
python -m loomy2api accounts              # 看登录态剩余天数和额度

python -m loomy2api serve                 # http://127.0.0.1:17890
```

接任意 OpenAI 兼容客户端：

```bash
curl http://127.0.0.1:17890/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "deepseek-v4-flash-0731",
       "messages": [{"role": "user", "content": "你好"}]}'
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:17890/v1", api_key="随便填")
print(client.chat.completions.create(
    model="gpt-4o-mini",                      # 别名可配置
    messages=[{"role": "user", "content": "你好"}],
).choices[0].message.content)
```

Claude Code / 任意 Anthropic 客户端：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:17890
export ANTHROPIC_API_KEY=随便填
```

## 多账号池

`accounts.json`（已在 .gitignore 里）长这样：

```json
{
  "accounts": [
    { "name": "主号",   "loginid": "13800000000", "password": "…", "enabled": true },
    { "name": "备用",   "loginid": "13900000000", "password": "…", "enabled": true },
    { "name": "朋友共享", "session": "<32 位 session>", "userid": "…", "expireAt": 0 }
  ]
}
```

* **有密码的账号**会自己续期：后台线程按 `quota_refresh_minutes` 巡检，
  剩余天数低于 `session_renew_before_days`（默认 3 天）就自动重登——整池可以无人值守。
* **只有 session 的账号**也能用（适合别人给你的号），但 14 天到期后要重新
  `loomy2api login` 或短信登录。
* **客户端导入**：本机装有 Loomy 桌面端且已登录时，它的登录态会被自动当作一个额外账号
  （`sessions_from_client: true`）。

路由策略：

| 策略 | 行为 |
|---|---|
| `balance`（默认） | 用可用积分最多的账号 |
| `round_robin` | 按顺序轮换 |
| `lru` | 优先用最久没用的 |

遇到 `401/403` 会丢弃该账号的登录态；遇到 `402` / 额度耗尽会把账号冷却
`cooldown_seconds` 秒，然后**自动换下一个账号重试**（`max_retries`）。
当前每个账号的状态在 `GET /v1/admin/accounts` 里一目了然。

## 网页面板

浏览器打开 <http://127.0.0.1:17890/panel>（直接开根路径也是这个页面）：

* 每个账号的积分（可用 = 余额 + 每日额度）、登录态剩余天数、请求数/已扣分、冷却状态、最近错误
* 添加账号（手机号 + 密码 → 立刻登录；也可以只贴一个 session）
* 强制续期、禁用/启用、删除
* **重绑设备标识**（见下一节）
* 实时日志尾巴，可自动刷新

如果配置了 `api_keys`，面板的接口就需要这个 Key（页面本身保持公开，方便你填 Key），
Key 存在浏览器 localStorage 里。

## 账号设备标识（指纹）

每个账号在**首次登录时**随机生成一套独立设备标识，并绑定写进 `accounts.json`：

```json
"identity": {
  "devid": "web-0ca44246df704952",
  "ua": "Loomy|Desktop|Electron|macOS",
  "modelid": "Web", "version": "1.0.0",
  "campus_device_id": "loomy-campus-0cbce23d-6ec2-4ee5-9591-f43408d23896",
  "created_at": 1790471588
}
```

之后每次续期、每次请求都用同一套，所以一个账号始终表现为同一台设备，
而不是所有账号都对外宣布 `devid: web`。

**说清楚它是什么、不是什么**：协议里真正涉及"设备"的字段只有四个
（`devid`、`ua`、`modelid`/`version`，加一个每请求随机的 `traceid`），
而校园推广用的设备号只出现在 `/points/activation` 和 `/points/first-login` 的请求体里，
**不会随对话请求发出**。绑独立标识能让账号在这些字段上互不雷同，
但它**不改变出口 IP**——而 IP 才是大多数风控真正看的东西。
所以请把它当"账号隔离"，不要当"防封保证"。

`config.json` 里的 `identity_mode`：

| 值 | 行为 |
|---|---|
| `per_account`（默认） | 每个账号独立 `devid`（`web-<16 位 hex>`）和校园设备号；`ua`/`modelid`/`version` 仍用真实客户端的值 |
| `client` | 完全照抄官方客户端（`devid: web`） |

重绑方式：面板上的"换标识"，或
`POST /api/panel/accounts/identity {"name": "...", "regenerate": true}` ——
它会生成一套新标识**并**用新标识重新登录一次。

## 模型清单

上游 `/models` 返回什么就暴露什么，另外加上你配置的别名。典型清单
（倍率就是扣分系数，`spark-x` 的 x0.1 最省）：

| id | 倍率 | 说明 |
|---|---|---|
| `spark-x` | x0.1 | Spark X2.5，文本 + 推理 |
| `GLM-5.3-Flash` | x0.8 | 视觉 / 视频 / 工具 |
| `qwen3.8-flash` | x0.8 | 视觉 / 视频 / 工具 |
| `deepseek-v4-flash-0731` | x3.0 | 1M 上下文，支持工具 |
| `mimo-v2.5` | x3.3 | 音频 / 图像 / 视频 |
| `MiniMax-M3` | x4.0 | 视觉 / 视频 |
| `Kimi-k2.6` | x6.5 | 视觉 / 视频 |
| `qwen-3.8-max` | x12.0 | 最强也最贵 |
| `Hy-Image-3.5-preview`、`doubao-seedream-5-lite`、`qwen-image-3.0-pro` | — | 生图 |

## 配置

`config.json` 所有键都可省略（默认值见 `loomy2api/config.py`，带注释的样例见
`config.example.json`）。环境变量优先级更高：

| 环境变量 | 作用 |
|---|---|
| `LOOMY_HOST` / `LOOMY_PORT` | 监听地址 |
| `LOOMY_UPSTREAM` | 模型网关地址 |
| `LOOMY_ACCOUNT_BASE` | 账号服务地址 |
| `LOOMY_AK_ID` / `LOOMY_AK_SECRET` | 覆盖内置的客户端签名密钥 |
| `LOOMY_API_KEYS` | 网关自己的 Key，逗号分隔（`[]` = 不鉴权） |
| `LOOMY_DEFAULT_MODEL` | 兜底模型 |
| `LOOMY_ACCOUNTS_FILE` / `LOOMY_LOG_DIR` | 状态文件位置 |
| `LOOMY_PROXY` | 如 `http://127.0.0.1:7877`（默认直连） |
| `LOOMY_STRATEGY` | `balance` / `round_robin` / `lru` |

### 给网关加把锁

```json
{ "api_keys": ["sk-local-whatever"] }
```

客户端带 `Authorization: Bearer sk-local-whatever` 或 `x-api-key: sk-local-whatever`。
`/health` 始终公开，方便探活。

## Docker

```bash
docker build -t loomy2api .
docker run -d --name loomy2api -p 17890:17890 -v $PWD/data:/data loomy2api
# 把 accounts.json 放进 ./data（容器内即 /data/accounts.json）
```

## 命令行

```
loomy2api serve                 启动网关
loomy2api accounts              账号池状态：额度、剩余天数、冷却
loomy2api add <名字> --phone … --password …
loomy2api remove <名字>
loomy2api login [名字…]          登录 / 强制续期
loomy2api sms <手机号>           发短信验证码（短信登录第一步）
loomy2api verify <名字> <手机号> <验证码> <msgid>
loomy2api identity <名字> [--rebind]   查看 / 重绑设备标识
loomy2api models                列出上游模型
loomy2api quota                 各账号积分
loomy2api chat "问题"            走账号池发一次请求自检
```

## 原理

Loomy 客户端是 Electron 应用，它的**主进程源码是明文**的
（`resources/app.asar.unpacked/electron/`，773 个 JS 文件），而 provider 配置里写了
`useSessionAuth: true` —— 也就是不存 apiKey，直接把登录态当 Bearer 用。
讯飞账号服务是一套标准 HTTP + HMAC-SHA1 签名的接口，而且密码登录天生可脚本化：
`getPuKey` 随 RSA 公钥一起下发的 `rcode` 是服务端 nonce，**不是人机验证码**。

所以本项目做的就是：在账号服务上登录 → 持有 14 天登录态 → 用它把 OpenAI / Anthropic
请求转发到模型网关。

完整逆向记录（端点、签名串、错误指纹、积分账本、客户端配置加密）见
**[docs/PROTOCOL.md](docs/PROTOCOL.md)**。

## 测试

```bash
python -m unittest discover -s tests -t . -v
```

全部离线：本地假上游同时扮演账号服务和模型网关，跑测试不会碰真实账号、不消耗积分。
CI 在 Linux 和 Windows 上跑 Python 3.9 / 3.11 / 3.13。

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `账号池里没有可用账号` | `accounts.json` 为空、账号被禁用，或全部处于冷却 |
| `... session 不可用且没有账号密码` | 补 `loginid` + `password`，或走 `loomy2api sms` + `loomy2api verify` |
| `getPuKey ... HMAC signature does not match` | `access_key_secret` 抄错/被截断——留空即可用内置的客户端常量 |
| 上游一直挂到超时 | 有人把 `traceparent` 头去掉了 |
| `402` / 积分耗尽 | 充值，或往池子里再加一个账号 |
| Windows 上两个实例抢同一端口 | Windows 允许重复 `SO_REUSEADDR` 绑定——`netstat -ano \| findstr 17890` 找出残留 PID 杀掉 |

## 注意事项

* 每次调用都扣账号积分（长期余额 + 每日免费额度），和官方客户端完全一样，
  用 `GET /v1/points` 盯着。
* 上游账号体系是真的：别猛怼登录接口，也不要做激进的重试脚本。
* 请用你自己的账号。把同一个号共享给很多人用，会明显提高被限流甚至封号的概率。

## 免责声明

本项目与科大讯飞无任何从属关系，仅为了与自己账号的互操作性、供个人使用。
`loomy2api/constants.py` 里的签名常量是官方客户端每个安装包里都会带的客户端常量，
只用于和账号服务通信。请勿用它滥用、转售或压垮上游服务。

## 协议

[MIT](LICENSE)
