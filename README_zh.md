# Yet Another Ledger

> 🌐 [English](README.md) · **中文**

面向自由职业者和小微企业的自托管发票与业务管理系统。发票、报价、开支、收款、PDF + 邮件，以及荷兰 **BTW（增值税）申报**汇总——通过 Docker Compose 运行（一个应用容器 + PostgreSQL）。

技术栈 **FastAPI + Vue 3 + PostgreSQL**。

> **状态——1.0 前版本（`v0.11.0`）。** 开源、自托管。容器镜像已发布到 GitHub Container Registry（GHCR）。`v1.0` 之前仍可能有粗糙之处与破坏性变更。

## 功能

- **发票** —— Standard、Advance、Final 以及绑定来源的 Credit Note；后端定价、并发安全编号、相互独立的生命周期/结算/贷记状态和可审计的单据链。
- **报价** —— 可复用内容块/模板、到期自动失效，并可不可变地选择直接开票、仅收据定金或正式分阶段 Advance/Final 开票。
- **报价 PDF 附录（M17）** —— 在内容资料库保存可复用静态 PDF，或上传到单个已保存报价，手动选择与排序，并在预览、下载或邮件中使用一份合并 PDF。作者于 2026-11-06 完成已记录范围的走查。
- **编辑与复用** —— 对符合资格、未交付且未收款的 Standard Invoice 进行审计修订，保留 SENT、原编号、未修改的税务／身份快照及历史 PDF；Invoice、Quote、Estimate 编辑页支持明细拖动排序；Quote 可复制为独立新编号、新日期的草稿。
- **成本核算 → 报价** —— 内部按 margin 反推卖价，成本/毛利绝不泄漏给客户。
- **收付款与退款** —— 分次收款、Quote 定金、自动结算状态、关联 Credit 的 Refund，以及 Refund Confirmation。
- **开支** —— 先审阅后入账的票据收件箱、图片／PDF 原件的 AI 提取、周期性开支、记账字段（付款来源 / 业务使用 % / 折旧年限）、独立P/L计入选择及NET/GROSS/AUTO成本口径。
- **外部销售** —— 保留平台／能源原件及原编号，审核可选AI建议、按多税率汇总入账，并审计修订、credit、结算差额及作废；可排除P/L收入而仍计入VAT。
- **家用电池** —— 对符合普通VAT条件的购置仅登记一次，确认五年Q4调整所需年度私用输入，保留证据与修订历史，明确提示缺失或未确认输入。电池资产导航默认隐藏，可在 Settings → Preference 中开启。
- **客户与目录** —— 地址、VAT 号、每客户默认币种与单据语言；产品/材料目录。
- **单据** —— 发票、报价、收据和 Refund Confirmation 的中英 PDF/邮件，包含发出时交易方快照、实际下载/发送成品的精确留存，以及可选 AI 建议性比对的不可变历史 Invoice PDF 补传。
- **报表** —— 盈亏（P/L）、**荷兰 BTW 申报汇总**、ICP 清单、开支报表和 ECharts 仪表盘，包含 Advance/Final/Credit 只计一次的投影，以及独立里程费用与扣里程前／后利润。
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

> `:latest` 指向最新的非预发布版本；想固定本次版本就在 `.env` 里设 `JAI_IMAGE=ghcr.io/yet-another-ledger/yet-another-ledger:0.11.0`。想自行 build 而非拉取，在 `docker compose up -d` 之前先跑 `docker build -t ghcr.io/yet-another-ledger/yet-another-ledger:latest .`。

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

### 报价 PDF 附录（M17）

打开 **内容库 → PDF 资料**，可上传公司私有的可复用静态 PDF，按标题搜索、预览／下载原件、修改标题，以及归档或恢复资料。在已保存的报价中，使用 **PDF 附录** 选择共享文件或上传报价专用文件、调整顺序并随报价保存。新报价必须先使用**保存并继续**创建报价，之后才能上传报价专用文件。即使之后放弃尚未保存的选择，报价专用上传仍属于该报价；如需使用，重新打开报价并从该报价专用文件列表中选入。移除已保存清单中的项目不会删除原件。

报价预览、下载和邮件使用同一份组合 PDF：先放报价正文，再按已保存顺序附上原件。保留原静态页面内容、尺寸与方向；原件不会翻译、栅格化或改写。替换资料库文件需上传新记录；归档／恢复和修改标题不会改写既有报价选择。已接受报价仍可查看和下载，但选择清单只读。复制报价不带附录，转换也不会将附录复制到 Invoice。

服务器默认的类型化限制为：单次上传 10 MiB、每个报价 10 个文件、所选原件合计 15 MiB、组合输出 15 MiB、总页数（含报价正文）200 页。部署可通过对应的类型化设置覆盖：`MAX_APPENDIX_FILE_BYTES`、`MAX_QUOTE_APPENDICES`、`MAX_QUOTE_APPENDIX_INPUT_BYTES`、`MAX_QUOTE_PDF_BYTES`、`MAX_QUOTE_PDF_PAGES`。仅接受无加密、无签名、无可填写表单的静态 PDF。系统会拒绝或移除不支持的活动内容及不安全链接。组合文件会重新序列化，因此不同请求生成的 PDF 不保证字节完全相同。邮件编码会增大附件，邮件服务商也可能设有更小上限。

附录原件及其元数据通过事务存储在 PostgreSQL（`BYTEA`），不存入 `STORAGE_DIR`。备份和恢复流程必须包含数据库；只备份存储卷并不包含这些原件。既有正式 Invoice／Refund `DocumentArtifact` 历史保持不变：M17 不会留存精确的历史报价邮件 PDF，也不会冻结报价正文。

从 v0.10.0 升级时，请先备份 PostgreSQL 和现有票据文件存储，再启动新镜像。正常 Compose 迁移服务会应用 `0053` 和 `0054`。既有报价的附录清单初始为空。如果在 `.env` 中显式设置了 `APP_VERSION`，请更新为 `0.11.0`。

## 开发

从源码起完整开发栈（app + Postgres，带 dev override）：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

后端在 `backend/`（`uv run ...`），前端在 `frontend/`（`npm run ...`）。约定、红线与契约先行工作流见 [`AGENTS_zh.md`](AGENTS_zh.md)。

## 路线图

M0–M14 已完成。M16／M17 已在记录范围内验收。**作者于 2026-11-06 恢复 M15 已有功能评估**：先将最新 main 合入 `wip/m15-agent-accounting-paused`，再从 3/6 报价准备继续走查；尚未验收或批准重设计。Agent/MCP 实现保留在 M15 分支，主线不包含该实现。现有收件箱与 AI 提取继续沿用既有服务。详见 [M15 检查点](docs/plan/milestones/M15_zh.md)和[路线图](docs/plan/roadmap_zh.md)。

作者已于 2026-11-01 按记录的代表性走查范围验收 [M16](docs/plan/milestones/M16_zh.md)，外部销售凭证记账、VAT／P&L 分离、储能年度私用调整及并入的 SI3 里程利润拆分已交付；跳过／未重测路径及已知遗留仍明确记录。

作者于 2026-11-04 确认并记录 [M17 报价 PDF 附录设计](docs/plan/milestones/M17_zh.md)：共享资料库和报价专用上传、人工选择排序、预览／下载／邮件统一合并输出。六个实施步骤均通过本地门禁和独立审查。作者于 2026-11-06 完成已记录范围的走查。走查范围与跳过项见 M17 计划。M15 保持暂停。

SI1（单据排序、Quote 复制和服务器侧 Sent 副本）已于2026-10-25按代表性走查范围验收；未实际执行的邮件类型和失败分支明确记录为自动化覆盖。参见 [SI1 计划与验收](docs/plan/improvements/SI1_zh.md)。

## 文档

文档英文优先，并配同步的中文镜像（`*_zh.md`）。从 [`docs/plan/roadmap_zh.md`](docs/plan/roadmap_zh.md) 入手；荷兰 VAT/BTW 申报口径见 [`docs/insight/btw-aangifte-2026-guide_zh.md`](docs/insight/btw-aangifte-2026-guide_zh.md)。

## 许可证

[MIT](LICENSE)。
