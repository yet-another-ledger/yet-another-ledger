# M14 · 收据收件箱与审阅

> 🌐 [English](M14.md) · **中文**

> **状态**：🟡 设计已共定；尚未开始实现和验收。本文是 M14 的权威设计及原子步骤清单。M15 Agent 收件依赖此流程，但另行设计。

## 执行模式

- `AGENTS.md` 规定的默认模式是手动模式。只有作者明确要求编排模式或直接生成时，才运行 implementer/reviewer/fixer 循环、逐步提交和 autosquash。
- 每个实现步骤完成后，在本地 `review-notes/M14-step<n>-impl.md` 写中文简报。所有步骤结束后，写包含完整里程碑 walkthrough 的 `review-notes/M14-report.md`。这些文件保持被忽略且不纳入版本控制。
- 编排模式下，全新的 reviewer 只看到本设计、Roadmap、本步简报和 diff。发现问题后走 fixup 与复审；五轮上限按 `AGENTS.md` 执行。

## 依赖与现有能力

- **M8/M8.5 Expense**：`ExpenseInput` 必填日期、分类、PURCHASE 侧 VAT treatment、VAT rate、净额和 VAT 金额；还包含可抵扣、支付来源、业务使用比例和折旧年数。Expense 服务验证字典项，在后端独占 `Decimal` 舍入与快照，并创建非草稿 Expense。周期性 Expense 草稿是另一条流程。
- **M8 收据文件**：`expense_attachment` 保存元数据；`Storage`/`LocalStorage` 保存原件。现有验证允许 PNG、JPEG、WebP 和 PDF，检查声明的 MIME 与魔数，并按 `max_receipt_bytes` 限制每份文件（默认 10 MiB）。删除 Expense 时已通过 FK 级联清除附件元数据，并由服务删除磁盘文件。
- **M8 AI**：已具备由作者配置的 OpenAI 兼容 Chat Completions、收据提示词、防御性的 `ExpenseAIPrefill` 解析和 PDF 栅格化。当前端点针对一份 Expense 附件提取且不持久化建议；M14 共用提取核心时须保持该端点行为。
- **M10 报表**：Expense、P/L 和 VAT 报表读取 Expense 记录。待审核收件箱记录没有 Expense 行，因此不产生会计或报表影响。
- **M15 依赖**：未来 Agent 端点将调用同一个后端收件服务。M14 不增加 MCP 服务、Agent 凭据、Telegram 发送者限制或投递幂等键。

## 目标与范围

**目标**：先接收收据原件，保留可审阅的 AI 建议；只有作者核对会计字段后，才确认创建一笔带原件的完整 Expense。

纳入范围：

- 按公司隔离的收件箱，包含待审核／已处理列表、详情／审阅页；一条代表一笔消费，可含一至五份图片和／或 PDF 原件。
- 创建或变更文件后立即持久上传并自动进行整组 AI 提取；提供手动重试；失败和缺失字段可见，但不阻断审核。
- 使用现有 `ExpenseInput` 和 Expense 服务规则手动确认、原子转挂附件、只创建一次 Expense，并保留链接到 Expense 的已处理记录。
- 显式丢弃待审核项，清理元数据并尽力删除磁盘文件；不自动过期。
- EN/ZH 前端文案、契约生成、隔离／并发测试和里程碑级 walkthrough。

排除范围：

- M15 的 MCP／Telegram 传输、Agent 鉴权、发送者白名单与重复投递去重。
- 自动入账、由 AI 权威决定 VAT treatment、创建供应商主数据、新的货币／汇率规则或新的报表计算。
- 回填现有 Expense，或将其已有附件迁进收件箱。
- 自动保留期、跨条目合并／拆分，以及确认到一笔已存在的 Expense。多份文件须在确认前归入同一待审核项。

## 已冻结的产品与架构决策

- [x] **D1 · 一条对应一笔 Expense**：一个收件箱条目代表一笔采购，包含 1–5 份文件。创建时可同时上传多份，之后可向同一待审核项补传。确认恰好创建一笔新的 PURCHASE Expense。MILEAGE 与周期性草稿不适用。
- [x] **D2 · 原始字节留存**：沿用现有存储校验和每份文件 10 MiB 的设置。每份文件最多读取 `max_receipt_bytes + 1` 字节即可判断超限。文件名只作展示元数据；存储键由受控 UUID 构成。确认只转移元数据归属，不复制或改写字节。
- [x] **D3 · 独立待审核实体**：待审核项绝不创建占位或草稿 Expense。Expense、P/L、VAT 和 Dashboard 查询均不读取收件箱行。现有 Expense 数据和报表语义不变。
- [x] **D4 · 自动但仅供参考的 AI**：先提交上传，再使用已配置的服务对当前整组文件尝试提取。网页上传 UI 会在提交前说明：启用 AI 后收据内容会发送给已配置的服务商。PDF 沿用每份文件的页数限制（`ai_pdf_max_pages`，默认 3）。混合文件组发给模型时，每张图片保留实际 MIME，PDF 页面使用 PNG。AI 关闭、缺配置、超时、服务错误或输出不全时，条目仍可审阅。作者可显式重试。
- [x] **D5 · 建议不是权威数据**：只持久化经过解析与校验的 `ExpenseAIPrefill`，以及安全的状态／错误码；不把模型原始响应、凭据或图片数据存进 JSON。展示空值与不确定字段。VAT treatment 由作者选择，模型不得权威决定。
- [x] **D6 · 整组结果时效**：每次文件变更递增 `file_revision`，并清除旧建议／错误。提取时捕获该版本与运行代数；只有仍待审核、文件组未变且属最新运行的结果才能替换建议。过时运行即使完成，也不能覆盖更新结果或已确认项。
- [x] **D7 · 完整手动确认**：请求使用现有 `ExpenseInput`，包括全部必填字典项和金额字段。UI 可把 AI 建议复制到可编辑输入框，但 Expense 服务会重新验证全部值，执行所有金额计算与快照。确认创建 `is_draft=false`。
- [x] **D8 · 只确认一次**：锁定待审核项，在一个无中途提交的事务中创建 Expense、转挂所有文件并标记确认。重复确认或并发失败方返回带原 `expense_id` 的 `409 ALREADY_CONFIRMED`；绝不创建第二笔 Expense。
- [x] **D9 · 已处理历史**：确认后的条目留在已处理列表，记录确认时间及 Expense 链接。`receipt_inbox.expense_id` 使用 `ON DELETE SET NULL`；若 Expense 后续通过现有路径被删除，已处理条目仍在，并显示链接不可用。其原件随 Expense 的删除策略处理。
- [x] **D10 · 仅手动丢弃**：待审核项一直保留到确认或显式丢弃。丢弃通过 FK／ORM 级联删除元数据，服务负责删除文件。已确认项不能通过收件箱端点丢弃或修改。移除最后一份文件会被拒绝；应改为丢弃整条。
- [x] **D11 · 租户／安全边界**：全部路由要求现有 MFA owner 会话，并据此取得 `company_id`。新收件箱行使用带事务级公司上下文的 PostgreSQL RLS；附件服务在转挂和读取时核验同公司归属。缺失或跨公司 ID 在访问文件或 AI 前返回 `404`。
- [x] **D12 · 可恢复的存储失败**：整个上传批次先验证再持久化；若后续写入或数据库提交失败，补偿删除本次新写文件。丢弃时先移除数据库可见归属，再尽力删文件，遵循现有 Expense 删除取向。清理失败时记录日志，但不暴露私有字节；进程意外崩溃可能留下无数据库引用的文件，须由管理员清理。

## 数据模型与迁移

- 新增 `receipt_inbox`：UUID 主键、`company_id` FK／索引、状态（`PENDING`／`CONFIRMED`）、可为空且唯一的 `expense_id` FK `ON DELETE SET NULL`、已解析建议 JSONB、AI 状态（`NOT_RUN`／`RUNNING`／`READY`／`UNAVAILABLE`／`FAILED`）、安全错误码、`file_revision`、提取运行代数、AI 开始／结束时间、创建者和创建／更新／确认时间。增加公司 RLS 和现有迁移使用的运行时角色授权。
- 将 `expense_attachment.expense_id` 改为可空，新增可空的 `receipt_inbox_id` FK `ON DELETE CASCADE` 及索引。数据库 `CHECK` 要求两个父级 ID 恰有一个不为空。既有行保留其 `expense_id`；不回填文件或 Expense。现有 Expense 端点继续要求 `ExpenseAttachmentRead.expense_id` 非空；待审核文件使用单独的、不暴露存储键的 `ReceiptInboxFileRead`。
- 新待审核文件继续使用现有 `receipts` 存储命名空间，由受控的公司／条目／文件 UUID 构建键。确认后即使附件归属 Expense，存储键仍保持不透明且不变。现有内容读取继续受 owner 检查；通用 Expense 附件删除和 `/expenses/ai-extract` 拒绝待审核附件，以免绕过文件版本管理。
- Expense 删除沿用其附件级联与磁盘清理；已处理收件箱行通过 `SET NULL` 链接留下。只要还存在收件箱行，降级就拒绝运行；管理员清除这些行后，才恢复旧附件约束并删除新表。升级保持全部历史附件。

## API 契约

所有端点使用 owner/MFA 鉴权和服务端取得的公司归属。`ReceiptInboxRead` 包含 `id`、`status`、`files`、`ai_state`、可空 `ai_suggestion`、可空安全 `ai_error_code`、可空 `expense_id`、`created_at`、`updated_at` 和可空 `confirmed_at`。`ReceiptInboxFileRead` 包含 ID、展示文件名、MIME、字节数和创建时间；响应均不含磁盘路径或模型原始响应。`ReceiptInboxListResponse` 按新到旧分页，用 ID 作稳定次序补充。可选 `language` 默认 `en`；网页客户端传当前 EN/ZH 语言。列表分页默认 `limit=50`、`offset=0`，`limit` 最大 100。

| 端点 | 请求 → 响应 | 主要约束 |
| --- | --- | --- |
| `POST /api/v1/receipt-inbox` | multipart `files`（1–5）、可选 `language=en|zh` → `201 ReceiptInboxRead` | 验证整个批次；自动尝试 AI 前先持久保存原件。AI 失败写进读取模型，不算上传失败。 |
| `GET /api/v1/receipt-inbox` | `status=PENDING|CONFIRMED`、`limit`／`offset` → `200 ReceiptInboxListResponse` | 按公司隔离，最新优先。 |
| `GET /api/v1/receipt-inbox/{id}` | → `200 ReceiptInboxRead` | 缺失／跨公司 → `404`。 |
| `POST /api/v1/receipt-inbox/{id}/files` | multipart `files`（1 至剩余容量）、可选 `language` → `200 ReceiptInboxRead` | 只限待审核；递增版本并自动重新提取整组。 |
| `DELETE /api/v1/receipt-inbox/{id}/files/{file_id}` | → `200 ReceiptInboxRead` | 只限待审核；至少保留一份；递增版本并重新提取。 |
| `POST /api/v1/receipt-inbox/{id}/extract` | `{language?: "en"|"zh"}` → `200 ReceiptInboxRead` | 只限待审核；显式重试；AI 失败仍返回更新后的状态。 |
| `POST /api/v1/receipt-inbox/{id}/confirm` | `ExpenseInput` → `201 ReceiptInboxConfirmationRead {inbox, expense}` | 一个事务；沿用 Expense 校验；重复／并发 → 带 `expense_id` 的 `409 ALREADY_CONFIRMED`。 |
| `DELETE /api/v1/receipt-inbox/{id}` | → `204` | 只限待审核；删除元数据并清理文件。 |

文件内容沿用 `GET /api/v1/attachments/{attachment_id}/content`，并受 owner／公司检查。`422` 用于无效文件或无效 `ExpenseInput`；`409` 用于状态／容量冲突，包括移除最后一份文件。缺失或跨公司条目和文件返回 `404`。对外错误和日志不含服务密钥、存储键或收据字节。

稳定的收件箱冲突码为 `INBOX_NOT_PENDING`、`FILE_LIMIT_REACHED`、`LAST_FILE` 和 `ALREADY_CONFIRMED`。AI 状态 `UNAVAILABLE` 携带 `AI_DISABLED` 或 `AI_NOT_CONFIGURED`；状态 `FAILED` 携带 `AI_TIMEOUT`、`AI_PROVIDER_ERROR` 或 `AI_INVALID_RESPONSE`。`READY` 仍可能有空字段，须手工审核。只有 `ALREADY_CONFIRMED` 的错误体包含 `expense_id`。

## 事务、AI 与失败流程

1. 验证全部上传 part 和容量，用受控键保存文件，然后提交收件箱／附件元数据。若后续保存或数据库提交失败，补偿删除本次写入文件。客户端若在提交后超时，可通过列出收件箱恢复。
2. 将待审核项按当前 `file_revision` 和提取运行代数标记为 `RUNNING`，不长时间持有行锁即可调用 AI。仅当条目仍待审核且两个计数仍相等时，才保存已解析建议或安全的不可用／失败状态。进程死亡留下的 `RUNNING` 状态可重试；重试开启新一代运行。
3. 确认时锁定待审核项，通过共用 Expense 服务验证用户提交的完整 `ExpenseInput`，不在中间提交就创建一笔 Expense、转挂所有附件并标记确认。稍后返回的 AI 结果不能修改已确认项。若 Expense 验证或转挂失败，整个事务回滚，原待审核项和文件保持不变。
4. 丢弃时锁定待审核项，借级联移除数据库元数据；提交后尽力删除文件。现有 Expense 删除继续沿用自身文件清理行为。不运行定时过期任务。

## 原子步骤

### Step 1 · 契约、归属约束与生产安全迁移

- **目标**：建立收件箱类型和父级归属不变量，不改变现有 Expense 行为。
- **契约**：定义 `ReceiptInboxRead`、`ReceiptInboxFileRead`、列表／确认 schema、AI／状态／错误枚举和路由形状；重新生成 `schema.d.ts`。
- **后端／数据**：增加 `receipt_inbox` ORM／迁移、RLS 与运行时授权；将附件父级改为可空／新增收件箱 FK 及恰有一个父级的检查约束；保持旧序列化器与 Expense 附件路由。
- **前端**：只保证现有 Expense 页面与生成契约编译通过；暂不加入收件箱流程。
- **必须覆盖**：全新及生产形态升级、旧行／字节／Expense 合计不变、原始 SQL 拒绝零或两个父级、FK／级联／运行时角色 RLS 矩阵、降级姿态、OpenAPI freshness 和 build。
- **盲审点**：不回填历史、不改变报表查询、不把 `ExpenseAttachmentRead.expense_id` 改可空；schema 无租户泄漏或存储路径；数据库约束同时覆盖原始 SQL 与 ORM。
- **DoD**：满足 Roadmap §5 门禁。实施后写 `review-notes/M14-step1-impl.md`。

### Step 2 · 持久收件、文件生命周期与手动丢弃

- **目标**：在没有 Expense 的情况下保存、查看、修改和丢弃原件。
- **契约**：实现创建／列表／详情、补传／移除文件、丢弃和文件内容读取；到 Step 3 接通自动 AI 前，新条目可报告 `NOT_RUN`。
- **后端／API**：建立供未来 M15 传输层共用的集中收件服务；有界读取 multipart、整批验证、1–5 份限制、安全存储键、元数据持久化、版本递增、事务锁和补偿清理。阻止通用 Expense 附件删除／AI 提取作用于待审核文件。
- **前端**：仅保证生成类型兼容；完整收件箱 UI 放在 Step 5。
- **必须覆盖**：PNG／JPEG／WebP／PDF 及混合文件组、单份大小／魔数／伪造文件名、第二份无效时第一份不变、容量、移除最后一份的拒绝、缺失／跨公司／未登录访问、上传／删除竞争、DB／存储故障注入、旧附件 CRUD 回归。
- **盲审点**：不创建占位 Expense；客户端文件名不进入路径；不用手写数据库级联；失败批次不留部分元数据；待审核文件变更不能绕过版本递增。
- **DoD**：满足 Roadmap §5 门禁。写 `review-notes/M14-step2-impl.md`。

### Step 3 · 自动整组 AI 提取与建议落盘

- **目标**：为每个待审核文件组提供可审阅建议，且 AI 可用性不成为上传前提。
- **契约**：实现重试提取，以及最终 `ai_state`／建议／错误响应；创建和文件变更从此在持久上传后自动尝试提取。
- **后端／API**：把 M8 单附件入口重构为共用的多文件提取核心；保留 M8 提示词、分类匹配、语言处理、PDF 页数限制和防御性解析。模型消息构造应给每张原生图片标真实 MIME、给每张 PDF 栅格化图片标 PNG。只持久化解析输出；以文件版本和运行代数拒绝过时写入；提供安全失败状态与手动重试。
- **前端**：暂不做完整页面；共用核心重构后须保持现有 Expense AI 按钮可用。
- **必须覆盖**：混合 JPEG／WebP／PDF 文件组且逐张 MIME 正确、PDF 页数限制、部分／空建议、畸形或提示词注入输出、关闭／缺 key／缺 model、超时／连接／HTTP 失败、重试、乱序完成、AI 调用中确认、无真实网络及原始响应／密钥落库。
- **盲审点**：旧 `/expenses/ai-extract` 语义不变；AI 失败不回滚上传；模型输出不权威决定 VAT treatment，也不写会计字段。
- **DoD**：满足 Roadmap §5 门禁。写 `review-notes/M14-step3-impl.md`。

### Step 4 · 只确认一次并转挂 Expense 附件

- **目标**：一次提交中创建一笔完整 Expense，并转挂所有原件。
- **契约**：实现接收现有 `ExpenseInput` 的 `POST /api/v1/receipt-inbox/{id}/confirm`、`201` 成功结果和带恢复链接的 `409 ALREADY_CONFIRMED`。
- **后端／API**：从目前自行提交的 Expense 创建服务中抽出可参与外层事务的原语；复用 `_apply_body`／现有校验和快照；锁定待审核项，创建非草稿 PURCHASE Expense，转移全部附件父级 ID，设置状态／链接／确认时间后只提交一次。
- **前端**：仅维持类型和 build 兼容；完整审核表单放在 Step 5。
- **必须覆盖**：所有必填字段与字典约束、后端舍入、多份原件且哈希不变、验证／转挂回滚、双击／并发确认、AI 仍运行、待审核项在报表中缺席、确认后 Expense／P/L／VAT 只计一次、之后删除 Expense 与历史链接。
- **盲审点**：没有中途提交或重试产生第二笔 Expense；前端／AI 无金额权威；不复制文件、不为报表写特殊计算；普通 Expense 创建不回退。
- **DoD**：满足 Roadmap §5 门禁。写 `review-notes/M14-step4-impl.md`。

### Step 5 · 完整网页收件箱与审阅流程

- **目标**：在 SPA 中提供待审核上传、辅助审阅、确认和已处理历史。
- **前端**：增加待审核／已处理列表和详情路由、多文件上传／补传／移除／预览及自动 AI 服务商提示、显式丢弃、AI 状态／重试／缺字段展示和已处理 Expense 链接。把现有 PURCHASE Expense 表单的字典／输入处理抽成共享组件或 composable，供收件箱审核表单与 Expense 编辑器共用；所有金额预览只取后端。完成 EN/ZH、无障碍、响应式及加载／错误状态。
- **后端**：只修复联调发现的小型契约／错误文案问题，不扩展业务范围。
- **必须覆盖**：AI 关闭／失败后手工完成、多文件组及建议刷新、作者覆盖 AI 值、验证错误、409 恢复链接、Expense 删除后的已处理项、移动布局、前端组件测试、i18n 对称、codegen 和生产 build。
- **盲审点**：UI 只发送原始 `ExpenseInput`；不自动确认；用户能看见不确定／缺失的 AI 字段；现有 Expense 编辑／附件流程仍可用；加载按钮切换不造成生产构建中的点击丢失。
- **DoD**：满足 Roadmap §5 门禁。写 `review-notes/M14-step5-impl.md`。

### Step 6 · 收尾、完整回归与里程碑报告

- **目标**：完成验证和文档，不增加功能。
- **执行**：Ruff、严格 mypy、默认及完整 PostgreSQL integration 测试、迁移升级／降级／head、运行时角色 RLS 测试、OpenAPI→TS freshness、前端测试／build、EN/ZH key 对称和 Docker build。
- **复核**：M8 Expense／附件／AI 与周期性草稿、M10 Expense／P/L／VAT／Dashboard 报表、跨公司文件访问、确认竞争、存储清理和现有 Expense 删除。
- **文档**：在两份 M14 文档和两份 Roadmap 中记录最终结果；写 `review-notes/M14-step6-impl.md`，收敛后写包含下方完整 walkthrough 的 `review-notes/M14-report.md`。仅在作者通过 walkthrough 后标记 M14 完成。
- **盲审点**：不混入 M15 Agent 传输／鉴权；证据对应最终工作树；已接受设计项不悄然消失；双语文档对齐，review notes 保持不入库。
- **DoD**：Roadmap §5 全部门禁通过。

## 🟢 部署自测点

按 `AGENTS.md` 默认开发 Compose 启动；将以下步骤放入 `review-notes/M14-report.md`，供作者做里程碑 walkthrough。

1. 打开新收件箱，在未建 Expense 时上传一张收据图片。检查原件、待审核状态，以及 AI 建议或明确且可恢复的 AI 状态。
2. 另建条目上传 PDF。预览／下载两份原件，核对字节及 MIME。向其中一条补传文件，检查更新后的整组建议。
3. 关闭或故意配错 AI，上传另一张收据并重试。检查原件仍在、失败可见且手工审阅可用。恢复 AI 后重试成功。
4. 检查缺失／不确定的建议。修正日期、分类、PURCHASE VAT treatment／rate、净额／VAT 金额和记账字段，然后确认。检查只生成一笔非草稿 Expense，且其下有全部原件。
5. 确认前，在 Expense 列表、Expense 报表、P/L、VAT、Dashboard 中检查待审核项无影响。确认后检查 Expense 只出现一次，金额由后端计算。
6. 重复确认，并核对并发 integration 测试场景。检查 `409 ALREADY_CONFIRMED` 链接原 Expense，未产生重复账目或文件。
7. 丢弃另一条待审核项；检查条目消失、文件被清理。已确认项留在已处理历史且有 Expense 链接；删除该 Expense 后链接变为不可用，文件不会复活。
8. 测试错误 MIME／大小、第六份文件、移除最后一份、未登录和跨公司访问。检查错误清晰、无泄漏文件或部分 Expense，且旧 Expense 附件上传／AI／删除仍正常。
9. 执行完整自动化门禁和 Docker build；检查 OpenAPI 类型、EN/ZH 文案、RLS 及 M8/M10 回归全绿。

## 验收结论

- 完成日期：待实现及作者 walkthrough。
- 验收：待定。
- 已知顺延：M15 Agent 收件及任何后续运维清理工具另行划定范围。
