# Yet Another Ledger

> 🌐 [English](README.md) · **中文**

面向自由职业者和小微企业的自托管发票与业务管理系统。发票、报价、开支、收款、PDF + 邮件，以及荷兰 **BTW（增值税）申报**汇总——通过 Docker Compose 运行（一个应用容器 + PostgreSQL）。

技术栈 **FastAPI + Vue 3 + PostgreSQL**。

> **状态——1.0 前版本（`v0.8.0`）。** 开源、自托管。容器镜像已发布到 GitHub Container Registry（GHCR）。`v1.0` 之前仍可能有粗糙之处与破坏性变更。

## 功能

- **发票** —— Standard、Advance、Final 以及绑定来源的 Credit Note；后端定价、并发安全编号、相互独立的生命周期/结算/贷记状态和可审计的单据链。
- **报价** —— 可复用内容块/模板、到期自动失效，并可不可变地选择直接开票、仅收据定金或正式分阶段 Advance/Final 开票。
- **编辑与复用** —— Invoice、Quote、Estimate 编辑页支持明细拖动排序；Quote 可复制为独立新编号、新日期的草稿。
- **成本核算 → 报价** —— 内部按 margin 反推卖价，成本/毛利绝不泄漏给客户。
- **收付款与退款** —— 分次收款、Quote 定金、自动结算状态、关联 Credit 的 Refund，以及 Refund Confirmation。
- **开支** —— 先审阅后入账的票据收件箱、图片／PDF 原件的 AI 提取、周期性开支、记账字段（付款来源 / 业务使用 % / 折旧年限）。
- **客户与目录** —— 地址、VAT 号、每客户默认币种与单据语言；产品/材料目录。
- **单据** —— 发票、报价、收据和 Refund Confirmation 的中英 PDF/邮件，包含发出时交易方快照、实际下载/发送成品的精确留存，以及可选 AI 建议性比对的不可变历史 Invoice PDF 补传。
- **报表** —— 盈亏（P/L）、**荷兰 BTW 申报汇总**、ICP 清单、开支报表和 ECharts 仪表盘，包含 Advance/Final/Credit 只计一次的投影。
- **平台** —— TOTP 两步验证、类型化三层设置、`Decimal` 金额计算、双语界面（English / 中文）、单容器应用经 Docker Compose 部署。

## 技术栈

| 层 | 选型 |
| --- | --- |
| 后端 | FastAPI · SQLAlchemy 2.0 (async) · Alembic · fastapi-users · PostgreSQL (asyncpg) · Python 3.12 (uv) |
| 前端 | Vue 3 + TypeScript · Pinia · Vue Router · Vite · Naive UI · ECharts |
| 打包 | 单应用容器（前端构建 + 后端 + uvicorn）+ PostgreSQL，经 Docker Compose 运行 |

## Quick Start

拉取已发布的镜像，用 Docker Compose 运行（app + PostgreSQL）。

**前置要求：** Docker + Docker Compose，以及 `git`。

```bash
# 1. 克隆（为了拿到 docker-compose.yml + .env.example）
git clone https://github.com/yet-another-ledger/yet-another-ledger.git
cd yet-another-ledger

# 2. 配置（模板已为本地 HTTP 设好 COOKIE_SECURE=false）
cp .env.example .env

# 3. 预先建好收据存储目录，且归属你的用户
#    （app 容器默认以 uid:gid 1000:1000 运行；用户不同就在 .env 里设 PUID/PGID）
mkdir -p data/storage

# 4. 拉取镜像并启动（先跑 DB 迁移，再起 app + PostgreSQL）
docker compose up -d
```

打开 **http://localhost:8000**，注册第一个（owner）账号，并设置 TOTP 两步验证。应用只发布在 `127.0.0.1` 上；远程访问请放在 TLS 反向代理后面。

停止：`docker compose down`（加 `-v` 同时删除数据库卷）。

> `:latest` 指向最新的非预发布版本；想固定本次版本就在 `.env` 里设 `JAI_IMAGE=ghcr.io/yet-another-ledger/yet-another-ledger:0.8.0`。想自行 build 而非拉取，在 `docker compose up -d` 之前先跑 `docker build -t ghcr.io/yet-another-ledger/yet-another-ledger:latest .`。

## 配置

Compose 会自动读取 `.env`。最常用的变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `jai` | 数据库凭据（app 与 Postgres 共用）。 |
| `APP_HOST_PORT` | `8000` | 应用发布到宿主机的端口（仅 loopback）。 |
| `COOKIE_SECURE` | `.env.example` 中为 `false` | 生产环境走 HTTPS 时必须设为 `true`。 |
| `BASE_URL` | `http://localhost:8000` | 邮件里生成绝对链接所用的公网 URL。 |
| `STORAGE_DIR` | `./data/storage` | bind-mount 给收据/附件的宿主机目录。 |
| `PUID` / `PGID` | `1000` | app 运行所用的宿主机 uid:gid（拥有存储目录）。 |
| `AUTH_SECRET` | 自动 | 首次启动自动生成并持久化到 DB；仅在需固定外部密钥时设置。 |

### SMTP 与服务器端 Sent 副本

首次登录后，在「设置」中配置 SMTP，用于单据邮件、收款收据、退款确认、测试邮件和密码重置。已保存且含 host 的 SMTP 设置优先；否则沿用 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USERNAME`、`SMTP_PASSWORD`、`SMTP_FROM_EMAIL`、`SMTP_FROM_NAME`、`SMTP_USE_TLS` 和 `SMTP_USE_SSL` 环境变量回退。SMTP 回退时仍保留已保存的 Sent 配置，并复用**实际生效的 SMTP 用户名／密码**；没有独立 IMAP 登录表单。供应商可能要求应用专用密码，并同时允许 SMTP 和 IMAP。

Sent 保存默认关闭。在 SMTP 面板开启后填供应商 IMAP host，选择 **SSL/TLS（通常端口 993）**或 **STARTTLS（通常端口 143）**。系统验证证书，不支持明文连接。切换安全模式不会改变显式填写的端口，请自行设置正确端口。

填写已有文件夹的精确服务器名称（包括层级前缀），或留空 Folder，自动发现唯一可选择的特殊用途 `\Sent` 文件夹。若服务器未公布特殊目录，则尝试已有且可选择的 `Sent`。多个特殊目录、没有可用目录或显式名称错误时，需要指定有效目录；系统不会创建目录或任意挑选。副本将完整邮件保存在**供应商服务器上**，包括正文、To／CC 和全部附件；首次测试后请在 webmail 查看，并下载附件核对。如果供应商已经自动保存 SMTP 邮件，**保持开关关闭**，避免重复副本。

SMTP 与 Sent 副本结果独立。复制失败时，SMTP 接受邮件仍代表发送成功；测试结果／发送警告和业务 Email log 独立显示复制结果。`SAVED` 表示服务器已确认 APPEND；`FAILED` 包括结果不确定；`NOT_ATTEMPTED` 表示关闭复制或 SMTP 失败。历史日志为 `NOT_ATTEMPTED`，不推断旧邮件是否已在供应商 Sent 中。密码重置响应保留原有行为，副本失败只记录安全运行日志码，不新增业务邮件记录。

副本失败时检查稳定错误码：`IMAP_CONFIGURATION_FAILED` 表示缺少 host／可用共享凭据；连接／TLS／超时码需要检查 host、端口、安全模式、证书与可达性；`IMAP_AUTH_FAILED` 需要检查 IMAP 权限与实际生效的 SMTP 凭据；文件夹缺失／歧义／无效／失败码需要检查精确目录和发现支持；`IMAP_APPEND_FAILED` 需要检查写权限或配额；其他复制失败使用 `IMAP_COPY_FAILED`。错误只暴露安全码，不含供应商原始响应、凭据或邮件内容。**没有重试和历史回填**，恢复设置只影响新邮件。不要为了补副本重发已经送达的业务邮件，否则收件人会再次收到邮件。

SMTP 测试失败仍返回通用的 `SMTP_SEND_FAILED` API 错误。服务端日志补充固定分类，例如 `SMTP_AUTH_FAILED`、`SMTP_SENDER_REJECTED`、`SMTP_RECIPIENT_REJECTED`、`SMTP_TIMEOUT`、`SMTP_TLS_FAILED` 和 `SMTP_CONNECTION_FAILED`，不记录供应商原文或凭据。再次手动测试前，可按分类核对相应配置。

## 开发

从源码起完整开发栈（app + Postgres，带 dev override）：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

后端在 `backend/`（`uv run ...`），前端在 `frontend/`（`npm run ...`）。约定、红线与契约先行工作流见 [`AGENTS_zh.md`](AGENTS_zh.md)。

## 路线图

M0–M14 已完成。**M15 仍在开发中，作者于 2026-10-24 决定暂缓，优先开展其他工作**；尚未验收。实现与走查修复保存在 `wip/m15-agent-accounting-paused` 分支，不属于 `main` 的功能。M15 范围为具备 scope 的 Agent/MCP 收据接入及客户、报价、普通发票草稿工具；现有 M14 收件箱与 AI 提取保持独立。当前没有活动里程碑，后续工作由作者指定，不自动恢复 M15。详见 [M15 状态与恢复检查点](docs/plan/milestones/M15_zh.md)、[路线图](docs/plan/roadmap_zh.md)及 [M14](docs/plan/milestones/M14_zh.md)。

SI1（单据排序、Quote 复制和服务器侧 Sent 副本）已于2026-10-25按代表性走查范围验收；未实际执行的邮件类型和失败分支明确记录为自动化覆盖。参见 [SI1 计划与验收](docs/plan/improvements/SI1_zh.md)。

## 文档

文档英文优先，并配同步的中文镜像（`*_zh.md`）。从 [`docs/plan/roadmap_zh.md`](docs/plan/roadmap_zh.md) 入手；荷兰 VAT/BTW 申报口径见 [`docs/insight/btw-aangifte-2026-guide_zh.md`](docs/insight/btw-aangifte-2026-guide_zh.md)。

## 许可证

[MIT](LICENSE)。
