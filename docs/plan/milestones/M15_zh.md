# M15 · Agent 收据接入与业务草稿

> 🌐 [English](M15.md) · **中文**

> **状态**：⬜ 设计于 2026-09-30 商定；实现与部署走查尚未开始。本文是 M15 范围、契约及原子步骤的权威文档。M14 已完成并验收。

## 执行模式

- `AGENTS.md` 规定的默认模式是手动模式。只有作者明确要求编排模式或直接生成时，才运行 implementer/reviewer/fixer 循环、逐步提交和 autosquash。
- 每个实现步骤完成后，在 `review-notes/M15-step<n>-impl.md` 写中文本地简报。所有步骤结束后，在 `review-notes/M15-report.md` 写完整的里程碑走查报告。这些文件保持被忽略且不纳入版本控制。单纯规划不产生实现简报。
- 每步遵循 roadmap §5 的 DoD。API 契约变化后重新生成 `frontend/src/api/schema.d.ts`；每次修改英文文档须在同一变更中更新中文镜像。

## 依赖与现有行为

- **M14 收件箱**：`create_item` 持久保存经验证的 1–5 份 PNG/JPEG/WebP/PDF 原件，随后 `extract_item` 保存仅供参考的 AI 建议。只有 owner 能在网页将待审核项确认成一笔 Expense。M15 共用该流程，不另建收据或 Expense 引擎。
- **M3/M4/M6 主数据**：已有客户搜索／创建、产品／材料、VAT 记录和单据模板。Agent 可以查询；客户匹配、价格或税务选择有歧义时必须向用户追问，不得凭空编造权威价格或 VAT 值。
- **M5/M6/M12 单据**：现有 `QuoteWrite` 与 `InvoiceWrite` 必须有客户、日期、税模式和至少一条明细。报价创建时分配报价编号，初始为 `DRAFT`。普通发票创建时是未编号的 `DRAFT`；法定发票号只在签发时分配。两者均使用后端定价服务。缺少必填输入时，Agent 不创建部分填好的业务记录。
- **部署**：应用与 PostgreSQL 运行于现有 Compose 栈。应用端口默认仅绑定主机 localhost；首版 Hermes 与 JAI 运行在同一服务器。浏览器审阅链接使用 `BASE_URL`，而非内部 MCP 地址。

## 目标与范围

**目标**：让获授权的 Agent（首个场景是通过 Telegram 交互的 Hermes）提交采购单据供网页审核、匹配或创建客户，并通过小范围、可复用的 MCP 接口准备可编辑的草稿报价和普通销售发票。

纳入范围：

- 在现有 JAI 应用中提供 Streamable HTTP MCP 端点，并由 owner 在网页管理可撤销、按权限授权、长期有效的应用令牌。
- 提交收据及查询状态，保留原始字节，支持投递幂等、安全状态／错误报告和网页审阅链接。优先通过远程 MCP 的 Base64 文件参数传输；若真实 Hermes 图片或 PDF 传输失败，第 5 步必须补交本机流式上传适配器。
- 客户搜索／详情／创建；只读查询产品／材料、单据模板及 VAT 选项；`DRAFT` 报价和普通发票的计算预览、创建、读取及修改。Agent 在明确识别目标后，可以修改同公司任何已有草稿。
- 在 Hermes 中配置 Telegram 发送者／聊天限制，在 JAI 中按令牌授权，记录操作来源，完成 EN/ZH 设置／帮助文案和端到端作者走查。

排除范围：

- Agent 确认或丢弃收件箱条目；自动创建已入账 Expense；自动发送、签发报价／发票，变更状态，报价转发票，收款或 VAT 申报。
- Agent 修改产品、模板、VAT 字典或已有客户记录。M15 不暴露 Advance、Final、Credit Note、退款和更正草稿；未来沿用它们已有的专门服务扩展，此时不增加空占位端点。
- JAI 自带 Telegram bot、webhook、Telegram 凭据或独立的 Telegram 身份验证；v1 公网／OAuth 部署；通用数据库或无限制 CRUD 工具。

## 已冻结的产品与架构决策

- [x] **D1 · 对话与业务职责分离**：Hermes 处理自然语言、Telegram 附件访问、用户／聊天授权、追问和回复。JAI 验证结构化工具参数、权限及租户归属，并调用现有服务。JAI 的收据 AI 服务继续走 M14 提取流程。
- [x] **D2 · 两层信任检查**：在 Hermes 配置 Telegram 数字发送者 ID 和获准聊天 ID，记账流程不启用 allow-all。JAI 验证应用令牌。Hermes 提供的 Telegram 来源字段只用于追踪和去重，不是身份凭证；JAI 不能声称自己独立验证了 Telegram 用户。
- [x] **D3 · 限权应用令牌**：完成 MFA 的 owner 创建具名令牌、选择能力、只看一次明文，并可立即撤销。令牌默认不过期。JAI 只存摘要和元数据，不存明文。令牌指向一家公司和创建者；运行时角色以该公司上下文进行 RLS 查询，再核验令牌明文后授权工具。Agent 不持有 owner 的 cookie 或密码。
- [x] **D4 · 最小工具面**：只暴露下方契约列出的工具。工具发现可以隐藏令牌无权使用的工具，但每次调用仍须再次验权。网页 API 能执行的动作不代表 MCP 也能执行。即使现有网页报价生命周期允许编辑其他状态，MCP 的报价修改也仅限 `DRAFT`。
- [x] **D5 · 稳定写入 ID**：每次变更都要求调用方提供 `request_id`。Telegram 收据使用平台、聊天及消息 ID 派生的稳定键（相册使用媒体组 ID）；同一消息中的不同操作加操作后缀。其他 MCP 客户端提供自己的稳定 UUID。在同一公司／令牌／请求 ID 下，操作和规范化参数相同则返回上次结果；参数改变则返回冲突。令牌撤销后不能取回旧结果。
- [x] **D6 · 原子幂等**：在同一个数据库事务内占用操作键、完成业务变更并保存目标／结果。仅将受影响服务的提交边界重构为可嵌入事务的基础操作，保持现有网页 API 行为。崩溃或并发请求不得留下已提交但无法通过操作记录恢复的业务对象。AI 提取和 M14 一样，在收据事务持久提交后运行。
- [x] **D7 · 原件传输**：远程 `submit_receipt` 以 Base64 内容接收 1–5 份 M14 支持的文件，持久保存前执行 M14 单文件限制和 `MAX_FILES × max_receipt_bytes` 的解码后总大小限制，并在解析前限制编码后的 HTTP 请求体。绝不记录字节日志。第 5 步必须用真实 Hermes 成功发送图片和 PDF。若 Hermes 无法提供工具可见的原始字节，或 MCP 大请求不可靠，同一步交付小型 Hermes 侧 stdio MCP 适配器。其工具接收本地附件路径，只读取获准的附件缓存根目录，并通过 multipart 流式上传至受 JAI 令牌保护的接口；该接口调用相同的收件／幂等服务。备用路径不能降低验收标准，也不创建第二套收据模型。
- [x] **D8 · 草稿会计边界**：收据建议保持待审核，直到网页确认。报价和普通发票工具调用现有计算、校验服务；金额计算始终在 `services/`。创建结果为 `DRAFT`；修改时在锁定目标后检查状态。MCP 不暴露发送／签发／状态变更／确认工具。创建单据要求现有写入 schema 的完整输入；缺少事实时由 Hermes 追问，不增设不完整记录结构。
- [x] **D9 · 安全的来源和审计数据**：保存令牌 ID、操作、请求 ID、来源平台／聊天／消息 ID、目标类型／ID、结果和安全错误码。审计行和日志均不保存收据字节、Base64、AI 原始响应、Bearer 令牌或完整敏感请求体。链接使用已配置的 `BASE_URL`；若手机走查得到无法访问的 localhost 链接，视为部署未通过。

## 契约与数据变更

### Owner API 与数据

- 新增 `agent_token`：UUID ID、`company_id` FK/RLS、owner 创建者 FK、名称、令牌摘要／选择器、类型化权限集合、创建／最近使用／撤销时间。只有创建响应返回明文。`GET /api/v1/agent-tokens` 列出元数据；`POST /api/v1/agent-tokens` 创建；`DELETE /api/v1/agent-tokens/{id}` 撤销。全部要求现有 MFA owner 会话。设置面板提供 EN/ZH 创建／一次性复制／列表／撤销 UI。
- 新增 `agent_operation`：UUID ID、`company_id` FK/RLS、令牌 FK、唯一 `(company_id, token_id, request_id)`、操作名、规范化参数摘要、来源元数据、目标类型／ID、结果摘要、安全结果／错误和时间戳。这是持久的幂等与追踪记录。即使先前创建的目标后续被删除，原操作仍是已记录结果；重试不得创建替代对象。
- 定义 `receipt:intake`、`customer:read`、`customer:create`、`catalog:read`、`quote:read`、`quote:write_draft`、`invoice:read` 和 `invoice:write_draft` 类型化权限。读写工具要求对应权限；报价／发票的计算预览使用对应读取权限。创建令牌时拒绝空集合或未知权限。
- 一次性令牌格式包含非机密公司选择器、令牌选择器及至少 256 位随机密钥。选择器为查询设置事务级 RLS 上下文；只有摘要核验通过才授予访问权。迁移同时包含运行时角色授权和原始 SQL RLS 测试。

### MCP 与上传接口

使用官方 Python MCP SDK，将单一 Streamable HTTP 端点挂载在 `/api/v1/mcp`，且位于 SPA fallback 之前，并把 SDK 生命周期接入主应用。不增加单独容器。每个 HTTP 请求都检查 `Authorization: Bearer`；浏览器 cookie 不可作为 MCP 身份凭证。针对首版 localhost 部署配置 Host/Origin 防护。固定令牌方案只作为内网集成，不宣称完整 OAuth 互操作。

所有变更工具接收 `request_id`，返回 `operation_id`、`status`、目标 ID 和适用时的安全网页链接。`status` 区分新创建与重放结果。错误为未认证、权限不足、验证失败、目标缺失／歧义、非草稿状态及同请求 ID 参数变化定义稳定代码；错误不回显密钥或文件字节。M14 AI 提取失败属于带安全 `ai_error_code` 的成功待审核结果，不算提交失败。

| 工具 | 输入与结果 | 必要约束 |
| --- | --- | --- |
| `submit_receipt` | `request_id`、`source`、`language=en|zh`、`files[{filename,mime_type,data_base64}]` → 收件箱 ID、`ai_state`、安全 `ai_error_code`、审阅 URL | `receipt:intake`；M14 验证；1–5 份原件；AI 前先持久提交。 |
| `get_receipt_status` | 收件箱 ID → 待审核／已确认状态、AI 状态／错误和审阅 URL | `receipt:intake`；同公司；不能确认或读出文件内容。 |
| `search_customers` / `get_customer` / `create_customer` | 搜索／详情；创建使用 `CustomerWrite` → 客户 ID／详情 | 读取／创建权限；不模糊自动合并或修改客户。 |
| `search_catalog` / `list_document_templates` / `list_vat_options` | 用于组装单据明细的有界、按公司隔离选项 | `catalog:read`；不臆造价格／税率或修改记录。 |
| `preview_quote` / `create_quote_draft` / `get_quote` / `update_quote_draft` | 现有报价计算／`QuoteWrite` 与 UUID → 后端合计、状态和网页 URL | 报价读／写权限；锁定后只改 `DRAFT`；不变更状态。 |
| `preview_invoice` / `create_invoice_draft` / `get_invoice` / `update_invoice_draft` | 现有发票计算／`InvoiceWrite` 与 UUID → 后端合计、状态和网页 URL | 发票读／写权限；仅 Standard；锁定后只改 `DRAFT`；不签发／发送。 |

可选备用路径新增 `POST /api/v1/agent/receipt-inbox`：令牌认证的 multipart 文件，加相同的 `request_id`、来源和语言，返回与主工具相同的提交结果。Hermes 侧适配器暴露接收原始缓存路径并转发文件流的本机 MCP 工具；只有第 5 步远程传输门禁失败时才安装。不得接收任意远程文件 URL，也不得让 JAI 抓取用户提供的 URL。

## 原子步骤

### Step 1 · 冻结契约并挂载默认关闭的 MCP 壳

- **交付**：最终的 owner 令牌 Pydantic schema 与路由签名、类型化工具输入／输出 schema、MCP SDK 依赖、`/api/v1/mcp` 挂载和主应用生命周期。在业务逻辑前将准确工具名、错误码及第 5 步备用路径触发条件写进双语 M15 文档。第 2 步之前拒绝全部 MCP 调用；不得暴露匿名业务工具。
- **验证**：协议握手／工具发现形状、默认拒绝鉴权、Host/Origin 处理、OpenAPI codegen 无漂移、现有 API/SPA 路由不变。盲审检查挂载子应用时确实启动 session manager，且没有通用路由可匿名访问。
- **DoD**：roadmap §5；实现后写 `review-notes/M15-step1-impl.md`。

### Step 2 · Owner 管理的可撤销 Agent 凭据

- **交付**：令牌迁移／ORM/RLS、只显示一次的创建／列表／撤销 owner API、按权限验证依赖和 EN/ZH 设置 UI。管理使用现有 MFA owner 会话；MCP 每次调用使用 Bearer 验证。
- **验证**：新库及生产形态迁移、runtime-role RLS、数据库只存摘要、明文仅显示一次、owner/MFA 限制、无效／撤销／跨公司令牌拒绝、各权限允许／拒绝矩阵、前端文案／构建与类型生成。盲审检查创建之后的响应、日志、数据库中均无明文令牌。
- **DoD**：roadmap §5；写 `review-notes/M15-step2-impl.md`。

### Step 3 · 共用的事务级幂等与追踪记录

- **交付**：`agent_operation` 迁移／RLS 和 Agent 变更调用共用事务包装。定义规范化参数摘要、唯一请求键、重放响应及同键异参冲突。来源 ID 仅作信息使用，并遮蔽敏感字段。
- **验证**：顺序及并发重复调用、崩溃／回滚故障注入、跨公司及已撤销令牌、成功后目标被删除、审计／日志敏感数据检查。盲审检查操作和业务效果一起提交；重试不产生已提交的孤立动作或重复目标。
- **DoD**：roadmap §5；写 `review-notes/M15-step3-impl.md`。

### Step 4 · 远程 MCP 收据提交和状态工具

- **交付**：有界 Base64 解码、共用 M14 收件事务、持久提交后提取、`submit_receipt` 和 `get_receipt_status`、安全审阅 URL 及重放处理。现有网页收件箱端点行为保持不变。
- **验证**：每种受支持 MIME、1–5 份混合文件、字节／哈希不变、伪造 MIME／文件名、单文件及总大小限制、AI 关闭／错误／超时、存储／数据库写入失败、跨公司查询和重复／并发投递。盲审检查不创建占位／已确认 Expense，不在审计或日志记录原始内容，也不另建一套收件实现。
- **DoD**：roadmap §5；写 `review-notes/M15-step4-impl.md`。

### Step 5 · 真实 Hermes/Telegram 附件路径

- **交付**：可部署的 Hermes MCP 配置和 Telegram 数字发送者／聊天白名单说明；稳定消息／媒体组请求 ID；含 AI 状态与网页链接的回复文案。实际发送 Telegram 图片与 PDF。若任一文件无法可靠地通过远程 MCP 工具传递原始字节及结果，本步实现已规定的本机 stdio MCP 适配器和令牌保护的流式上传路由，并提供安装说明。
- **验证**：图片和 PDF 原始哈希、一笔采购对应一条收件项、重投不重复、未授权发送者／聊天被 Hermes 拒绝、停用／撤销令牌被 JAI 拒绝、手机可打开审阅链接、AI 失败仍产生待审核项。盲审检查适配器不能读取任意主机路径，也不能绕过令牌权限或 M14 校验。
- **DoD**：roadmap §5；写 `review-notes/M15-step5-impl.md`。不得只凭 mock 关闭本步。

### Step 6 · 客户与只读业务上下文

- **交付**：客户搜索／详情／创建，以及有界产品／材料、模板及 VAT 查询。复用现有 schema／服务，存在可能重复客户或缺失／有歧义值时要求 Hermes 先追问。客户创建纳入共用幂等事务。
- **验证**：精确／已有客户与相似名称、仅名称有效创建、缺少必填名称、请求重放、其他公司 ID、分页／结果上限、目录中的税率和价格来自 JAI 而非模型猜测。盲审检查没有暴露客户修改或目录写入。
- **DoD**：roadmap §5；写 `review-notes/M15-step6-impl.md`。

### Step 7 · 报价预览与草稿工具

- **交付**：基于 `QuoteWrite` 及现有报价／定价服务的计算、创建／读取／修改工具。报价创建／修改与操作记录在同一事务提交；锁定并确认 `DRAFT` 后才修改。返回后端合计、报价编号和网页 URL。
- **验证**：客户／字典校验、多明细、折扣／VAT 与后端舍入和网页 API 一致；重放／并发修改；MCP 拒绝修改 `SENT`／`ACCEPTED`／`REJECTED`／`EXPIRED`；无发送／状态／转换工具。盲审检查没有复制定价逻辑，授权不超过冻结范围。
- **DoD**：roadmap §5；写 `review-notes/M15-step7-impl.md`。

### Step 8 · 普通发票预览与草稿工具

- **交付**：基于 `InvoiceWrite` 及现有发票／定价服务的计算、创建／读取／修改工具。创建／修改与操作记录原子提交；只允许 Standard 类型及锁定后的 `DRAFT`，法定编号继续在签发时产生。
- **验证**：未编号草稿、可编辑明细／条款、后端金额／VAT 一致、重放／并发修改、非草稿及非 Standard 拒绝、签发前不影响报表、无发送／签发／贷项／预付款／尾款操作。盲审检查网页和 document-chain 语义不变。
- **DoD**：roadmap §5；写 `review-notes/M15-step8-impl.md`。

### Step 9 · 全量回归、文档与里程碑报告

- **交付**：完整 EN/ZH 部署及 owner/Hermes 操作说明、最终自动化证据、同步更新的双语 roadmap、每步简报，以及含下方完整走查的 `review-notes/M15-report.md`。仅在作者验收后标记 M15 完成。
- **验证**：Ruff、strict mypy、默认及 PostgreSQL 集成测试、迁移／RLS、TS 类型无漂移、前端测试／构建及 EN/ZH key 对称、Docker build；M14 收件箱／AI／确认、客户 CRUD、报价／发票生命周期和 M10 报表回归。盲审检查文档／证据与最终工作树一致、`review-notes/` 未被跟踪、未混入被禁的 Agent 能力。
- **DoD**：roadmap §5；写 `review-notes/M15-step9-impl.md` 和里程碑报告。

## 🟢 部署自测点

使用 `AGENTS.md` 规定的默认 dev Compose 启动，然后让同服务器的 Hermes 连接 JAI。里程碑报告须提供具体命令／配置及以下作者走查步骤。

1. 在 JAI 创建仅有收据权限的应用令牌，配置 Hermes 的 Telegram 数字发送者／聊天白名单和 MCP 连接，再发送图片及 PDF。在 M14 收件箱逐字节核对两份原件、查看 AI 状态，并在手机上打开 Telegram 中的审阅链接。重试／重投一条消息，确认不新增重复条目。
2. 关闭或错误配置收据 AI，再提交一份文件，确认 Hermes 报告具有可恢复 AI 状态的待审核项。只在 JAI 网页修改字段并确认；核对恰好一笔 Expense 及预期报表效果。
3. 让 Hermes 使用目录／模板选项为已有客户起草报价；被追问时补齐价格／税务细节。核对带编号的 `DRAFT` 报价和后端计算的合计，在网页编辑，并确认 Hermes 无法标记发送。
4. 为新客户请求报价；查看匹配候选，没有匹配项时创建客户，再建草稿。重放同一创建请求，确认客户和报价均不重复。
5. 请求普通发票草稿，核对其未编号 `DRAFT` 状态及后端合计，再通过 Hermes 和网页修改。确认没有签发／发送，也没有 VAT 报表效果；非 Standard 单据请求被拒绝。
6. 撤销令牌，确认新 MCP 调用失败。测试未授权 Telegram 发送者／聊天及权限不足的令牌；它们均不能创建已确认 Expense、已发送单据或任何越权草稿。

## 验收结论（里程碑收尾时填写）

- 完成日期：
- 自动化门禁及真实 Hermes 附件路径：
- 作者走查与验收：
- 已知遗留／顺延事项：
