# SI4 · 通过 MCP 读取费用

> 🌐 [English](SI4.md) · **中文**

> **状态（2026-11-10）** 🟢 实现、独立盲审及作者委托的真实 TCP subagent 验收完成。两个只读工具及 `expense:read` 已交付，版本为 0.13.0。M15 的历史验收保留，人工网页／Telegram／Hermes 检查与本轮自动化证据分开记录。

> **后续计划（2026-11-10）** [M18 里程 MCP 录入](../milestones/M18_zh.md)设计已确认、待实施，将复用既有里程与 Agent 基础提供客户端通用的选项、预览及确认创建，使用独立 `mileage:create` 权限。

## 契约与边界

- 新增 `list_expenses` 和 `get_expense`，均要求新的 `expense:read`。当前完整工具面变为 18 项、权限变为九项。旧 token 保留原有授权，owner 需要创建替换 token 获得新权限。
- `list_expenses` 对齐现有 owner API 的筛选 `q`、`category_id`、`vat_treatment_id`、`deductible`、`is_draft`、`kind`、含端点的 `date_from`／`date_to`、`limit`（默认 50、范围 1–500）、`offset`（默认 0、非负）、`sort_by`（`expense_date` 或 `created_at`，默认前者）。复用现有费用排序，返回已有类型化列表行及 total。草稿与已确认费用均沿用现有筛选语义。
- `get_expense` 接受 UUID `expense_id`，返回包含已有 `ExpenseRead` 字段的 `expense`。不存在及其他公司的 ID 均返回同一 `MCP_TARGET_NOT_FOUND`。输入通过既有安全校验拒绝未知字段及无效类型／边界，每次请求继续检查权限、撤销及 Host／Origin。
- 复用现有费用服务，在非超级用户 runtime role 下设置事务级 RLS。沿用现有定价及租户隔离逻辑。响应仅包含类型化业务字段及附件数，排除文件字节、磁盘／存储路径及 AI provider 原始响应。读取不要求 request ID 或 source，不创建操作／重放记录，继续正常记录 token 最近使用时间。
- 本项仅交付费用读取。费用修改、过账／确认、附件下载及报表工具均在范围之外。
- 在 `0056` 后新增 `0057`，扩展既有 token scope CHECK 约束，保留已发布迁移、token 数据与操作历史。如果任意存量 token 仍含 `expense:read`，降级必须事务性失败，保留数据及新约束。触发此保护时，运维恢复旧版本使用升级前备份。

## 原子实现步骤

### Step 1 · 费用读取契约、授权及客户端

- 先定义 schema，再扩展 scope 枚举、受限 MCP 注册与服务委托。更新 token UI／类型及一致的 EN／ZH 文案，重新生成 OpenAPI TypeScript，新增迁移及对应回归。
- 当前完整 Hermes 配置／skill 及部署说明同步为 18 工具／九权限，并说明只读费用能力。仅收据及其他独立角色配置按自身权限保留。M15 原始冻结的 16 工具／八权限历史保留，并增加当前扩展入口。更新成对 README／roadmap／SI4 文档，项目版本统一为 0.13.0，依赖保持锁定。
- 测试覆盖真实 runtime-role 读取、列表／详情一致性、筛选分页、空列表、不存在／跨公司 ID、无效输入和安全错误、不足权限、撤销凭据含既有会话、旧 token 兼容，以及业务记录和操作记录计数。迁移验证存量 token 升级、未知 scope 拒绝、降级保护及不存在新 scope 数据时的安全降级。UI 测试覆盖第九项权限。
- 必要门禁为 Ruff、strict Mypy、默认 pytest、专用新 PostgreSQL 容器上的 integration pytest、类型生成无漂移、前端测试／构建及 Docker build。证据可结合本地验证及最终准确提交的 CI，未执行项明确区分。
- 独立审查仅接收本设计、roadmap／M15 背景及本步完整 diff。重点检查 scope 与工具映射、RLS、迁移兼容、安全失败、API／schema 一致性、旧 token 行为及双语配置。Fixer 接收相关 review 和 implementation 报告，全部 Git 写入由编排者负责。

## 独立验收与发版

Step 1 审查通过后，全新 subagent 使用独占临时 loopback 端口、一次性 PostgreSQL 数据库和两家公司合成数据，对真实 Streamable HTTP MCP 服务验收。覆盖发现、筛选／分页列表、详情、不存在／跨公司 ID、无效输入、不足权限及撤销。对比调用前后的业务行数及操作记录数，记录命令及预期／实际结果并清理独占资源。生产／开发／原走查环境及运行中的 Hermes均不作为测试目标。

作者已将本次验收委托给 subagent。真实 MCP 客户端验收与人工网页／Telegram 检查分别记录。审查及独立验收通过后提交推送，最终准确提交通过完整 CI 门禁后发布 0.13.0，支持 linux/amd64 与 linux/arm64，核验远端标签摘要及拉取后的镜像，返回多架构 SHA-256。中间简报及发版证据仅保存在被忽略的 `review-notes/`。

## 验收结论（2026-11-10）

- 一项原子实现及两轮修复完成，最终全新 Reviewer 无可操作发现。根参数校验、已撤销 token 的降级说明及既有收据工具发现回归均已核验。
- 本地 1705 项默认后端测试、1303 项完整 PostgreSQL 集成测试、Ruff、strict Mypy、前端 7 项 Node／431 项组件测试、前端构建、类型生成与 Docker 构建通过。修复后的独立审查另完成 26 项相关 PostgreSQL 测试及协议探针。
- 全新 subagent 在专用 PostgreSQL、临时 loopback TCP 服务和两家合成公司上完成 19／19 项验收，覆盖发现、筛选分页、详情、空列表、实际外公司及不存在 ID、无效参数、权限不足和已建立会话撤销。费用快照及 Agent operation 行数核对一致。
- 公司隔离验证沿用现有 Expense 服务边界及 runtime role 下的事务级 company context，与 roadmap §3.3 一致。
- 作者已委托本次自动化验收并授权提交推送发版。人工网页、Telegram 及 live Hermes 检查未执行，不能记为人工通过。
- CI 与镜像发布结果由 GitHub Actions 和正式 v0.13.0 Release 记录。完整本机简报、修复报告、验收结果和可选人工步骤保存在被忽略的 `review-notes/SI4-expense-read-20261109/`。
