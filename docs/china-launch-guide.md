# 上线指南：Tangent AI 部署到中国大陆公网（傻瓜版 · 收费上线）

把这个 Flask SaaS（报告产品 + 公开的 Fusion 多模型对话）**部署到大陆公网、第一天就收费**，
跑在**阿里云**上的完整、可照抄的 runbook。代码侧的生产栈（Docker Compose：nginx → gunicorn×4 +
scheduler + Postgres + Redis、开机自动迁移、国产模型模式、真支付/短信、落盘加密、`/healthz`、
`/admin` 看板、备份脚本）**已经全部就绪**——真正要做的是**法律备案 + 备齐公司资料 + 跑部署**。

> **一句话心智模型**：大陆服务器上的公开网站，**ICP 备案通过前都不许对外**；而**收费**经营还要在
> ICP 备案之上加一张 **经营性ICP许可证（EDI）**。备案需要 营业执照 + 实名域名 + 大陆阿里云 ECS。
> 所以**纸面流程是最长的环节（数周–数月）**，技术部署 ~1 小时。两条线**并行推进**。

---

## 0. 关键路径（谁卡谁）

```
营业执照 ─┬─► 域名+实名 ─┐
          ├─► 云服务器(大陆ECS) ─┼─► ICP备案(1–3周) ─► 域名解析上线 ─► 公安备案(30天内)
          ├─► 对公账户 → 支付商户 ┘
          └─► 经营性ICP(EDI，1–3月+) + 算法备案 + 等保（收费 & 公开AI 两道合规）
```
**今天就并行启动这三件最慢的事**：① 营业执照（你已有）② **ICP 备案** ③ **经营性ICP(EDI)/算法备案**。

> **强烈建议的稳妥打法**：**先免费/内测上线**（保留内置 mock 收银台、不开真支付），ICP 备案一过即可
> 公测，不被 EDI 卡住；等 **EDI 批下来再开收费**。EDI / 等保 / 算法备案多数人**找代办**。

---

## PART 1 — 法律备案/证照清单（按顺序）

| # | 文件 / 备案 | 干什么用 | 在哪办 | 时长 |
|---|---|---|---|---|
| 1 | **营业执照**（统一社会信用代码） | 一切的根：备案、支付、短信签名、算法备案都要它 | 你已有 ✅ | — |
| 2 | **对公银行账户**（开户许可证） | 支付宝/微信商户结算 + 开发票的**收款账户** | 公司开户行 | 已有/数天 |
| 3 | **域名 + 域名实名认证** | 公网访问地址；实名后才能备案 | 阿里云 域名控制台 | ~数小时–1天 |
| 4 | **云服务器 ECS**（大陆 region、**≥3 个月**） | 跑服务；阿里云要"包月及以上"实例才发**备案服务号** | 阿里云 ECS | 即时 |
| 5 | **ICP 备案**（非经营性互联网信息服务备案） | **公网放行的前提**，否则云商封 80/443 | 阿里云 备案系统 | **1–20 工作日** |
| 6 | **经营性 ICP 许可证（增值电信业务经营许可证 / EDI 证）** | **收费**经营在线服务必须有 ← 你"启动即收费"的硬门槛 | 省**通信管理局** | **1–3 个月+**，常需**注册资本≥100 万**、社保、等保 |
| 7 | **公安联网备案** | 上线后 **30 天内**必办；备案号放页脚 | `beian.mps.gov.cn` | ~数天 |
| 8 | **生成式 AI 算法备案**（互联网信息服务算法备案） | **公开 AI 生成服务**（尤其 Fusion 公开聊天）需向网信办备案 | `beian.cac.gov.cn` | 数周，不确定 |
| 9 | **生成式 AI 安全评估 / 上线备案** | 有"舆论属性/社会动员能力"的生成式 AI 需安全自评估 | 属地网信办 | 与 #8 同步 |
| 10 | **网络安全等级保护备案（等保 2.0，通常二级）** | 持用户数据的在线平台普遍要求；EDI 审批也常要 | 定级→公安备案→测评机构 | 测评 ~1 个月 |
| 11 | **隐私政策 + 用户服务协议**（PIPL 合规文本） | 收手机号/上传文件/AI 处理数据，法律强制公示 | 自拟/律师，发布在站内 | 自办 |
| 12 | **软件著作权登记（软著）**（可选推荐） | 保护 IP；部分渠道/审批要用 | 中国版权保护中心 | 31 工作日 |

**注**：法规会变——**算法备案 / 安全评估 / EDI 的当下口径，务必找阿里云合规或专业代办确认。**
本仓库默认把 **Fusion 收到登录后**（`FUSION_FREE_ANON_TURNS=0`），先把公开 AI 面收窄，降低算法备案风险；
要开公开试用再在 `.env` 设 `FUSION_FREE_ANON_TURNS=3`。

---

## PART 2 — 公司资料/资产清单（动手前备齐）

**身份与公司**
- [ ] 营业执照（清晰扫描件/照片）
- [ ] 法定代表人身份证（正反面）
- [ ] 法人手机号（备案/支付/短信各种验证）
- [ ] **对公银行账户信息 / 开户许可证**（= 收款信息，绑商户结算 + 退款）
- [ ] 公司发票抬头 + 税号（给客户开发票）

**云 & 通信（同一个阿里云企业实名账号下）**
- [ ] **阿里云账号（企业实名认证）**——ECS / 短信 / OSS / SSL证书 / 备案 都在这
- [ ] **域名**（阿里云注册并实名）
- [ ] **SSL 证书**（阿里云免费 DV，Part 3 §H 申请）
- [ ] **阿里云短信**：**签名**（≈1–2 天审核）+ 含 `${code}` 的**模板**
- [ ] **企业邮箱**（腾讯企业邮 `smtp.exmail.qq.com` / 阿里企业邮 `smtp.qiye.aliyun.com`）→ 系统邮件&客服
- [ ] **企业微信**：① 群机器人 **Webhook**（告警/对账/支持）② 客服**群二维码**（站内"联系支持")

**支付（收费必备，需营业执照+对公账户）**
- [ ] **支付宝开放平台**应用：`APPID`、**应用私钥**、**支付宝公钥**
- [ ] **微信支付商户**：`商户号MCHID`、绑定的**公众号/开放平台 AppID**、`APIv3密钥`、`API证书序列号`、`apiclient_key.pem`、`微信支付平台证书`

**模型（团队"大脑"，必须实名认证否则 402）**
- [ ] **DeepSeek**：实名 + 充值 + `DEEPSEEK_API_KEY`（domestic 预设唯一必需的模型 key）
- [ ] **阿里云 DashScope/通义千问**（可选第 2 模型 + **唯一可用的联网搜索**）：实名 + `DASHSCOPE_API_KEY`

**品牌 & 合规文本**
- [ ] 公司 **Logo**（PNG/JPG ≤1MB）+ **品牌色**（站内"企业大脑→品牌"上传）
- [ ] **隐私政策 + 用户协议** 文本
- [ ] 上线后拿到的 **ICP 备案号 + 公安备案号**（放页脚）

**部署密钥（部署时在服务器现场生成，见 §E）**
- [ ] `SECRET_KEY`、`POSTGRES_PASSWORD`、`FERNET_KEYS`

---

## PART 3 — 从零到上线 + 监控（超详细，标注"在哪做"）

> 图例：🖥️【浏览器·阿里云控制台】 ＝网页点；💻【你的电脑·Mac 终端】 ＝本地终端；
> 🐧【服务器·SSH】 ＝登录服务器后的终端。**严格照标注在对应地方操作。**

### A. 买服务器 + 域名（🖥️ 阿里云控制台）
1. 🖥️ 注册/登录阿里云 → 完成**企业实名认证**。
2. 🖥️ **域名** → 注册域名 → 立即**域名实名认证**（上传营业执照）。
3. 🖥️ **ECS** → 购买（**包月**，**大陆 region** 如华东1·杭州）：**2 vCPU/4 GB** 起（有付费用户后升 4C8G）；
   系统盘 **40 GB+ SSD**；镜像 **Ubuntu 22.04 LTS**；设 root 密码或 SSH 密钥；记下**公网 IP**。
4. 🖥️ ECS → **安全组** → 放行入方向 **80、443**；**22 只放行你自己的公网 IP**。

### B. 备案（🖥️ 阿里云备案 + 省通管局）—— 与 C–G 并行
1. 🖥️ 阿里云 **备案** → 申请**备案服务号**（来自这台 ECS）。
2. 🖥️ 填主体（营业执照）+ 网站信息 + 网站负责人，用阿里云 App **人脸核验/拍照**。
3. ⏳ 等**管局审核**（1–20 工作日）。**过审前只能用公网 IP 自测，不能用域名对外。**
4. 🖥️ 收费上线：并行向**省通信管理局**申请 **经营性ICP(EDI)**（建议代办；同时备等保、算法备案）。
5. 备案通过后 → **公安备案**（`beian.mps.gov.cn`，30 天内），备案号回填页脚。

### C. 把代码传到服务器（💻 你的电脑 → 服务器）
本项目目前**没有 git 远程仓库**，二选一：
- **方式① 直接传（最省事）** —— 💻 你的电脑终端：
  ```bash
  rsync -avz --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
        --exclude 'data' --exclude 'memory_store' --exclude '.env' \
        ~/Company_Agent_Demo/  root@<服务器公网IP>:/opt/company-agent/
  ```
- **方式② 私有仓库**：推到阿里云 Codeup / Gitee 私库，再在服务器 `git clone`（以后好更新）。

### D. 装 Docker（🐧 服务器·SSH）
```bash
ssh root@<服务器公网IP>                  # 💻 本地输入登录
curl -fsSL https://get.docker.com | sh  # 🐧 一行装好 Docker + compose 插件
docker version && docker compose version
```

### E. 生成 3 个密钥（🐧 服务器·SSH）
```bash
cd /opt/company-agent
python3 -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))"
python3 -c "from cryptography.fernet import Fernet; print('FERNET_KEYS=' + Fernet.generate_key().decode())"
# POSTGRES_PASSWORD 自己想一串长随机串
```

### F. 写 .env（🐧 服务器·SSH）
```bash
cp .env.example .env
nano .env            # 粘贴下面这块，把 <...> 换成你的值
```
```ini
# —— 启动/安全（生产必填）——
APP_ENV=production
SECRET_KEY=<上一步生成的>
POSTGRES_PASSWORD=<长随机串>
FERNET_KEYS=<上一步生成的>
# —— 中国：整队跑国产模型，无需 Anthropic ——
TEAM_MODEL_PRESET=domestic
DEEPSEEK_API_KEY=sk-<你的 deepseek key>
# —— 可选：Qwen 作第二模型 + 联网搜索（国内 key 必须配 base_url）——
DASHSCOPE_API_KEY=sk-<你的 dashscope key>
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
# —— 你（管理员）+ 公网身份 ——
ADMIN_EMAILS=you@yourcompany.com
APP_BASE_URL=https://your-domain.com
PRODUCT_DOMAIN=your-domain.com
PRODUCT_NAME=你的产品名
# —— 花钱的"急停开关"（务必设）——
PLATFORM_HOURLY_CAP_YUAN=200
PLATFORM_DAILY_CAP_YUAN=2000
ACCOUNT_DAILY_CAP_YUAN=200
ADMIN_ALERT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<企业微信机器人>
# —— 异地备份（推荐）——
BACKUP_REMOTE=oss:my-bucket/company-agent
# —— 公开 AI 面：默认 0=登录后才用 Fusion（降算法备案风险）。要开公开试用改 3 ——
# FUSION_FREE_ANON_TURNS=0
```
> `RATELIMIT_STORAGE_URI` 和 `TRUST_PROXY` **由 docker-compose 自动设好**（Redis + nginx 一跳）。
> 只有在 nginx 前再加云负载均衡时，才把 `TRUST_PROXY` 改成 `2`。

### G. 启动整套（🐧 服务器·SSH）
```bash
docker compose up -d --build      # 一次拉起 5 个容器（含自动 alembic 迁移）
docker compose logs -f app        # 看到 "migrations applied" + gunicorn 启动即 OK（Ctrl+C 退出）
curl -fsS http://localhost/healthz   # → {"status":"ok","db":"ok",...}
```
浏览器开 `http://<服务器公网IP>/`，注册一个号，跑一份报告（走 DeepSeek）。能出报告即成功。

### H. HTTPS + 域名（🖥️ 阿里云 + 🐧 服务器）
本仓库**已 HTTPS-ready**：compose 已发布 443 并挂载 `./certs`，`nginx.conf` 底部留好了 HTTPS 配置块。
1. 🖥️ 阿里云 **SSL 证书** → 申请**免费 DV 证书**（绑你的域名）→ 签发 → **下载 Nginx 版**
   （得到 `your-domain.pem` 和 `your-domain.key`）。
2. 💻 传到服务器：`scp your-domain.pem your-domain.key root@<服务器IP>:/opt/company-agent/certs/`
3. 🐧 编辑 `nginx.conf`：把上方 `server { listen 80; server_name _; ... }` 注释掉，**解开**底部 HTTPS 两块、
   把 `your-domain.com` 换成你的域名；`docker compose up -d`（重载 nginx）。
4. 🖥️ 阿里云 **云解析 DNS** → 加 **A 记录** 指向服务器 IP（**备案通过后**再指）。
   开 `https://your-domain.com` 应正常加载。

### I. 接真实短信 / 邮件 / 支付（🖥️ 各控制台 + 🐧 改 .env，改完都 `docker compose up -d`）
- **短信**（🖥️ `dysms.console.aliyun.com`）：申请**签名**+**含 `${code}` 模板**，把
  `SMS_PROVIDER=aliyun` + `ALIYUN_SMS_ACCESS_KEY_ID/SECRET/SIGN_NAME/TEMPLATE_CODE` 写进 `.env`。
- **邮件**：把企业邮箱 `SMTP_HOST/PORT/USER/PASS/FROM`（用**客户端授权码**，非登录密码）写进 `.env`。
- **支付（收费核心，EDI 批下来再正式开）**：先 `APP_BASE_URL=https://你的域名`，再填 `ALIPAY_*`
  （开放平台建应用·绑电脑网站支付）和 `WECHATPAY_*`（商户平台·API证书/APIv3密钥）。
  **填上真支付后 mock 收银台自动失效**；没填前 mock 一直在（可先免费/内测）。
  在 `agent_team/config.py` 的 `PLANS` 改成你真实套餐价。

### J. 上线自检（🐧 服务器·SSH）
```bash
docker compose exec app alembic check    # → "No new upgrade operations detected."
docker compose exec app python -m pytest tests/test_phase7_concurrency.py tests/test_phase8_security.py -q
curl -fsS https://your-domain.com/healthz
```
逐条对照 Part 1/2 清单：备案、EDI 进度、支付实测进真收银台、隐私政策已发布。

### K. 监控 + 看数据 + 备份（🐧 服务器 + 🖥️ /admin）
- **健康探针**：`curl https://your-domain.com/healthz`（200=DB 正常；`scheduler_stale:true`=定时器掉了但站不挂）。
- **实时日志**：`docker compose logs -f app scheduler`（每请求一行 JSON，不打印密钥/文件内容）。
- **运营看板**（🖥️ 用 `ADMIN_EMAILS` 的号登录）：`https://your-domain.com/admin` —— 注册/激活/留存、
  **MRR/流失**、单账户成本 vs 收入、引导漏斗、对账 `/admin/reconciliation`、发票 `/admin/fapiao`、
  退款 `/admin/orders`、**急停开关 `/admin/circuit-breaker`**（被 503「系统繁忙」时来这里 reset）。
- **直接查库**：`docker compose exec postgres psql -U company company`（账号 `accounts`、报告 `runs`…）。
- **审计**：`docker compose exec postgres psql -U company company -c "SELECT created_at,account_id,action,ip FROM audit_log ORDER BY id DESC LIMIT 50;"`
- **每日备份**（🐧 主机 crontab）：`30 2 * * * /opt/company-agent/scripts/backup.sh >> /var/log/company-backup.log 2>&1`
  （设了 `BACKUP_REMOTE` 还会自动 `rclone` 上传 OSS/COS）。**务必按 `docs/ops.md §4a` 演练一次"恢复到临时库"。**
- **更新版本**：💻 重新 `rsync`（或 `git pull`）→ 🐧 `docker compose up -d --build`（自动迁移，数据卷不动）。
- **千万别**在生产敲 `docker compose down -v`（删库）；停服务用 `docker compose down`（保数据卷）。

---

## 关键提醒（China-specific 易踩坑）
1. **备案没过就解析域名对外** → 云商封端口、违规。过审前只用 IP 自测。
2. **收费但没 EDI** → 违规。建议**先免费/内测，EDI 批了再开支付**（mock 收银台天然支持）。
3. **国内 key 配了国际 base_url** → 401。务必配 `DASHSCOPE_BASE_URL` 等 `*_BASE_URL`。
4. **没做实名认证** → key 合法但每次调用 402。上线前 DeepSeek/DashScope 都实名+充值。
5. **忘了花钱急停** → 公开 AI 每轮花你的钱。`PLATFORM_*_CAP_YUAN` 是硬急停，开放前必设。
6. **把密钥写进 frontend 文件** → 生产启动自检会拒绝启动。密钥只放 `.env`，永不提交 `.env`/`certs/`。
7. **公开 AI 合规**：Fusion 公开聊天是开放式生成——默认已收到登录后；要开公开试用先确认算法备案口径。

---

### 更多细节
- `.env.example` — 每个环境变量的内联说明。
- `docs/ops.md` — 部署/备份/恢复/监控 runbook + 验收测试。
- `docs/launch-checklist.md` — 逐能力"填这个"的对照表。
- `CLAUDE.md`（China / domestic mode）— 国产预设 + 大陆 base_url 的原理。
