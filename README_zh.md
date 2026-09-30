# Yet Another Ledger

> 🌐 [English](README.md) · **中文**

面向自由职业者和小微企业的自托管发票与业务管理系统。发票、报价、开支、收款、PDF + 邮件，以及荷兰 **BTW（增值税）申报**汇总——通过 Docker Compose 运行（一个应用容器 + PostgreSQL）。

技术栈 **FastAPI + Vue 3 + PostgreSQL**。

> **状态——1.0 前版本（`v0.7.0`）。** 开源、自托管。容器镜像已发布到 GitHub Container Registry（GHCR）。`v1.0` 之前仍可能有粗糙之处与破坏性变更。

## 功能

- **发票** —— Standard、Advance、Final 以及绑定来源的 Credit Note；后端定价、并发安全编号、相互独立的生命周期/结算/贷记状态和可审计的单据链。
- **报价** —— 可复用内容块/模板、到期自动失效，并可不可变地选择直接开票、仅收据定金或正式分阶段 Advance/Final 开票。
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

> `:latest` 指向最新的非预发布版本；想固定本次版本就在 `.env` 里设 `JAI_IMAGE=ghcr.io/yet-another-ledger/yet-another-ledger:0.7.0`。想自行 build 而非拉取，在 `docker compose up -d` 之前先跑 `docker build -t ghcr.io/yet-another-ledger/yet-another-ledger:latest .`。

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

SMTP（用于密码重置和发送发票）在首次登录后于应用内「设置」中配置。

## 开发

从源码起完整开发栈（app + Postgres，带 dev override）：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

后端在 `backend/`（`uv run ...`），前端在 `frontend/`（`npm run ...`）。约定、红线与契约先行工作流见 [`AGENTS_zh.md`](AGENTS_zh.md)。

## 路线图

M0–M14 已完成。M15 自动化实现已完成，作者部署走查与验收待进行。它增加了具备 scope 的 MCP 工具，用于收据收件以及客户、报价和普通发票草稿工作流。详见 [M15 设计与验收清单](docs/plan/milestones/M15_zh.md)、[Hermes／Telegram 部署指南](docs/plan/milestones/M15-hermes-telegram_zh.md)和[路线图](docs/plan/roadmap_zh.md)。M14 增加票据收件箱：先上传原件、审阅 AI 填入的字段，再显式确认入账。详见 [`docs/plan/milestones/M14_zh.md`](docs/plan/milestones/M14_zh.md)。

## 文档

文档英文优先，并配同步的中文镜像（`*_zh.md`）。从 [`docs/plan/roadmap_zh.md`](docs/plan/roadmap_zh.md) 入手；荷兰 VAT/BTW 申报口径见 [`docs/insight/btw-aangifte-2026-guide_zh.md`](docs/insight/btw-aangifte-2026-guide_zh.md)。

## 许可证

[MIT](LICENSE)。
