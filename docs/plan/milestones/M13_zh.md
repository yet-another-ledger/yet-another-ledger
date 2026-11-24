# M13 · Document Artifact 历史补传

> 🌐 [English](M13.md) · **中文**

> **状态**：🟢 已于 2026-09-05 完成、验收并冻结。4 个编排实现步骤、盲审/返工循环和自动化收尾门禁均已收敛。作者通过 Standard Invoice Happy Path，并以自动化证据验收未执行的负向及 Advance/Final/Credit Note 人工路径。本文取代了原来未拆步骤的“收尾 / GA 前体检”占位。被移出的运维加固设想继续在[路线图 §4.aa](../roadmap_zh.md) 的已完成里程碑路线之外跟踪。

> **M20 已交付** [M20](M20_zh.md) 已让发票 PDF 的 AI 核验复用 Responses 或 Chat Completions 协议选择。M20 的真实提供商检查和作者走查仍待完成。

## 执行模式

- 按 `AGENTS_zh.md`，默认仍为手动模式。只有作者明确要求 orchestrator mode 或 direct generation 时，才运行自动 implementer/reviewer/fixer 循环。
- 在 orchestrator mode 中，每个原子步骤都由全新 implementer 完成，产出中文实现简报和一个 implementation commit；全新 reviewer 只看到本设计、roadmap、该简报和本步骤 diff。若有 finding，则交给全新 fixer 并产出 `--fixup` commit；复审最多五轮；收敛后按步骤 autosquash。
- OpenAI harness 的默认角色映射为：orchestrator/reviewer 使用 `gpt-5.6-sol` + `xhigh`，implementer/fixer 使用 `gpt-5.6-terra` + `high`。
- 每步简报只放在 `review-notes/M13-step<n>-impl.md`。全部步骤结束后写 `review-notes/M13-report.md`；作者依据该报告进行一次里程碑级 walkthrough。

## 依赖与既有能力

- **M8 AI 基础设施**：已有 owner 配置的 OpenAI-compatible base URL/model/API key、多模态能力探测、PDF 转图片、防御性 JSON 解析和 EN/ZH 解释文本。M13 只复用连接和传输层；收据提示词仍专属于 Expense，不复用。
- **M9 输出**：已有按 locale 解析的 Invoice PDF preview/download、精确响应文件名、邮件附件和安全化输出。
- **M12 正式单据与 artifact**：已开具的 `STANDARD`、`ADVANCE`、`FINAL` 和 `CREDIT_NOTE` 已冻结 party/tax snapshot。`document_artifact` 保留精确 PDF 字节、SHA-256、locale、文件名、reason、render fingerprint 和 renderer version；成功邮件日志关联实际 artifact。
- **当前多版本行为继续有效**：结算、退款、locale 或渲染变化可以合理地产生不同 artifact。M13 不把这段历史压成一行。
- **Roadmap 不变量**：OpenAPI contract-first、金额逻辑只在后端、PostgreSQL RLS、保留字节不可变、生产迁移只增量，以及 EN/ZH 文档同步继续为硬要求。

## 目标与范围

**目标**：允许一张尚无任何保留 artifact 的已开具正式 Invoice，接收一份来自旧 JAI 部署、旧开票系统或外部工具的精确历史 PDF。该上传件成为此 Invoice 当前 locale/展示 fingerprint 下的 canonical output；后续展示状态变化仍可通过 M12 正常管线生成新 artifact。

包含：

- 仅当 Invoice 当前 artifact 数量为零时，为已开具的 `STANDARD`、`ADVANCE`、`FINAL` 或 `CREDIT_NOTE` 提供一个 PDF 上传入口。
- 在既有 `document_artifact` 表中保留精确字节，并使用类型化的 `UPLOAD` creation reason。
- 当 locale 和正式输出 fingerprint 未变化时，普通 Download 和成功 Send 复用上传字节。
- 上传前可选、显式触发的 AI 对照检查。它只展示字段级参考结果，绝不授权、阻止或修改上传。
- 在既有 Invoice artifact 区域加入紧凑的“选择／检查／确认”UI。
- 在 upload、Download 和 Send 之间并发安全地决定首个 artifact。

不包含：

- 已存在任意 artifact 时上传；替换、删除、恢复、选择 primary 或重排的 UI/API。
- artifact 维护 CLI 或受支持的数据库清理命令。
- artifact audit trail、document-chain event、uploader 列、AI 结果落库或 AI validation token。
- 合并、去重或迁移清理既有多 artifact Invoice。
- Refund Confirmation、Quote PDF、payment receipt 或 expense receipt 上传。
- 通过 OCR 导入 Invoice 字段、由 PDF 驱动会计数据变更，或让上传内容与税务/报表数据自动对账。
- 把原先笼统的 wrap-up 工作并入 M13。备份恢复自动化、部署文档扩充和无关 UI 清理放在 roadmap §4.aa。

## 已冻结的产品与架构决策

- [x] **D1 · 零 artifact 门槛**：只有已开具 Invoice 没有任何 `document_artifact` 行时才可上传。任何既有 artifact——无论 reason、locale 或 hash——都会关闭上传路径。
- [x] **D2 · 覆盖全部正式 Invoice kind**：`STANDARD`、`ADVANCE`、`FINAL`、`CREDIT_NOTE` 使用同一上传规则。Refund Confirmation 和非 Invoice 输出保持不变。
- [x] **D3 · 保留既有多版本语义**：M13 不增加“一张 Invoice 只能有一个 artifact”的约束。后续结算、退款、locale 或 renderer 变化仍可保留另一份生成的 artifact。
- [x] **D4 · 上传件是该展示状态的 canonical artifact**：上传时记录 Invoice 当前解析出的 locale 和正式 render fingerprint。相同展示状态下的 Download/Send 返回上传的精确字节；fingerprint 改变后走既有生成 artifact 路径。
- [x] **D5 · Preview 始终实时**：`preview=true` 一律渲染当前 JAI 输出，不创建 artifact，也不替换成上传字节。
- [x] **D6 · 精确不透明字节**：接受的 PDF 永不重写、压缩、安全化改写或重新生成。后端计算其 SHA-256，并由既有数据库完整性 trigger 强制校验。
- [x] **D7 · 仅 PDF 校验**：必须通过声明 MIME、独立 10 MiB 上限、`%PDF` magic、可解析性及至少一页的检查。原始文件名仅是展示元数据，响应/邮件 header 使用安全规范化后的值。
- [x] **D8 · 只用默认 locale**：上传不接收 locale 输入，使用已开具 Invoice 冻结/默认的 locale 解析链。以后明确以另一 locale Download/Send 属于不同展示状态，可以生成新 artifact。
- [x] **D9 · 独立上传设置**：`max_artifact_bytes` 默认 `10 * 1024 * 1024`；修改收据上传上限不会影响正式 artifact 上限。
- [x] **D10 · AI 可选且显式触发**：选择文件不会联系外部模型。用户看到“PDF 将发送到已配置服务”的提示后，必须点击“使用 AI 核对”。
- [x] **D11 · AI 只作参考**：MATCH、WARNING、INCONCLUSIVE、模型失败或未配置 AI 均允许继续人工上传。用户始终负责选择正确原件。
- [x] **D12 · 专用固定提示词**：M13 复用 AI 连接/model/key 设置，但不复用 `receipt_prompt`；也不新增可编辑 artifact prompt。固定输出契约防止设置改变解析语义。
- [x] **D13 · 字段检查表**：AI 检查单据号/kind/日期、供应或预付款日期、卖方、买方、币种、未税总额、VAT 总额和含税总额。每项为 MATCH、MISMATCH 或 NOT_FOUND，并带 expected/observed 文本及可选安全 note。
- [x] **D14 · AI 不落库**：AI 响应与原始模型输出不写入任何表、artifact 元数据、event 或 log。浏览器里重新选择文件会使当前显示结果失效。
- [x] **D15 · 人工确认**：最终 UI 明确警告 PDF 无法通过 JAI 替换或删除；即使 AI 为 MATCH，也必须显式确认。
- [x] **D16 · 同 owner/RLS 边界**：只有已认证 owner 可校验/上传。不存在、跨 company、DRAFT 或 CANCELLED owner 不暴露 artifact 数据，也不能被用来把其他 tenant 的 PDF 发送给 AI。
- [x] **D17 · 首动作并发安全**：上传取得 Invoice 锁，并在写事务中重新检查零 artifact。upload/Download/Send race 串行化，恰好一个动作定义首个展示 artifact；失败的 upload 返回稳定 conflict 且不留半条记录。
- [x] **D18 · 无财务影响**：上传值或 AI 观察值绝不更新 Invoice、line、tax、payment、report 或 party snapshot。模型对照只属于展示层，不执行权威金额计算。
- [x] **D19 · 无清理入口**：极特殊清理仍是产品外的管理员数据库操作。应用 runtime 对 `document_artifact` 继续保持既有无 DELETE RLS posture。
- [x] **D20 · 既有数据不动**：迁移只新增 `UPLOAD` enum 值及配套 contract；不删除、不替既有 artifact 选原件，也不渲染历史 PDF。

## 用户流程与输出规则

### 可用条件

| Invoice 状态 | Artifact 数量 | 上传 UI/API | 结果 |
| --- | ---: | --- | --- |
| `SENT` 或 `COMPLETED`，任意正式 Invoice kind | 0 | 可用 | 可创建一个不可变 `UPLOAD` artifact |
| `SENT` 或 `COMPLETED` | ≥1 | 隐藏 / `409` | 既有历史继续权威有效 |
| `DRAFT` 或 `CANCELLED` | 任意 | 隐藏 / not found | 不接受正式上传 |
| 不存在或属于其他 company | 任意 | 隐藏 / `404` | 不泄漏 identity 或 artifact |

### 上传之后

| 动作 | 展示 identity | 行为 |
| --- | --- | --- |
| Preview | 任意 | 渲染实时 JAI PDF；不保留也不返回上传件 |
| Download | locale + fingerprint 相同 | 返回上传字节和上传文件名 |
| 成功 Send | locale + fingerprint 相同 | 附上上传字节/文件名，并在 `EmailLog` 关联其 artifact ID |
| 失败 Send | locale + fingerprint 相同 | 上传件保持不变；失败日志不带成功 artifact link |
| Download/Send | locale/fingerprint 已变 | 走既有 M12 生成/保留路径；上传件继续可从历史下载 |
| 状态后来回到原 fingerprint | 原 locale + fingerprint | 再次复用上传原件 |

Artifact 列表继续按最新优先排列。`creation_reason=UPLOAD` 是唯一的原始上传标记；不增加独立 primary flag 或排序字段。

## API 契约

### 上传

`POST /api/v1/invoices/{invoice_id}/artifacts`

- 请求：`multipart/form-data`，且恰好一个 `file` part。
- 成功：`201 DocumentArtifactRead`；既有 read model 的 `creation_reason` 新增允许 `UPLOAD`。
- `404`：不存在／跨 company／不是已开具正式 Invoice。
- `409`，`code=ARTIFACT_ALREADY_EXISTS`：已存在至少一个 artifact，或另一个首输出动作赢得 race。
- `422`，`code=INVALID_ARTIFACT_FILE`：MIME 不支持、超过上限、magic 错误、PDF 损坏/零页，或文件名/内容不可用。

### 可选 AI 核对

`POST /api/v1/invoices/{invoice_id}/artifacts/validate-upload?language=en|zh`

- 请求：同一 multipart `file`；不插入任何行。
- 在任何外部调用前完成 eligibility 和文件校验。校验之后仍可能并发出现首个 artifact，因此上传始终重新检查。
- 成功：`200 DocumentArtifactValidationRead`：
  - `file_sha256: str`
  - `status: MATCH | WARNING | INCONCLUSIVE`
  - `confidence: HIGH | MEDIUM | LOW | null`
  - `summary: str`
  - `total_pages: int`
  - `checked_pages: list[int]`，使用从一开始的页码
  - `checks: list[DocumentArtifactValidationCheck]`
- `DocumentArtifactValidationCheck`：
  - `field: DOCUMENT_NUMBER | DOCUMENT_KIND | DOCUMENT_DATE | SUPPLY_OR_ADVANCE_DATE | SELLER | BUYER | CURRENCY | TOTAL_EXCL_VAT | VAT_TOTAL | TOTAL_INCL_VAT`
  - `status: MATCH | MISMATCH | NOT_FOUND`
  - `expected_value: str | null`
  - `observed_value: str | null`
  - `note: str | null`
- `409`，`code=AI_NOT_CONFIGURED` 或 `ARTIFACT_ALREADY_EXISTS`；`422 INVALID_ARTIFACT_FILE`；timeout/provider/parser 失败返回 `502 AI_VALIDATION_FAILED`。这些响应只影响参考核对。

上传请求不携带 AI 结果/token，也不要求曾执行校验。锁定这些契约后重新生成并提交 `frontend/src/api/schema.d.ts`。

## 数据模型与增量迁移

- 向 PostgreSQL `documentartifactreason` 和 Python `DocumentArtifactReason` 添加 `UPLOAD`。与此前 PostgreSQL enum 添加相同，downgrade 不通过破坏性类型重写移除该 label。
- 不新增表或列。既有 Invoice/Refund hash 和 render 唯一约束保持不变。
- 上传行使用：
  - `artifact_kind=FORMAL_DOCUMENT` 和同 company 的 `invoice_id`；
  - 精确 `pdf_bytes` 和后端计算的 `sha256`；
  - 当前正式 `render_fingerprint` 和自动解析的 `locale`；
  - 规范化原文件名，另有受控 PDF fallback；
  - `creation_reason=UPLOAD` 和固定 external-upload renderer 标记。
- 扩展数据库 artifact-owner trigger：`UPLOAD` 插入时锁定所属 Invoice，并在已存在任何 artifact 时拒绝。普通 `DOWNLOAD`/`SEND` 插入继续允许多版本。
- 保留 immutable-update trigger 和 runtime 无 DELETE policy。不添加 uploader ID 或 AI 字段。

## 后端与并发设计

1. 解析同 company Invoice，且不泄漏跨 tenant identity；要求状态为 `SENT` 或 `COMPLETED`。
2. 落库前完整校验文件：最多读取 `max_artifact_bytes + 1`，要求 PDF MIME/magic，用既有 PDF library 打开且至少一页。不创建磁盘临时文件。
3. 在上传事务中，按既有全局顺序锁定 Invoice，重新查询全部 Invoice artifact；非空则返回 `ARTIFACT_ALREADY_EXISTS`。
4. 经由完全相同的 M12 正式输出准备路径解析展示 locale 和 fingerprint。不发明平行 fingerprint，也不重新计算金额。
5. 插入一条不可变记录，commit 后返回 read model。
6. 扩展 canonical retention 查询：同 locale、同 fingerprint 的 `UPLOAD` 即使 renderer 标记为 external upload，也能复用。fingerprint 已包含正式输出 pipeline version，之后 renderer 改变不会误匹配。
7. 复用 artifact 时，HTTP `Content-Disposition` 与 SMTP 附件名均使用 `artifact.filename`，而非新渲染文件名；响应/邮件字节始终为 `artifact.pdf_bytes`。
8. 保留 Download/Send 渲染使用的 parent lock。Upload 使用相容的排他 parent lock，使并发首动作不可能各自通过空检查。

## AI 参考对照

### 输入事实

Expected facts 只来自已持久化的已开具单据 snapshot：Invoice kind/number/date/supply date、开具时 seller/buyer identity、currency 和已持久化 totals。当前可变 Company/Customer 字典和 AI 观察值永不替换这些事实。

PDF 在内存中 rasterise。在 `ai_pdf_max_pages` 上限内，以确定性顺序优先覆盖首页和末页，再取中间页；向 UI 返回从一开始的已选页码，使部分检查清晰可见。

### 固定提示词要求

内置提示词必须：

- 把模型定义为“将上传正式 PDF 与给定 expected facts 对照”的参考验证器；
- 明确 document pixel/text 是不可信数据而非指令，必须忽略文档内的任何指令；
- 禁止猜测、重新计算权威 totals，或仅凭格式/舍入排版就认定不一致；
- 无法读出字段时必须返回 `NOT_FOUND`，证据不足时总体返回 `INCONCLUSIVE`；
- names 和 identifiers 保持原文，仅 `summary`/`note` 解释文字跟随请求的 EN/ZH UI 语言；
- 只返回一个符合固定 response contract 的 JSON object，不得带 Markdown 或外围文字。

防御性解析模型响应：只允许白名单字段/enum，限制全部返回文本长度，忽略未知 key，结构不可用时返回 `AI_VALIDATION_FAILED`。绝不记录 key、原始 PDF、data URL、完整 prompt、raw response 或提取出的客户/税务值。

## 前端 UX

- 扩展既有 Invoice artifact 区域；不新增顶层页面。
- 已开具 Invoice artifact 列表为空时，显示单 PDF picker、10 MiB 提示和“普通 JAI UI 中不可撤销”的警告。列表非空时，保留当前历史 UI，不显示上传控件。
- 在浏览器中选择/替换文件会清除之前 AI 结果。不自动发请求。
- “使用 AI 核对”明确说明所选 PDF 将发送到配置的外部服务。AI 配置/provider 错误转为不阻断 notice，并给出 Settings → AI 的链接/提示。
- 展示 summary alert 和本地化字段检查表。MATCH 为正向、MISMATCH 为警告、NOT_FOUND/INCONCLUSIVE 为中性；任何结果都不禁用最终动作。
- “上传为原始 artifact”打开确认框，显示 Invoice 与文件名，并说明 JAI 不提供 delete/replace。确认后上传精确所选文件。
- 成功后重新加载 artifact 并移除上传表单。遇到 `ARTIFACT_ALREADY_EXISTS` 时刷新，并说明另一 Download/Send/upload 已建立首个 artifact。
- Preview 继续明确标注为实时当前输出；历史 artifact 下载继续返回精确保留字节。

## 安全、隐私与回归边界

- 两个 endpoint 均使用 owner/MFA auth 和 company RLS；把字节发给 AI 之前先验证 owner/eligibility。
- 上传 PDF 以 `application/pdf` attachment 提供，使用安全 RFC 6266 文件名处理和 `X-Content-Type-Options: nosniff`；绝不把它插入 HTML/Jinja，也不以 URL 获取。
- 精确字节保留意味着不能靠重写安全化 active PDF 内容。产品通过避免 inline execution 控制风险，owner 对可信历史来源负责。
- 显式 AI 动作是向配置 provider 传输的同意边界。最终上传不会调用 AI。
- 本里程碑任何 service 均不写 financial、VAT、numbering、settlement、party snapshot 或 report state。
- 既有 Refund Confirmation artifact retention、tombstone、M12 多版本测试、EmailLog 成功/失败语义和跨 company 不披露均为回归门槛。

## 原子实现步骤

### Step 1 · Contract、enum 与校验基础

- **目标**：在改变输出行为前，先锁定增量 contract 和安全文件边界。
- **Contract**：添加 `UPLOAD`、上传/AI response enum 与 schema、route signature 和 `max_artifact_bytes`；重新生成 OpenAPI→TS types。
- **后端/数据**：增量 enum migration；可复用的仅 PDF 字节 validator；只允许针对已开具零 artifact Invoice 插入 UPLOAD 的 trigger 规则。
- **必测覆盖**：fresh/upgrade migration；enum downgrade posture；MIME/size/magic/损坏/零页/边界测试；raw SQL owner/hash/immutability/首上传强制；schema/codegen 检查。
- **盲审点**：不增加表级 artifact 唯一；既有行不动；Refund artifact 不受影响；校验不改写字节，也不把文件名当路径；稳定错误不泄漏跨 company identity。
- **DoD**：roadmap §5 后端门禁、迁移检查和 codegen freshness 全过。写 `review-notes/M13-step1-impl.md`。

### Step 2 · 精确上传与 canonical Download/Send 复用

- **目标**：保留一份历史原件，使其成为当前展示 artifact，同时不取消未来多版本。
- **后端/API**：实现上传 service/endpoint、lock-and-recheck、展示 fingerprint 捕获和上传元数据；Download/Send 复用匹配 UPLOAD 的字节/文件名。
- **必测覆盖**：四种 kind；issued/draft/cancelled/cross-tenant gate；精确 bytes/hash/filename；preview 只实时渲染；同 fingerprint Download/Send 复用；失败 Send；结算/locale/renderer 变化生成新 artifact；状态恢复后重新复用 upload；upload/upload、upload/download、upload/send race。
- **盲审点**：邮件 link 指向实际字节；上传字节不得搭配生成文件名；无平行 fingerprint 逻辑；lock order 不会与既有 payment/refund output 死锁；不修改金额或报表。
- **DoD**：roadmap §5 后端/集成门禁全过。写 `review-notes/M13-step2-impl.md`。

### Step 3 · 可选 AI 参考对照

- **目标**：帮助 owner 发现可能传错文件，同时不把概率性输出变成权威。
- **后端/API**：artifact 专用固定提示词、expected snapshot context、首页/末页 raster 选择、多模态调用、防御性 response parser 和不落库 validation endpoint。
- **必测覆盖**：match/mismatch/not-found/inconclusive fixture；EN/ZH 文字；首页/末页选择；disabled/no-key/no-model；timeout/connect/HTTP/invalid JSON；文档内 prompt injection；无 log/secret/persistence；mock 外部调用前已验证 eligibility。
- **盲审点**：receipt prompt/custom instruction 不能改变该 contract；AI 不批准/阻止上传；expected facts 使用 issue snapshot；模型值不触碰会计；不记录 raw 私密内容。
- **DoD**：roadmap §5 后端门禁全过，所有外部调用均 mock。写 `review-notes/M13-step3-impl.md`。

### Step 4 · 前端流程、回归收尾与里程碑报告

- **目标**：交付边界清晰的 empty-state 流程，并在不混入无关 polish 的前提下关闭 M13。
- **前端**：零 artifact 上传表单、显式 AI 动作/隐私提示、结果检查表、永久操作确认、稳定错误处理和 artifact refresh；完成 EN/ZH 文案以及 accessibility/loading state。
- **回归/收尾**：保留当前 artifact 历史和实时 preview UX；跑完全部后端/前端/集成/迁移/codegen/i18n/Docker 门禁；完成下方部署 walkthrough。
- **必测覆盖**：empty/non-empty/draft state；file change 使 AI 结果失效；AI success/failure/skipped 均可确认；409 refresh；精确文件名下载；production-build click/loading 行为；EN/ZH key symmetry。
- **盲审点**：UI 不宣称 AI 确定性；无 replace/delete 控件；不自动调用外部；前端不计算 expected totals 或决定 artifact eligibility；既有 M12 workflow 仍可用。
- **DoD**：roadmap §5 全部门禁通过。写 `review-notes/M13-step4-impl.md`，再写包含完整 walkthrough 的 `review-notes/M13-report.md`。

## 自动化验收门禁

- 后端：`uv run ruff check .`、`uv run mypy --strict src`、默认及完整 PostgreSQL integration pytest suites。
- 迁移：fresh database、当前生产 head→新 head、downgrade/upgrade posture 和真实 runtime-role RLS matrix。
- Contract：权威 OpenAPI 重新生成，且已提交的 `frontend/src/api/schema.d.ts` 无漂移。
- 前端：unit/component tests、production build 和严格 EN/ZH key symmetry。
- 输出：精确 PDF SHA/filename/email identity、CJK PDF/container smoke 和 M12 artifact/refund regression suites。
- 安全：测试不调用真实 model/network；跨 company 请求绝不向外传字节；credential/log scan 保持干净。

## 🟢 部署自测点

使用 `AGENTS_zh.md` 规定的默认 dev Compose 启动。里程碑报告必须包含以下完整人工步骤：

1. 打开一张无 artifact 的已开具 Standard Invoice，确认出现上传表单；打开 DRAFT 和已有 artifact 的 Invoice，确认不出现。
2. 选择有效历史 PDF，确认点击“使用 AI 核对”前没有外部请求，且 UI 提示 provider 传输。
3. AI 已配置时运行核对，检查总体结果以及号码/日期/双方/币种/金额检查表；确认 warning 不禁用上传。
4. 改为 AI disabled 或故意失败的 provider 再试；确认出现人工复核 notice，且仍可上传。
5. 确认上传。下载历史 artifact，验证 bytes/hash/filename 与所选文件完全一致，并确认上传控件消失。
6. 不改结算状态，使用普通 Invoice Download 和 Send；验证二者使用上传 PDF，且成功 EmailLog 关联该 artifact。
7. 改变相关 payment/refund 展示状态并再次 Download；验证出现新生成 artifact，同时上传原件保持 byte-identical 且仍可下载。
8. 对 Advance、Final 或 Credit Note 的零 artifact 情况执行一次上传，确认行为一致；确认 Refund Confirmation 没有上传 UI。
9. 依据集成测试证据检查 upload 与另一 Download/Send race：一个首 artifact 获胜，失败方不留部分数据。
10. 运行完整自动化验收门禁和 Docker/CJK PDF smoke；确认 contract、i18n、RLS 和既有 M12 artifact 均无回归。

## 验收结论

M13 已于 2026-09-05 完成、验收并冻结。4 个编排步骤交付了范围有界的零 artifact 上传合同、不可变 `UPLOAD` 持久化、Download/Send 精确规范复用、可选建议性 AI 比对和完整的 Invoice Detail 工作流。每轮实现与返工均由全新 reviewer 盲审，全部 review 循环收敛，当前没有未解决 finding。

最终自动化通过 Ruff、117 个源文件的 strict mypy、1243 项默认测试、1074 项 PostgreSQL integration 测试、到 revision 0042 的 fresh 与 production-head 迁移检查、OpenAPI codegen freshness、148 项前端测试、1458 个 EN/ZH key 对称、前端 build、生产 Docker build 和容器内 CJK PDF smoke。隔离的 tmpfs PostgreSQL 容器和临时 cache 均已删除；开发数据库为 walkthrough 保留，生产数据和服务没有被触碰。

作者使用真实旧系统 PDF 完成 Standard Invoice Happy Path：显式 AI 比对、不可变上传、artifact 历史和默认语言的 canonical 复用均按设计工作。来源应用最初导出的字节在 PDF 前包含一段字面 HTTP 响应头；删除这段非 PDF 前缀后，在不改变 PDF payload 的前提下得到有效文件。walkthrough 证据还暴露了 provider 截断（`finish_reason=length`）；共用 transport 现按用途使用不同 completion 预算：票据提取 1024 tokens、artifact 比对 4096、能力探测 512，后续盲审也已零 finding 收敛。

作者因没有合适 fixture，明确跳过其余人工负向路径以及 Advance、Final、Credit Note 变体；自动化套件中的合同、校验、并发、RLS、artifact identity 和回归覆盖被接受为这些路径的里程碑证据。AI 始终明确为可选建议：provider 失败或结论 inconclusive 都不会改变会计数据，也不会阻断经人工复核的有效上传。M13 没有未解决的验收 finding。
