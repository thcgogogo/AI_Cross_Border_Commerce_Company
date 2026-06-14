# Launch checklist — everything YOU need to fill in

The app runs locally with **zero** of this filled in (console SMS codes, mock
cashier, no emails). Each section below turns one real-world capability on.
Do them in order; stop whenever you have enough for what you're doing today.

## How filling things in works (read once)

Almost everything goes in **one file: `.env`** in the project folder.

1. Open it:  `open -e .env`
2. Add one line per setting, exactly like `NAME=value`
   - **no spaces** around the `=`, **no quotes** around the value
   - a line starting with `#` is a comment (ignored)
3. Save (Cmd+S), then **stop the app (Control+C) and run `python app.py` again.**
   Settings are only read at startup (a few, like ADMIN_EMAILS, apply instantly,
   but restarting always works).

Every setting named below also has a commented example in `.env.example` —
you can copy the line from there, delete the leading `# `, and fill in the value.

---

## 1. Make yourself the admin (2 minutes, do this first)

| Put in `.env` | What it is | Where to get it |
|---|---|---|
| `ADMIN_EMAILS=you@example.com` | The login email(s) allowed into every `/admin` page (ops dashboard, orders/refunds, fapiao, reconciliation, breaker, feedback). Separate several with commas. | The email you signed up with in the app. |

**Check it worked:** log in with that email, open `http://localhost:8000/admin` —
you should see the 运营看板 instead of 403.

## 2. Your support contacts (the 联系支持 button)

| Put in `.env` | What it is | Where to get it |
|---|---|---|
| `SUPPORT_EMAIL=support@your-domain.com` | The "邮件：" link in the 联系支持 popup. If unset, it falls back to your first ADMIN_EMAILS entry. | Any mailbox you actually read. |
| `SUPPORT_WECHAT_QR_URL=https://…/qr.png` | The 企业微信 support-group QR image shown in the popup (replaces the "待配置" placeholder). | In 企业微信: create a group → 群二维码 → save the image, then host it anywhere public — OR put the file in the `frontend/` folder (e.g. `frontend/support-qr.png`) and set `SUPPORT_WECHAT_QR_URL=/support-qr.png`. |

**Check:** click the floating 💬 联系支持 button — your QR and email appear.
(Messages customers type there are always saved; you see the latest at the
bottom of `/admin`.)

## 3. Your product identity

| Where | What to change | How |
|---|---|---|
| `.env` | `PRODUCT_DOMAIN=your-domain.com` — printed in the footer of every generated PDF report. | Your website domain. |
| Many files (optional) | The product name **统一智能体中转站** appears in the website header, sign-up page, and the closing line of every email. | One command replaces it everywhere (put your name where it says 你的产品名):<br>`grep -rl '统一智能体中转站' app.py frontend agent_team docs CHANGELOG.md \| xargs sed -i '' 's/统一智能体中转站/你的产品名/g'`<br>Then restart the app. |
| In the app (per account) | 企业大脑 → 品牌: your **logo** (PNG/JPG ≤1MB) + **brand color** — these brand the PDF reports; 公司档案 holds your company name/industry. | Log in → Menu → 企业大脑. The onboarding wizard already filled 公司档案 if you completed it. |

## 4. Sending real email (needed for report delivery + all lifecycle mail)

Without this, scheduled-report email, welcome/day-3/receipt emails, renewal
notices, fapiao notifications, and admin alert emails are silently skipped.

| Put in `.env` | Example | Where to get it |
|---|---|---|
| `SMTP_HOST=` | `smtp.exmail.qq.com` | Your email provider's SMTP address (腾讯企业邮: smtp.exmail.qq.com · 阿里企业邮箱: smtp.qiye.aliyun.com · QQ个人: smtp.qq.com). |
| `SMTP_PORT=` | `465` | From the same help page. 465 → also set `SMTP_SSL=1`; 587 → also set `SMTP_STARTTLS=1`. |
| `SMTP_USER=` | `no-reply@your-domain.com` | The mailbox you'll send from. |
| `SMTP_PASS=` | *(app password)* | NOT your login password — in the mailbox settings, generate a **客户端授权码 / app password** (usually under 设置 → 账户 → POP/SMTP). |
| `SMTP_FROM=` | `no-reply@your-domain.com` | Usually the same as SMTP_USER. |

**Check:** in the app, create a 定时任务 with email delivery and a near-future
cron, or just sign up a fresh test account — the welcome email should arrive.

## 5. Real SMS verification codes (Aliyun)

Until you set this, codes are printed in the Terminal (fine for testing).

| Put in `.env` | Where to get it (all at https://dysms.console.aliyun.com) |
|---|---|
| `ALIYUN_SMS_ACCESS_KEY_ID=` / `ALIYUN_SMS_ACCESS_KEY_SECRET=` | Aliyun console → top-right avatar → AccessKey 管理 → create an AccessKey pair (use a RAM sub-account with only SMS permission). |
| `ALIYUN_SMS_SIGN_NAME=你的签名` | 短信服务 → 国内消息 → 签名管理 → apply for a 签名 (usually your registered company/产品 name; takes ~1–2 days' review). |
| `ALIYUN_SMS_TEMPLATE_CODE=SMS_xxxxxxxx` | 模板管理 → apply for a verification-code template containing the variable `${code}`, e.g. 「您的验证码为${code}，5分钟内有效。」 Copy the `SMS_…` code it gets after approval. |

**Check:** sign up with a real phone number — the code arrives as a text instead
of in the Terminal.

## 6. Real payments (Alipay / WeChat Pay)

These need a **registered company** (营业执照) and a **public HTTPS domain** —
the providers' servers must be able to call your server back. Until then, the
built-in mock cashier handles everything locally. **The moment you fill either
provider in, the mock cashier turns itself off automatically.**

First, in `.env`: `APP_BASE_URL=https://your-domain.com` (your real public URL —
payment callbacks and email pay-links are built from it).

**Alipay** (apply at https://open.alipay.com → 网页&移动应用 → create an app,
then bind 电脑网站支付):

| Put in `.env` | Where to get it |
|---|---|
| `ALIPAY_APP_ID=` | The APPID shown on your app's overview page (a 16-digit number starting 2021…). |
| `ALIPAY_APP_PRIVATE_KEY=` | Generate a keypair with Alipay's 密钥工具 (开放平台密钥工具). It gives you an **应用私钥** (keep secret — this is the value) and an 应用公钥 (you UPLOAD that one to the console). Paste the long base64 body on one line. |
| `ALIPAY_PUBLIC_KEY=` | AFTER uploading your 应用公钥, the console shows the **支付宝公钥** — copy THAT (it verifies Alipay's signatures; it is not your own public key). |

**WeChat Pay** (apply at https://pay.weixin.qq.com → 商户平台, plus an AppID
from 公众平台/开放平台):

| Put in `.env` | Where to get it |
|---|---|
| `WECHATPAY_MCHID=` | 商户号 (10 digits) — merchant platform → 账户中心. |
| `WECHATPAY_APPID=` | The bound 公众号/小程序/开放平台 AppID (starts `wx`). |
| `WECHATPAY_API_V3_KEY=` | 账户中心 → API安全 → set the **APIv3 密钥** yourself: exactly 32 letters/digits you invent. Paste the same 32 chars here. |
| `WECHATPAY_SERIAL_NO=` | API安全 → 申请API证书 → the certificate's **序列号**. |
| `WECHATPAY_PRIVATE_KEY=` | From the downloaded certificate bundle: the contents of `apiclient_key.pem` (paste the base64 body on one line). |
| `WECHATPAY_PLATFORM_CERT=` | The **微信支付平台证书** (downloaded via their certificate tool) — the PEM body. |

**Check:** log in → dashboard → 充值 — you should land on a REAL Alipay/WeChat
cashier instead of the local mock page. Drop the providers' daily bill CSVs into
`BILLS_DIR` (set it in `.env`, e.g. `BILLS_DIR=/srv/company-agent/data/bills`)
and `/admin/reconciliation` checks them against your orders every night.

## 7. Going to a real server (production)

All explained inline in `.env.example`'s PRODUCTION block + [docs/ops.md](ops.md):

| Put in `.env` | What to type |
|---|---|
| `APP_ENV=production` | Exactly that — turns on the must-have-SECRET_KEY check + secure cookies. |
| `SECRET_KEY=` | Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` — paste the output. |
| `POSTGRES_PASSWORD=` | Any long random string (the database password used by docker compose). |
| `FERNET_KEYS=` | Generate: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — encrypts uploads + PDFs on disk. |
| `PLATFORM_HOURLY_CAP_YUAN=200` / `PLATFORM_DAILY_CAP_YUAN=2000` | Your model-spend kill-switch ceilings, in ¥ — pick numbers you could tolerate losing in a bad hour/day. |
| `ADMIN_ALERT_WEBHOOK=` | 企业微信 group → 群机器人 → 添加机器人 → copy its Webhook URL (alerts: breaker trips, reconciliation mismatches, support requests). |
| Optional | `SENTRY_DSN=` (error tracking), `BACKUP_REMOTE=` (offsite backups via rclone), `RATELIMIT_STORAGE_URI=redis://…` (only if you run several web workers), `HTTP_PORT=` (host port for nginx). |

**Never** commit `.env` or paste these values anywhere — the app refuses to boot
in production if a known secret value ever appears in a served frontend file.

## 8. Optional extras

- **Run the team on a domestic model (mainland China)** — Claude isn't reliably
  reachable from the mainland, and a foreign LLM raises 生成式AI 算法备案 compliance
  issues for a public Chinese service. Put two lines in `.env`:

  | Put in `.env` | What it does |
  |---|---|
  | `TEAM_MODEL_PRESET=domestic` | Flips the WHOLE team — every agent **and** the internal calls (metric extraction, the verification audit, the Fusion fuser) — to **DeepSeek V4** (`deepseek-v4-flash`). No Anthropic key needed. |
  | `DEEPSEEK_API_KEY=sk-…` | Your DeepSeek key (https://platform.deepseek.com) — the only model key the team then needs. |

  **Check:** restart, and the terminal prints `Team runs on the domestic preset
  (deepseek-v4-flash) — ANTHROPIC_API_KEY not required`. Generate a report; it runs
  on DeepSeek, metrics still land in 企业大脑, and the 数据来源与核验 audit still runs.
  - On a **reused** data volume, clear any stale Claude entries in
    `memory_store/model_overrides.json` (or reassign each agent in 内部工具 →
    团队与模型) — a saved per-agent choice **wins** over the preset. A fresh volume
    has none, so the preset alone governs.
  - Want more quality on planning/writing? Reassign the 首席执行官 and 报告撰写人 to
    `deepseek-v4-pro` in 团队与模型 (a one-click per-agent override).
  - DeepSeek can't make real `.pptx/.pdf` files or do live web search (Claude-only):
    the 媒体制作人 writes Markdown briefs instead — fine for the weekly-report product.
  - Prefer Qwen? Set `DASHSCOPE_API_KEY` and reassign agents to `qwen-max`/`qwen-plus`;
    for a cn-region key, edit the Qwen `base_url` in `agent_team/config.py` to
    `https://dashscope.aliyuncs.com/compatible-mode/v1`.
- **More model providers** — add any of `OPENAI_API_KEY`, `GEMINI_API_KEY`,
  `DEEPSEEK_API_KEY`, `DASHSCOPE_API_KEY`, … (see the top of `.env.example`);
  those models light up in the catalog automatically.
- **Plan prices/quotas** — edit `PLANS` in `agent_team/config.py` (基础版 ¥199 /
  专业版 ¥599 are placeholders — set your real pricing before charging anyone).
- **Help docs & changelog** — `docs/help/*.md` and `CHANGELOG.md` are plain
  markdown; edit freely, changes show on refresh (no restart).
- **Industry packs** — `templates_industry/<key>/pack.yaml`: tune required KPIs,
  benchmarks, and report structure for the verticals you serve.
