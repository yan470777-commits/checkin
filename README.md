# NewAPI 自动签到 · 增强版

多站点 NewAPI / OneAPI 中转站每日自动签到。**Fork 到 GitHub 云端跑，手机只收通知**——
不用开电脑、不用挂浏览器、不用天天掏 Cookie。

在 [Jasonliu-0/Newapi-checkin](https://github.com/Jasonliu-0/Newapi-checkin) 基础上增强：

| 增强项 | 说明 |
|---|---|
| 🎫 **签到码支持** | 站点要「签到码/口令」才能签？支持写死当天码、`${环境变量}` 占位、或填发布页地址**每天自动抓取** |
| 🖼️ **滑块验证码求解** | 新增 `slider_solver.py`：真实 Chrome + ddddocr 本地识别缺口，拟人化拖拽过滑块（极验/自研滑块） |
| 🔍 **站点模式探测** | 新增 `probe.py`：用真实凭据实测站点需要哪种签到方式，输出推荐配置，批量录入不抓瞎 |
| 📱 **手机端适配** | 网页配置生成器新增签到码字段；全流程手机浏览器可操作 |

## 你的四类站点，分别怎么处理

| 站点类型 | 处理方式 | 你要做的事 |
|---|---|---|
| 脚本放了（普通签到） | HTTP 直连 `/api/user/checkin` | 只填 access_token |
| 要签到码 | 签到返回需要码时，自动解析并带码重试 | 填 `checkin_code` 或 `checkin_code_url` |
| 纯粹人机验证（Turnstile） | 真实 Chrome 渲染组件自动点击 | 无（自动） |
| 滑动图片（滑块） | ddddocr 识别缺口 + 拟人化拖拽 | 无（自动，GHA 需能跑 Chrome） |
| Cloudflare 盾 | 无头浏览器过检，失败自动切真实 Chrome | 无（自动） |
| PoW 工作量证明 | 本地解算 SHA-256 nonce | 无（自动） |

## 🚀 手机端 5 分钟部署（推荐）

> 全程手机浏览器操作。签到时 GitHub 云端执行，结果推到手机。

**第 1 步：Fork 本仓库**
手机浏览器打开本仓库页 → 右上角 `Fork` → 创建到你自己的账号。

**第 2 步：开启 GitHub Pages（配置工具要挂在 Pages 上）**
Fork 后的仓库 → `Settings` → `Pages` → Source 选 `Deploy from a branch` → 分支 `main`，目录 `/ (root)` → `Save`。
等 1~2 分钟，访问 `https://你的用户名.github.io/newapi-checkin-plus/` 就是配置生成器。

**第 3 步：抓各站点的 access_token**
手机浏览器登录中转站 → 右上角头像 → 个人设置 → **系统访问令牌** → 生成并复制。
（注意：是系统访问令牌，不是 `sk-` 开头的模型调用密钥，两者不能混用。）

**第 4 步：生成配置**
打开你的 Pages 配置页 → 按表单填站点地址 + 令牌 + 用户ID → 生成 JSON 并复制。
有多个站就反复添加。要签到码的站，把「签到码」或「签到码发布页」也填上。

**第 5 步：写进 Secrets**
仓库 → `Settings` → `Secrets and variables` → `Actions` → `New repository secret`
→ 名称 `NEWAPI_ACCOUNTS`，值粘贴第 4 步的 JSON → 保存。

**第 6 步：启用 Actions**
仓库 → `Actions` 页 → `I understand my workflows, go ahead and enable them` → 选 `NewAPI 自动签到` → `Run workflow` 手动跑一次测试。
以后每天北京时间 8:10 自动执行。

**第 7 步（建议）：配手机通知**
签到结果直接推到手机，失败了才知道去处理。任选其一，加到 Secrets：

| 通知方式 | Secret 名 | 值 |
|---|---|---|
| Server酱（微信） | `SERVERCHAN_SENDKEY` | SendKey |
| 钉钉机器人 | `DINGTALK_WEBHOOK` | Webhook 地址 |
| 邮件 | `EMAIL_SMTP_HOST` 等 | 见 `.env.example` |

> 手机上第一时间收到「✅ 成功 N 个 / ❌ 失败 M 个」的日报，失败的附排查建议。

## 🔑 为什么用 access_token 不用 Cookie

- 系统访问令牌走 `Authorization` 头认证，**不过期**，不用每周重新抓 Cookie
- Cookie（session）通常几天到几周就失效，手机上掏 Cookie 很痛苦
- 令牌同样支持 Turnstile / CF / PoW 站点的自动签到

## ⚙️ 配置字段详解

`NEWAPI_ACCOUNTS` 用 JSON 数组，每个站点一个对象：

```json
[
  {
    "url": "https://api.example.com",
    "session": "",
    "access_token": "系统访问令牌",
    "user_id": "123",
    "name": "主力站",
    "checkin_code": "当天签到码（可选）",
    "checkin_code_url": "https://签到码发布页（可选，每天自动抓）",
    "checkin_code_regex": "签到码[:：]\\s*([A-Za-z0-9]{4,20})（可选，自定义提取正则）",
    "login_username": "账号（可选，session失效时自动登录续期）",
    "login_password": "密码（可选）",
    "cf_clearance": "xxsCgaPMM...（可选，有CF时配合使用）"
  }
]
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `url` | ✅ | 站点地址 |
| `access_token` / `session` | ✅ 二选一 | 推荐 access_token |
| `user_id` | ✅ | 个人设置页显示的數字 ID |
| `name` | | 备注，通知里显示 |
| `checkin_code` | | 当天签到码，支持 `${ENV_VAR}` 占位 |
| `checkin_code_url` | | 签到码发布页，每天自动抓取 |
| `checkin_code_regex` | | 自定义提取正则（含一个捕获组） |
| `login_username` / `login_password` | | session 过期时自动重新登录 |
| `cf_clearance` | | CF 过站后的 clearance（一般不用手动填） |

签到码解析优先级：`checkin_code` 固定值 → `checkin_code_url` 抓取 → 站点自身公告接口/首页。
签到返回需要验证码时，脚本会依次尝试 body / query / header 三种常见传码位置。

## 🔍 批量录入：先用 probe.py 探测站点

站点太多不知道每个站是什么脾气？本地跑一遍探测：

```bash
pip install requests
python probe.py https://api.example.com <session或access_token> [user_id]
```

它会实测并输出：`plain`（普通）/ `code`（要签到码）/ `turnstile`（人机验证）/
`pow` / `slider`（滑块）/ `cf`（CF盾）/ `auth_failed`，并直接给出推荐配置 JSON。

> ⚠️ 探测第 4 步是真实签到请求：今天还没签会直接帮你签掉，已签到则跳过。

## 💻 本地运行（Windows / macOS / Linux）

```bash
git clone <你的fork>
cd newapi-checkin-plus
pip install -r requirements.txt
cp .env.example .env   # 填入 NEWAPI_ACCOUNTS 和通知配置
python checkin.py
```

滑块站需要额外依赖：`pip install ddddocr pillow`（requirements.txt 已包含）。

**交互式配置助手**：`python config_helper.py`（问答式生成配置）
**单站测试**：`python test_checkin.py <站点URL> <session值>`
**签到码抓取测试**：`python code_fetcher.py <URL> [自定义正则]`
**今日签到状态巡检（只读，不会签到）**：`python status_report.py`（加 `--json` 输出机器可读结果）

## 📁 项目文件

| 文件 | 说明 |
|---|---|
| `checkin.py` | 主签到脚本（plain / code / turnstile / pow / slider / cf 全模式分发） |
| `slider_solver.py` | 滑块验证码求解器（ddddocr + 真实 Chrome 拖拽） |
| `code_fetcher.py` | 签到码自动抓取与提取 |
| `probe.py` | 站点签到模式探测 |
| `turnstile_solver.py` | Turnstile 人机验证求解（真实 Chrome + CDP） |
| `cf_bypass.py` | Cloudflare 拦截检测与绕过 |
| `config_generator.html` | 网页配置生成器（GitHub Pages 托管，手机可用） |
| `config_helper.py` | 命令行配置助手 |
| `notifier.py` / `dingtalk_notifier.py` | 邮件 / Server酱 / 钉钉通知 |
| `lottery.py` | 中转站专属抽奖/翻卡（哈基米、维云） |
| `sync_config.py` | 配置云端同步（WebDAV） |
| `status_report.py` | 只读巡检：查各站「今天签没签到」（不签到、不改配置） |

## ❓ 常见问题

**Q: GHA 跑滑块站成功率低？**
数据中心 IP 过滑块比家庭宽带难。滑块站建议本地跑（`python checkin.py`，走你家宽带），
或者只把滑块站留在本地、其他站放 GHA。

**Q: 访问令牌过期？**
系统访问令牌不过期。若报 `token 无效`，去站点重新生成一个，更新 `NEWAPI_ACCOUNTS`。

**Q: 签到码每天变怎么办？**
用 `checkin_code_url` 指向签到码发布页，脚本每天自动抓。发布页需要自定义正则时配 `checkin_code_regex`。

**Q: 通知里怎么知道哪站失败了？**
通知按站列出结果，失败的附排查建议（重新生成令牌 / 补签到码 / 换 IP 等）。

**Q: 定时时间能改吗？**
编辑 `.github/workflows/checkin.yml` 里的 cron（UTC 时间，北京时间 = UTC+8）。

**Q: 部分站失败会拖垮全部吗？**
不会，单站失败自动隔离，继续跑其他站。

## ⚠️ 免责声明

1. 仅供学习交流和个人自动化使用，请遵守目标站点服务条款
2. 合理使用，不要对站点造成过大访问压力
3. 账号风险（封号、降权）由使用者自行承担
4. 所有凭证只保存在你自己的 GitHub Secrets / 本地，项目不上传任何数据

## 📄 许可证

MIT License（继承上游项目）
