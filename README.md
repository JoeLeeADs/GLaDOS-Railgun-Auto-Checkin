# GLaDOS / Railgun 自动签到（合体版）

基于 [Devilstore/Glados-Railgun-checkin](https://github.com/Devilstore/Glados-Railgun-checkin) 与
[MZRidaz/GLaDOS-Auto-Checkin](https://github.com/MZRidaz/GLaDOS-Auto-Checkin) 整合的自动签到脚本，
运行在 GitHub Actions 上，无需服务器、无需编程基础。

## ✨ 功能特性

- 🌐 **多服务自适应**：内置 GLaDOS / Railgun 等多个候选域名，**首次探测到可用域名即锁定复用**，应对官方反复改域名
- 📱 **设备平台自适应重试**：遇到 `code 4 device-mismatch` 自动按 `loginDevice` 切换 UA 重试，无需手动配 UA
- 📬 **8 路推送**：PushDeer / Server酱 / Telegram / PushPlus / 钉钉 / 飞书 / 企业微信 / 云湖
- 🔒 **Cookie 前缀无关校验 + 归一化**：`koa:` / `gld:` / 任意前缀自动兼容，自动去引号与 `Cookie:` 头名
- 🛡️ **隐私脱敏**：日志中邮箱 / Cookie 自动打码
- 💎 **积分兑换阈值保护**：总积分达标才兑换，不浪费积分（可选）
- 🔄 **网络重试 + 退出码区分**：全部失败标红（退出码 1），区分「业务失败」与「通知失败」
- 👥 **多账号**：`COOKIES` 支持 `|||` / `&` / 换行 三种分隔

## 📂 项目结构

```
.
├── checkin.py                  # 签到脚本（单文件，仅依赖 requests）
├── requirements.txt            # 依赖
├── .github/workflows/
│   └── checkin.yml             # GitHub Actions 配置（定时 + 保活）
└── README.md
```

## 🚀 使用教程

### 1. Fork 本项目到你的 GitHub

右上角 **Fork** → 到你自己的仓库。

### 2. 获取站点 Cookie

1. 浏览器登录 [https://glados.cloud](https://glados.cloud)（或 Railgun 站点）
2. 按 **F12** → **Application**（或 **Storage**）→ **Cookies** → 复制完整 Cookie 值

示例（现网签发 `gld:` 前缀，旧版 `koa:` 同样支持）：

```
gld:sess=xxxxxx; gld:sess.sig=yyyyyy
```

> ⚠️ `sess` 与 `sess.sig` 必须同时存在、前缀一致。脚本会自动识别任意前缀。

### 3. 配置 GitHub Secrets

仓库 **Settings → Secrets and variables → Actions → Secrets → New repository secret**：

| Secret 名 | 说明 | 必填 |
| --- | --- | --- |
| `COOKIES` 或 `GLADOS_COOKIES` | Cookie 值；多账号用 `\|\|\|` / `&` / 换行分隔 | ✅ |
| `DOMAINS` | 候选域名（逗号分隔），覆盖默认值，如 `glados.cloud,railgun.info` | ❌ |
| `EXCHANGE_PLAN` 或 `GLADOS_EXCHANGE_PLAN` | 兑换计划：`plan100` / `plan200` / `plan500`，不配则不兑换 | ❌ |
| `SENDKEY` | PushDeer 推送 key | ❌ |
| `SERVERCHAN_KEY` | Server酱 key | ❌ |
| `TG_BOT_TOKEN` + `TG_CHAT_ID` | Telegram 机器人 | ❌ |
| `PUSHPLUS_TOKEN` | PushPlus token | ❌ |
| `DINGTALK_WEBHOOK` (+ `DINGTALK_SECRET`) | 钉钉机器人（加签需配 secret） | ❌ |
| `FEISHU_WEBHOOK` (+ `FEISHU_SECRET`) | 飞书机器人（加签需配 secret） | ❌ |
| `WECOM_BOT_WEBHOOK` | 企业微信机器人 | ❌ |
| `YUNHU_TOKEN` + `YUNHU_RECV_ID` (+ `YUNHU_RECV_TYPE`) | 云湖机器人 | ❌ |

> 推送渠道配哪个用哪个，可同时配多个。

### 4. 开启 Actions

**Actions** 标签页 → 启用工作流。默认**北京时间每天 09:18 自动运行一次**；也可在 Actions 页面手动 **Run workflow** 立即测试。

> 想改时间：编辑 `.github/workflows/checkin.yml` 里的 `cron: '18 9 * * *'`（分 时 日 月 周），`timezone` 保持 `Asia/Shanghai` 即可按北京时间解读。

## 💡 积分兑换说明

配置 `EXCHANGE_PLAN` 后，每次签到会在**总积分 ≥ 计划所需积分**时才调用兑换接口，否则跳过，不会浪费积分。

| 计划 | 消耗积分 | 兑换天数 |
| --- | --- | --- |
| `plan100` | 100 | 10 天 |
| `plan200` | 200 | 30 天 |
| `plan500` | 500 | 100 天 |

## ❓ 常见问题

- **签到提示「鉴权失败 / 没有权限」**：Cookie 失效或复制不完整，重新登录获取最新 Cookie 并更新 Secrets。
- **日志中邮箱/Cookie 不完整**：隐私保护自动脱敏。
- **日志时间和收到通知的时间差 8 小时？**：GitHub runner 默认 UTC。v1.1.0 起已强制 `TZ=Asia/Shanghai`，日志为北京时间；旧版日志是同一时刻的 UTC 显示（并非跑错时间）。
- **Actions 被暂停**：内置每月 1 号空提交保活；如仍被暂停，手动触发一次 workflow_dispatch。
- **通知里 `今日已领` 是什么意思**：GLaDOS 每天只能签到得分一次。若当天该账号已签到，接口只返回 `code=1, points=0`（不带当日积分），故显示 `今日已领`。自 v1.2.1 起定时为**每天一次**，正常情况下每天只会收到一条**带积分**（`今日 +N 积分`）的通知；只有你手动额外触发时才会出现 `今日已领`。
- **Cookie 有有效期吗**：有，会随会话过期，需重新获取。

## 📝 更新日志

### v1.2.1 — 2026-10-13
- **定时改为每天一次**：`cron` 由 `18 9,12`（北京 09:18 与 12:18）精简为 `18 9`（**北京时间每天 09:18**）。
  这样**每天只产生一条通知、且必定带着当天获得的积分**，不再出现第二次运行只会显示 `今日已领` 的情况。
- **兑换计划维持不变**：仍为「不配置即不兑换、配了也要总积分达标才兑」，需要时自行加 `EXCHANGE_PLAN` Secret 即可。

### v1.2.0 — 2026-10-13
- **修复「通知里看不到今日签到获得的积分」**：原实现只在「✅ 成功」分支显示积分，「🔄 已签到」分支把积分丢掉了。
  GLaDOS 在已签到时**同样会返回今日已获得的积分**（如文案「已经签到成功，获得 1 点」），现在**成功与已签到都会展示 `今日 +N 积分`**。
- **积分来源多路兜底**：按 `响应顶层 points` → `data.points` → `message` 文案（英文 `got N points` / 中文 `获得 N 点`）依次解析，兼容官方不同版本的返回结构。
- **签到分类更稳**：中文文案 `获得 N 点` 也识别为「成功」，避免因文案变化被误判为失败。
- **新增诊断日志**：每次签到打印 `code / points / message`，便于后续排查接口文案变动。

### v1.1.0 — 2026-10-12
- **域名顺序调整**：候选域名中 `railgun.info` 提前为**第一个**尝试
- **推送排版重构**：改为**每个账号独立分块、逐行展示**，修复手机上长行换行、内容挤成一团的问题
- **PushPlus 换行修复**：`html` 模板下 `\n` 不换行，现自动转为 `<br>`
- **时区修复**：日志统一为**北京时间**（`TZ=Asia/Shanghai`），不再显示 UTC 而导致与通知时间相差 8 小时
- **定时修正**：修正 `cron` 叠加 `timezone` 造成的偏差，恢复为**北京时间 09:18 与 12:18** 各签一次

### v1.0.0 — 2026-10-10
- **首版发布**：整合 [Devilstore/Glados-Railgun-checkin](https://github.com/Devilstore/Glados-Railgun-checkin) 与 [MZRidaz/GLaDOS-Auto-Checkin](https://github.com/MZRidaz/GLaDOS-Auto-Checkin) 两者优点
- **多服务自适应**：内置 GLaDOS / Railgun 候选域名，首次探测到可用域名即锁定复用（应对官方反复改域名）
- **设备平台自适应重试**：`code 4 device-mismatch` 自动按 `loginDevice` 切换 UA 重试，免手动配 UA
- **8 路推送**：PushDeer / Server酱 / Telegram / PushPlus / 钉钉 / 飞书 / 企业微信 / 云湖（纯 `requests`，零额外依赖）
- **Cookie 前缀无关校验**：`koa:` / `gld:` / 任意前缀自动兼容，自动剥离引号与 `Cookie:` 头名
- **隐私脱敏**：日志中邮箱 / Cookie 自动打码
- **积分兑换阈值保护**：仅显式配置且总积分 ≥ 阈值时才兑换，默认不兑换
- **健壮性**：网络重试 + 退出码区分「业务失败」与「通知失败」；每月空提交保活

## 📄 许可证

MIT
