# 把 Tangent Agent 做成可安装的桌面版 —— 傻瓜式步骤指南

> 这份文档回答一个问题：**“我要怎么把它变成客户能下载、双击安装的桌面 App？”**
>
> 先看清楚一件事：**“能跑”和“能签名分发给客户”是两件事，难度差很多。**
> 所以下面分成 3 条路线，从最简单到最正式。**先从路线 A 开始**，你今天就能看到效果。

---

## 0. 先选你现在要哪一条路线

| 路线 | 你得到什么 | 需要花钱吗 | 需要什么 | 适合谁 |
|------|-----------|-----------|---------|--------|
| **A. 本机直接跑（源码）** | 托盘图标 + 本地网页，自己电脑上立刻能用 | ❌ 免费 | 只要 Python | **现在就想看到效果、做演示** |
| **B. 未签名安装包（dev build）** | 一个 `.dmg`/`.msi`，能装，但系统会弹“未知开发者”警告 | ❌ 免费 | GitHub Actions（点一下） | 想测试真正的安装体验 |
| **C. 正式签名安装包** | 客户双击直接装、不报毒、能自动更新 | ✅ 要花钱（见下） | 苹果/微软证书 + 备案 | **正式发给客户、上线** |

> 👉 **路线 A 不需要 Phase 4 的任何东西。** “Phase 4 / 签名分发预算”指的只有路线 C。

---

## 路线 A —— 本机直接跑（5 分钟，免费，先做这个）

这是验证“桌面 Agent 真的能在本地文件上干活”的最快方式。**不需要签名、不需要打包、不需要花钱。**

### A1. 安装桌面版依赖

在项目根目录 `/Users/brucetang/Company_Agent_Demo` 打开终端，复制粘贴：

```bash
cd /Users/brucetang/Company_Agent_Demo
python3 -m venv .venv-desktop          # 建一个干净的虚拟环境（只做一次）
source .venv-desktop/bin/activate      # 进入它（每次开终端都要）
pip install -r requirements-desktop.txt
```

### A2. 给它一个能用的“大脑”（二选一）

桌面 daemon 自己不带任何模型钥匙。给它接上模型有两种方式：

**方式 1 —— 用你自己的 API Key（最简单，适合本机演示）**
```bash
export ANTHROPIC_API_KEY=sk-ant-...     # 或者用国内模型：export DEEPSEEK_API_KEY=...
```

**方式 2 —— 配对到云端（真正的产品形态，钥匙不落到客户电脑）**
```bash
# 1) 先在云端网页登录 → 生成配对码（见下面“路线 C 的设备配对”一节，或调用 /api/devices/pair-code）
# 2) 用配对码换设备令牌，然后：
export TANGENT_CLOUD_URL=https://你的云端地址
export TANGENT_DEVICE_TOKEN=tng-dev-...
```

### A3. 启动 daemon

```bash
python -m agent_team.runner
```

会发生什么：
- 终端打印一个端口，例如 `TANGENT_DAEMON_PORT=51873`
- 系统托盘出现一个蓝色 **T** 图标（菜单：打开面板 / 配对账户 / 监控文件夹 / 暂停 / 退出）
- 浏览器自动打开 `http://127.0.0.1:<端口>`，顶部有一条蓝色“本地模式”隐私横幅

✅ 到这一步，桌面版已经在你电脑上跑起来了。把一个 Excel 拖进 `~/Documents` 或 `~/Downloads`，就能让本地 Agent 分析它（前提是 UI 里已经接好入口 —— 见本文件末尾“⚠️ 还差 UI”）。

### A4.（可选）装成一个命令

```bash
pip install -e .      # 之后任何地方输入 tangent-agent 就能启动
tangent-agent
```

---

## 路线 B —— 未签名安装包（验证真正的双击安装体验，免费）

这一步产出一个能安装的 `.dmg`（Mac）/`.msi`（Windows），但**没有签名**，所以：
- Mac 会提示“无法验证开发者”，要右键→打开才能装；
- Windows SmartScreen 会拦一下，要点“仍要运行”。

给自己/同事内测够用了。**最省事的做法是用 GitHub Actions，一行命令不用敲：**

1. 把代码推到 GitHub。
2. 打开仓库 → **Actions** 标签 → 左侧选 **desktop-release** 工作流。
3. 点 **Run workflow** → 把 `dev_build` 勾上（默认就是勾上的）→ 绿色 **Run workflow**。
4. 等它在 macOS + Windows 上各自构建完（约 10–20 分钟）。
5. 在这次运行的页面底部 **Artifacts** 里下载 `tangent-agent-<平台>-unsigned`。

> 工作流文件：[.github/workflows/desktop-release.yml](../../.github/workflows/desktop-release.yml)。
> 它会先用 PyInstaller 打包 Python 核心（[build/agentcore.spec](../../build/agentcore.spec)），
> 再用 Tauri 把它包成桌面 App。**未签名构建会自动加 `-unsigned` 后缀**，绝不会被误当成正式版。

（进阶：如果你本机装了 Rust + Node + Tauri CLI，也能本地 `pyinstaller build/agentcore.spec && cargo tauri build` 自己出包，但对新手不推荐，用 Actions 更省心。）

---

## 路线 C —— 正式签名安装包（上线给客户用，要花钱）

这就是你说的 **“Phase 4 的事”**。下面是**完整的、按顺序的清单**。把它当成开账户 + 配密钥 + 打 tag 三大块。

### 💰 先认清成本（一次性 + 每年）

| 项目 | 干什么用的 | 费用 |
|------|-----------|------|
| **Apple Developer Program** | Mac 安装包签名 + 公证（不然 Mac 用户装不了） | **$99 / 年** |
| **Azure Trusted Signing** | Windows 安装包签名（注意：不是贵的 EV 证书） | 按量，**约 $9.99 / 月** |
| **软件著作权（软著）** | 国内分发合规 + 给杀软白名单做材料 | 代办约 ¥300–1000，自办免费但慢 |
| **杀毒软件白名单** | 防止 360 / 火绒 / 腾讯管家误杀 PyInstaller 包 | 免费，但要时间提交审核 |
| **ICP 备案的服务器** | 放自动更新的 `latest.json` + 安装包（你应该已有） | 已含在云服务器里 |

> 国内渠道特有的坑：**先开始软著和杀软白名单的流程**，它们要排队，最拖时间。详见 [docs/desktop/cn.md](cn.md)。

### C1. 苹果（macOS）

- [ ] 注册 **Apple Developer Program**（$99/年）：https://developer.apple.com/programs/
- [ ] 在 Apple 后台创建一个 **Developer ID Application** 证书，下载并导出成 `.p12` 文件（导出时会让你设密码，记住它）。
- [ ] 生成一个 **App 专用密码**（用于公证）：https://account.apple.com → 登录与安全 → App 专用密码。
- [ ] 记下你的 **Team ID**（开发者后台右上角，10 位字符）。

### C2. 微软（Windows）

- [ ] 开通 **Azure Trusted Signing**（在 Azure 门户里搜 “Trusted Signing”）。
- [ ] 建一个 **Signing Account** 和一个 **Certificate Profile**，记下它们的名字。
- [ ] 建一个有签名权限的 **App 注册（service principal）**，拿到 `client id / client secret / tenant id`。

### C3. 自动更新密钥（Ed25519）—— ⚠️ 丢了就全完了

```bash
npx @tauri-apps/cli signer generate -w tangent-updater.key
```
- 它会打印一个**公钥**，把它填到 [src-tauri/tauri.conf.json](../../src-tauri/tauri.conf.json) 里
  `plugins.updater.pubkey`（替换掉 `REPLACE_WITH_ED25519_PUBLIC_KEY...`）。
- 它会把**私钥**写进 `tangent-updater.key`。

> 🔴 **这把私钥一旦丢失，就再也无法给已安装的用户推送任何更新** —— 所有人都得手动重装。
> **立刻做这三件事**（详见 [src-tauri/UPDATER.md](../../src-tauri/UPDATER.md)）：
> 1. 把 `tangent-updater.key` + 密码存进密码管理器 / 密封保险库；
> 2. 再做一份离线加密备份，放在另一个地方；
> 3. **永远不要 commit、不要贴进聊天/日志。**

### C4. 把真实地址填进配置（替换示例占位符）

[src-tauri/tauri.conf.json](../../src-tauri/tauri.conf.json) 里有两处写的是示例域名 `api.tangent.example`，**必须换成你自己备案过的真实网关地址**：
- `app.security.csp` 里的 `connect-src` / `img-src`
- `plugins.updater.endpoints`（放 `latest.json` 的地址）

### C5. 把密钥配进 GitHub Secrets

仓库 → **Settings → Secrets and variables → Actions → New repository secret**，逐个加（名字必须**完全一致**，工作流就是按这些名字读的）：

**苹果：**
```
APPLE_SIGNING_IDENTITY        例：Developer ID Application: 你的公司 (TEAMID)
APPLE_CERTIFICATE             .p12 文件的 base64（命令：base64 -i cert.p12 | pbcopy）
APPLE_CERTIFICATE_PASSWORD    导出 .p12 时设的密码
APPLE_ID                      你的 Apple 账号邮箱
APPLE_PASSWORD                上面 C1 生成的 App 专用密码
APPLE_TEAM_ID                 10 位 Team ID
```
**微软：**
```
AZURE_CLIENT_ID
AZURE_CLIENT_SECRET
AZURE_TENANT_ID
AZURE_TRUSTED_SIGNING_ENDPOINT     例：https://eus.codesigning.azure.net/
AZURE_TRUSTED_SIGNING_ACCOUNT      你的 Signing Account 名
AZURE_CERT_PROFILE                 你的 Certificate Profile 名
```
**更新签名：**
```
TAURI_SIGNING_PRIVATE_KEY            C3 里 tangent-updater.key 的内容
TAURI_SIGNING_PRIVATE_KEY_PASSWORD  生成密钥时设的密码
```

> 工作流里有一道“诚实闸门”：**正式 tag 如果缺任何一个签名密钥，会直接报错失败**，绝不会偷偷产出一个“看起来签了其实没签”的包。

### C6. 出正式版（打一个 tag 就行）

```bash
git tag v0.1.0-desktop      # tag 名必须是 v*-desktop
git push origin v0.1.0-desktop
```
推上去之后 GitHub Actions 会自动：
1. 在 Mac(Intel) / Mac(Apple 芯片) / Windows 三台机器上各自用 PyInstaller 打包 Python 核心；
2. 用 Tauri 打成安装包，**Mac 自动签名 + 公证、Windows 用 Azure 签名**；
3. 把带签名的安装包传到这次运行的 Artifacts。

### C7. 发布自动更新清单

- [ ] 用 [src-tauri/scripts/gen_latest_json.py](../../src-tauri/scripts/gen_latest_json.py) 处理三个平台的安装包 + `.sig` 签名，生成 `latest.json`。
- [ ] 把 `latest.json` 和安装包上传到 **C4 里填的那个备案地址**。
- [ ] 以后每出一个新版本（`v0.2.0-desktop`…），重复 C6 + C7，已安装用户就会收到更新提示。

### C8. 国内渠道收尾（详见 [cn.md](cn.md)）

- [ ] **软著**：拿构建好的安装包去申请软件著作权。
- [ ] **杀软白名单**：把签名后的 `.exe` / 安装包提交给 **360 安全卫士 / 火绒 / 腾讯电脑管家** 做白名单，避免误杀。
- [ ] **ICP 备案**：确认放 `latest.json` 和安装包的域名已备案。

### C9. 上线前人工冒烟测试（脚本测不了的）

- [ ] 签名的 `.dmg` / `.msi` 双击能越过 Gatekeeper / SmartScreen。
- [ ] App 启动后有托盘图标，浏览器打开本地 SPA。
- [ ] 选一个文件夹 → 本地跑一次 → 真的生成了一个 `.xlsx`。
- [ ] 配对后 `tng-dev-` 令牌存进了系统钥匙串（不是明文落盘），重启后还能用。
- [ ] 从 `vN` 升到 `vN+1` 的更新能被已安装的 App 接受。

---

## ⚠️ 重要：现在还差“UI 入口”（这就是你说的“服务器没变化”）

WS01–WS12 把**后端、daemon、打包脚手架**都建好了，但**客户网页里还没有对应的入口**：
- 云端网页（`python app.py`）里**没有**“智能体市场”页、**没有**“下载桌面版 / 我的设备”页、**没有**设备配对界面。
- `frontend/js/api.js` 里**还没有**调用 `/api/devices/*`、`/api/agents/catalog` 等新接口的方法。

所以**路线 A/B/C 即使全部跑通，客户在网页上也还看不到“去下载桌面版 / 配对设备”的按钮。**
这部分 UI 集成是单独的一块工作（见与本文件同批次的 UI 集成计划）。

---

## 一句话总结

- **今天就想看到效果** → 走**路线 A**，免费，5 分钟。
- **想测安装体验** → 走**路线 B**，免费，GitHub 点一下。
- **正式发给客户** → 走**路线 C**，需要 Apple $99/年 + Azure 签名 + 软著 + 杀软白名单。
- **但客户网页要先补上“下载/配对”的 UI 入口**，否则上面这些客户用不上。
