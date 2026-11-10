# M8 · 开支（Expenses，含 AI 票据填单）

> 🌐 [English](M8.md) · **中文**

> 进入本里程碑前 JIT 产出（**已冻结 · 2026-06-13 与作者共定 D1–D16**，含 AI 客户端（httpx 自构造）/ 可配置提示词 / PDF 栅格化三轮追加）。先读 `docs/plan/roadmap.md` §2 全局约束 + M8 那一格；分析文档权威章节：§7.1.B（收款与开支 v1 圈定）、§7.4.1（进项净额+税额分存）、§7.4.2 / §7.4.7（进项侧 VAT 类别与申报去向）、§7.4.4（每笔开支需存的字段）、§3.5（三层设置）、§3.6（文件/存储/定时任务）。
>
> **本里程碑取向**：给系统加一条**与单据线完全解耦的开支线**——挂 `company_id` 的 `expense` 实体（进项净额 + 税额分存、是否可抵扣）、磁盘 storage 抽象上的收据上传、APScheduler 驱动的周期性开支自动生成、以及一条**视觉大模型票据识别管道**（拍票据 → 预填 → 人工确认才入库）。**算钱在后端、单一本位币**（同 M5/M7）。

> **已规划后续（2026-11-10）** [M19](M19_zh.md) 将扩展共享采购识别提示词和响应，为 Expense 编辑页及收件箱提供校验后的 VAT 类型建议。该设计明确人工选择保护，以及票面 VAT 与反向征税申报名义税率的分离。设计已确认，待开发。

## 进入前已就位的相邻能力（agent 动手前先认，别重造）
> 这些都是 M1–M7 已落地、M8 直接复用 / 镜像的东西。每条都给了文件路标。

- **进项 VAT 类别（M4 已种子化，直接复用，不另起枚举 · 红线 12）**：`models/vat.py::VatTreatment` + `services/vat.py::_TREATMENT_SEEDS` 已种好 **4 条 PURCHASE 侧** treatment，带 `effect` 与 `deductible` 提示列：
  - `NL_DOMESTIC_PURCH`（`APPLY_RATE`, `deductible=True`）→ 国内正常进项（M10 → 5b）；
  - `EU_B2B_REVERSE_PURCH`（`ZERO_REVERSE`, `requires_icp=True`, `deductible=True`）→ 欧盟内采购自核（M10 → 4b 销项 + 5b 进项对冲，§7.4.7）；
  - `EU_B2C_PURCH`（`APPLY_RATE`）；
  - `IMPORT_NON_EU`（`APPLY_RATE`）→ 欧盟外进口（M10 → 4a）。
  - `report_box` 仍为 NULL（M10 与作者共定后才填）；M8 只**存对**类别 + 净额 + 税额，**不做申报聚合**（聚合在 M10）。
- **可抵扣默认来源（M4 已建列）**：`models/dictionary.py::ExpenseCategory.default_deductible`（nullable bool）→ 每笔开支 `deductible` 的默认值来源（可逐笔覆盖）。
- **税率字典**：`models/vat.py::VatRate`（`percent` `NUMERIC(6,3)`，用户可改）。
- **收据存储 ≠ `binary_asset`**：`models/binary_asset.py` 是 512 KB 上限的 **bytea**（logo 用），其 docstring 已明写「**收据走 M8 的磁盘存储 + storage 抽象**」；roadmap M8 也写「本地存储 + storage 抽象」。**本里程碑新建磁盘 storage 抽象 + `expense_attachment` 元数据表，不塞 `binary_asset`。** 上传校验镜像 `services/assets.py`（MIME 白名单 + 体积上限 + 路径安全，红线 7）。
- **APScheduler（M6 已立）**：`main.py::lifespan` 里单个 `AsyncIOScheduler`，已挂 `_expire_quotes_job`（cron，调 `services/quote.expire_due_quotes_all`，自身不 commit 由 job 包事务）。**周期性开支生成 = 在同一 scheduler 上加第二个 cron job**，镜像这套写法；config flag 见 `config.py::scheduler_enabled` / `scheduler_expire_quotes_hour`。
- **三层设置 + 凭证存储范式（M1/M2 已立）**：`services/settings.py`（`get_setting`/`set_setting`，`USER→COMPANY→GLOBAL` 回退 + 进程内缓存）。**SMTP 凭证范式**（`schemas/setting.py::SmtpSettings`/`SmtpSettingsRead` + `api/settings.py` 的 `GET/PUT /settings/smtp` + `services/email.py::_get_smtp_config` 的「DB 设置 → env 兜底」）= **AI 凭证完全照搬的母版**（明文存 GLOBAL JSONB、读时打码、env 兜底）。
- **货币舍入（M0/M7.5 已立）**：`services/money.py::quantize_money`（3 位中间精度）/ `quantize_to_minor_unit`（2 位 / 分，文档级）。开支金额是**直接录入的到分金额**（票据/AI 给的就是含分的净额/税额），不走 M5 那套行→折扣→计税流水线；落盘按分（`quantize_to_minor_unit`），列仍 `NUMERIC(18,3)`。
- **镜像母版**：实体/服务/路由分层与 cascade 对齐 `models/invoice.py` + `models/payment.py`（`_MONEY = Numeric(18,3)`、`company_id` `RESTRICT`、子表 `CASCADE`、字典 FK `SET NULL` + name 快照、`creator_id` `SET NULL`）；列表/分页/过滤镜像 `services/payment.py::list_payments` 与 `services/invoice.py`；迁移写法对齐 `backend/alembic/versions/` 里 M6.5/M7 那几条 additive 迁移。

## 执行模型（两种实现方式 · 协议固化在 CLAUDE.md，本文不重复）
> 同 `M7.md` 顶部那节：① Opus orchestrator → 干净 Sonnet implementer（空对话）→ 中文实现简报 → 干净 Opus 盲审 → Sonnet fixer 循环到无 finding → per-step autosquash → 里程碑末出报告供作者人工 walkthrough；② 作者人工实现每步 + agent review。**默认人工模式**，作者明确点名才跑 orchestrator。
> **本文对两种方式的保证**：每个原子步骤**自包含**——干净 agent 只靠「本文档 + 仓库（CLAUDE.md / 记忆 / 既有代码）+ 作者口头几句」即可落地，每步给清**目标 / 契约 / 要镜像的既有文件 / 不变量 / 必覆盖测试 / 盲审要点**；逐步安全网 = 自动化测试 + 盲审，人工 walkthrough 收敛到**里程碑末一次**（见文末「🟢 部署自测点」）。

## 目标与范围
- **目标**：能记一笔开支（进项净额 + 税额分存、VAT 处理类别、是否可抵扣）；上传/预览/下载票据；按周期自动生成固定成本（生成为草稿待确认）；拍一张票据由视觉大模型预填字段、人工确认后入库。**全程算钱在后端、单一本位币。**
- **依赖**：M2（公司本位币）+ M4（`expense_category` / `payment_method` 视需要 / `vat_treatment` PURCHASE 侧 / `vat_rate` / 币种）。**不依赖发票/报价/收款**——roadmap §4 的「开支独立线」，与单据线 M5/M6/M7 并行；本项目 M5–M7 均已完成，M8 现在直接开工。
- **纳入（IN）**：
  - **Expense 实体 + CRUD + 列表**：挂 `company_id`（红线 2）；分类（M4，FK `SET NULL` + name 快照）；供应商（v1 = 自由文本 `supplier_name`）；**净额 + 进项税额分存**（§7.4.1，两者皆为权威录入值，`gross = net + vat` 后端算）；VAT 处理类别（PURCHASE 侧 `vat_treatment`，FK `RESTRICT` + code/label/effect 快照）+ 税率（`vat_rate`，FK `RESTRICT` + percent/label 快照）；**是否可抵扣**（默认取分类 `default_deductible`，可逐笔覆盖）；开支日期；`note`（`text`）；`reference`（`text`，票据号）；`is_draft`（周期生成用）。**单一本位币**：`currency = 公司本位币`、`exchange_rate = 1`、`base_* = 原值`。
  - **收据上传（磁盘 storage 抽象）**：新建 `services/storage.py`（`Storage` 抽象 + `LocalStorage` 实现，配置化根路径 + 体积上限 + MIME 白名单，路径不可穿越）+ `expense_attachment` 元数据表（`CASCADE` 于 expense）；上传 / 列出 / inline 下载 / 删除。**storage 抽象为 M9 PDF 落盘共用预留**。
  - **周期性开支**：`recurring_expense` 模板 + 周期（MONTHLY/QUARTERLY/YEARLY）→ APScheduler 定时**幂等克隆**出 `is_draft=true` 的开支（复用 M6 scheduler）；可暂停（`active`）、可设上限（次数或截止日）；提供「立即生成」手动触发端点（便于 walkthrough / 补生成）。
  - **⭐ AI 票据智能填写（OpenAI 兼容 / Chat Completions）**：`services/ai.py` 视觉管道走 **OpenAI 兼容的 Chat Completions 接口**——`base_url` / `api_key` / `model` / `receipt_prompt` **全部用户在设置里自填**（可指 OpenAI / OpenRouter / 本地 vLLM 等任意兼容端点），客户端用 **`httpx`（async）自构造请求、不用厂商 SDK**（直接 `POST {base_url}/chat/completions` + 多模态 `image_url`）。**不写死任何 provider / 模型 id。** **同步调用**；`POST /expenses/ai-extract`（对一张**已上传到 storage 的票据**调用）→ 返回 `ExpenseAIPrefill`（净额/税额/税率/供应商/日期/分类建议）**不落库**，人工在编辑器确认后才存正式 expense；**失败 / AI 关闭 → 优雅回退手填**。**输入统一成图**（D16）：图片直接发；PDF（电子版/扫描版）后端用 `pypdfium2` 栅格化成图再发——三种情况一条路径。凭证走 M2 三层设置（**照搬 SMTP 范式**：GLOBAL 明文 JSONB + 读时打码 + env 兜底，红线 5/9）+ 「启用 AI」开关；**提示词可改**（默认 + 用户覆盖）；**凭证绝不进日志 / 不进任何响应体**。
  - **AI 连通性 + 多模态测试**：`POST /settings/ai/test`（**镜像 `POST /settings/smtp/test`**）——后端拿配置（已存或请求体临时传入）**实际发一条带小图片的 Chat Completions 探针**，确认 ① 连得通 / 鉴权过；② 该模型**接受图片输入（多模态）**；返回 `{ ok, multimodal, detail }`。前端设置页放「测试」按钮。
  - **前端**：开支列表（过滤）/ 编辑器 / 收据预览（原图 `<img>` / PDF 内嵌）/ 周期管理 / 「拍票据自动填」入口；齿轮设置面板新增 **AI 分类**（填 `base_url`/`model`/`api_key` + 启用开关 + **「测试」按钮**，复用 M2.5 预留的「未来 AI 扩展位」）；i18n EN/ZH。**前端不本地权威算钱**（`gross` 等取后端）。
- **不纳入（OUT / 顺延）**：
  - **⭐ AI 供货价单识别 → 灌 M4 产品目录**（follow-on）→ **M8 主线收尾后单列一轮**（作者 2026-06-13 决策）。复用本里程碑视觉管道，按 SKU upsert 进 `product`（[[product-sku-upsert]]）；**不计入 M8 验收**，文末「follow-on」节给指针。
  - **at-rest 加密凭证** → 不做（照搬 SMTP 明文范式；仓库无 crypto 基建，保持同构）。
  - **服务端缩略图** → 不做（serve 原图，前端渲染预览）。注：`Pillow` 会因 PDF 栅格化（D16）引入，但**不用于生成缩略图**。
  - **PDF 原生输入大模型 / 抽 PDF 文字层喂模型** → 不做。OpenAI 兼容 Chat Completions 的 `image_url` 只吃图片，原生 PDF 是各家私有扩展（绑 provider，违背 base_url 任意兼容端点的前提）；抽文字层只对电子版 PDF 有效、且打乱票据布局。**改为后端栅格化成图统一走 image_url**（见 D16，已纳入 IN）。
  - **多币种开支 / 开支侧 FX / 收款日汇率** → 顺延（同单据线单一本位币口径，§7.4.5 现金口径后置）。`base_*` 列与 `exchange_rate` 列建好留位，additive 即可。
  - **部分可抵扣百分比**（招待费等部分抵扣，§7.4.4）→ 顺延（v1 `deductible` 为布尔）。
  - **进项 VAT 申报聚合**（2a/4a/4b/5b）→ M10（M8 只把净额/税额/类别/可抵扣**存对**）。
  - **供应商作为独立实体 / 挂 M3 客户** → 顺延（v1 供应商 = 自由文本）。
  - **银行流水自动导入开支** → vNext（roadmap §4.x）。
  - **开支复制** → 顺延（roadmap 标 🔜）。
- **对应文档**：roadmap M8 / §2（红线 **1 / 2 / 3 / 5 / 7 / 9 / 10 / 12**）；分析文档 §7.1.B / §7.4.1 / §7.4.2 / §7.4.4 / §7.4.7 / §3.5 / §3.6。

## 本轮拟定的产品与技术决策（动手前已与作者共定 · 2026-06-13）
- [x] **D1 · AI 凭证存储 = 照搬 SMTP 范式**：`SETTING_KEY_AI = "ai"`，GLOBAL 级，`AiSettings { enabled: bool, base_url: str, api_key: str, model: str }` 明文存 JSONB；读时 `AiSettingsRead { enabled, base_url, api_key_set: bool, model }` 打码（只 `api_key` 隐藏）；`services/ai.py` 走「DB 设置 → env 兜底（`config.ai_base_url` / `ai_api_key` / `ai_model` / `ai_enabled`）」。**无 at-rest 加密**（与现有 SMTP 同构）。`base_url` 默认 `https://api.openai.com/v1`，用户可覆盖。
- [x] **D2 · 收据 = 磁盘 storage 抽象，不做缩略图**：新建 `services/storage.py`（`LocalStorage`，根 `config.storage_root`，体积上限 `config.max_receipt_bytes`，MIME 白名单 = 图片 + PDF），`expense_attachment` 存元数据 + storage key；**不入 `binary_asset` bytea**；不生成缩略图（serve 原文件）。与 M9 PDF 共用 storage 接口。
- [x] **D3 · 周期性开支生成为草稿待确认**：定时 job 克隆出 `is_draft=true` 的 expense，用户在列表确认/补票据后转正（`is_draft=false`）。**幂等**（同一周期只生成一次）、可暂停、可设上限（次数 / 截止日）。
- [x] **D4 · AI 价单 follow-on 单列一轮**：M8 主线（开支 CRUD + 收据 + 周期 + AI 票据填单 + 前端收尾）先收口、过验收；价单→目录识别复用 AI 管道，作为 M8 收尾后独立的一轮（不计入 M8 验收）。
- [x] **D5 · AI 调用形态 = 同步 + 预填不落库 + 失败回退手填**：`POST /expenses/ai-extract` 同步调视觉模型，返回 `ExpenseAIPrefill` 仅供前端回显；用户确认后走正常 `POST /expenses` 才入库。AI 关闭 / 调用失败 → 返回明确信号，前端回退纯手填。
- [x] **D6 · AI 客户端 = OpenAI 兼容 Chat Completions，用 `httpx` 自构造、不用任何厂商 SDK（作者 2026-06-13 定）**：**不用 `anthropic`、不用 `openai` SDK、不用 `requests`（同步会阻塞 async）、不写死任何 provider/模型**；`base_url`/`api_key`/`model` 全部用户在设置里自填（可指 OpenAI / OpenRouter / 本地 vLLM 等）。客户端直接 `httpx.AsyncClient` `POST {base_url}/chat/completions`，请求/响应用自定义小 Pydantic 模型；`httpx` 从 dev 提升为后端**主依赖**。**调用必须可注入 / 可 mock**（`respx`/`MockTransport`），单测不打真网络。多模态走 `image_url`（base64）——**图片直接发、PDF 后端栅格化成图再发**（见 D16），三种票据一条路径。理由：调用面极小且稳定、async-native、provider-agnostic = HTTP 契约而非 SDK、少一个会 churn 的重依赖。
- [x] **D14 · AI 设置带连通性 + 多模态测试**（作者 2026-06-13 定）：`POST /settings/ai/test` 镜像 SMTP test——后端用配置 `httpx` 实发一条带小图片的 Chat Completions 探针，回 `{ ok, multimodal, detail }`，确认连得通 + 模型接受图片输入；前端设置页有「测试」按钮。**测试探针的图片是内置极小图（不外发用户数据），key 不入响应/日志。**
- [x] **D15 · 提示词可配置（默认 + 用户覆盖 · 作者 2026-06-13 定）**：`AiSettings.receipt_prompt`（GLOBAL，用户可在设置里改），空 → 回落代码常量 `DEFAULT_RECEIPT_PROMPT`（含荷兰票据/BTW 语境）。**用户改的是「抽取指令」；输出 JSON 字段契约由系统固定追加、不可编辑**（`_OUTPUT_CONTRACT` footer），保解析稳健；解析器全程容错（`ExpenseAIPrefill` 全字段可空）。模型只回**分类文本名**，服务端 best-effort 按名匹配本公司 `category_id`；VAT treatment 不让模型猜（留用户、默认 `NL_DOMESTIC_PURCH`）。无迁移（走既有 `setting` JSONB）。
- [x] **D16 · PDF 票据 = 后端栅格化成图，统一走 image_url（作者 2026-06-13 定 · 反转早前「PDF→AI 顺延」）**：`ai-extract` 把输入**统一规整成图片**再喂模型——`image/*` 直接用；`application/pdf` 用 **`pypdfium2`**（自带预编译 wheel、**无系统 poppler**、BSD/Apache 许可）逐页 `render()` → `Pillow` 编码 PNG/JPEG → base64 进 `image_url`（多页各一个 part，页数上限 `config.ai_pdf_max_pages` 默认 3，渲染 scale/DPI 可配）。**电子版 PDF 与扫描版 PDF 一视同仁**（扫描版栅格化即取回那张扫描图）。**理由**：image_url 是唯一 provider-agnostic 的路径；模型「看图」比抽 PDF 文字层准且不丢布局；一条路径/一个提示词/一个解析器。新增依赖 `pypdfium2` + `Pillow`（纯 wheel、无系统包；不选 `pdf2image`[要 poppler]/`PyMuPDF`[AGPL]）。真正不支持的 MIME → `422`。
- [x] **D7 · 进项 VAT 复用 `vat_treatment`（PURCHASE 侧）+ `vat_rate`**：不另起开支侧枚举（红线 12）。treatment FK `RESTRICT` + 快照 code/label/effect；rate FK `RESTRICT` + 快照 percent/label（镜像 `invoice`）。
- [x] **D8 · 净额 + 税额皆权威录入、不强制 `vat == net × rate`**：真实票据逐行各自舍入、汇总后常与 `net×rate` 差几分；故 net、vat 两者都按票面**原样存**（票据/AI 给的就是到分值），后端只算 `gross = net + vat`、按分量化，**不做等式校验**（`vat_rate` 仅作申报分类/展示参照，非计算驱动）。
- [x] **D9 · 可抵扣默认取分类、可逐笔覆盖**：`ExpenseInput.deductible` 缺省 → 取 `expense_category.default_deductible`（再缺省 → `true`）；显式传则以传入为准。
- [x] **D10 · 单一本位币**（同 M5/M7）：`currency = 公司本位币`、`exchange_rate = 1`、`base_net/base_vat/base_gross = 原值`；非本位币金额后端拒绝（`422`）。FX 留 additive。
- [x] **D11 · 金额一律 `Decimal`/`NUMERIC(18,3)`、落盘到分**（红线 1 + M7.5）：录入的 net/vat 进库前 `quantize_to_minor_unit`（EUR=2 位）；`gross = quantize_to_minor_unit(net + vat)`。**算钱逻辑必须单测。**
- [x] **D12 · 分类 FK `SET NULL` + name 快照；删开支级联删附件 + 磁盘文件**（红线 3）：`expense.category_id` `SET NULL`（nullable，create 时业务要求必填，删字典项后靠 `category_name` 快照保历史）；`expense_attachment.expense_id` `ON DELETE CASCADE`，且**删 expense 时 service 负责把磁盘文件一并删掉**（DB cascade 只清元数据行）。
- [x] **D13 · AI 设置 UI 进齿轮面板**：复用 M2.5 预留的「未来 AI 扩展位」（齿轮 → AI 分类），不新开入口。

## 契约（先行 · 前后端各自对着写）
> 业务端点一律 `/api/v1/*`。改契约就 `npm run codegen` 重生成 `schema.d.ts`，CI drift 关强制无漂移（红线 11）。沿用 M1 cookie 会话 + `current_mfa_user`；owner-only 复用 `api/payments.py::_owner_only` / `api/invoices.py::_owner_only` 同款。所有写端点 `company_id` 由 service 注入，绝不从请求体取。

**开支 CRUD / 列表（步骤 1）**
- `POST /api/v1/expenses` body `ExpenseInput` → `201 ExpenseRead`（非本位币 → `422`；分类/treatment/rate 跨公司 → `404`/`422`）。
- `GET /api/v1/expenses` query `{q?, category_id?, vat_treatment_id?, deductible?: bool, is_draft?: bool, date_from?, date_to?, limit?=50, offset?=0, sort_by?: "expense_date"|"created_at"="expense_date"}` → `200 ExpenseListResponse {items: ExpenseListItem[], total}`。
- `GET /api/v1/expenses/{id}` → `200 ExpenseRead`；跨公司 → `404`。
- `PUT /api/v1/expenses/{id}` body `ExpenseInput` → `200 ExpenseRead`（含把 `is_draft` 由 true 改 false 来「确认」周期草稿）。
- `DELETE /api/v1/expenses/{id}` → `204`（级联删 `expense_attachment` 行 + service 删磁盘文件）。

**收据附件（步骤 2）**
- `POST /api/v1/expenses/{id}/attachments`（`multipart/form-data`，字段 `file`）→ `201 ExpenseAttachmentRead`（MIME/体积不合 → `422`；跨公司 expense → `404`）。
- `GET /api/v1/expenses/{id}/attachments` → `200 ExpenseAttachmentListResponse`。
- `GET /api/v1/attachments/{attachment_id}/content` → `200` 原文件流（`Content-Type` = 存的 MIME，`Content-Disposition: inline`）；跨公司 → `404`。
- `DELETE /api/v1/attachments/{attachment_id}` → `204`（删 DB 行 + 磁盘文件）。

**周期性开支（步骤 3）**
- `POST /api/v1/recurring-expenses` body `RecurringExpenseInput` → `201 RecurringExpenseRead`。
- `GET /api/v1/recurring-expenses` query `{active?: bool, limit?, offset?}` → `200 RecurringExpenseListResponse`。
- `GET /api/v1/recurring-expenses/{id}` → `200 RecurringExpenseRead`；跨公司 → `404`。
- `PUT /api/v1/recurring-expenses/{id}` body `RecurringExpenseInput` → `200 RecurringExpenseRead`（含 `active` 暂停/恢复）。
- `DELETE /api/v1/recurring-expenses/{id}` → `204`（**不**级联删已生成的历史开支——它们是独立账目）。
- `POST /api/v1/recurring-expenses/{id}/run-now` → `200 { generated: int, next_run_date }`（手动触发一次到期生成，便于 walkthrough；幂等同定时 job）。

**AI 票据填单 + 设置（步骤 4）**
- `POST /api/v1/expenses/ai-extract` body `{ attachment_id: uuid }` → `200 ExpenseAIPrefill`（对已上传票据识别——**图片直接用，PDF 后端栅格化成图**，D16）；真正不支持的 MIME → `422`；AI 关闭 / 无 key → `409`/明确错误码；模型失败/超时 → `502` + 可回退信号；跨公司附件 → `404`）。
- `GET /api/v1/settings/ai` → `200 AiSettingsRead`（**打码**：只回 `enabled` / `base_url` / `api_key_set` / `model`）。
- `PUT /api/v1/settings/ai` body `AiSettingsUpdate { enabled?, base_url?, api_key?, model? }` → `200 AiSettingsRead`（`api_key` 省略=保留原值，空串=清除；镜像 `SmtpSettingsUpdate` 语义）。
- `POST /api/v1/settings/ai/test` body `AiSettingsUpdate`（省略字段回落已存配置）→ `200 AiTestResult { ok: bool, multimodal: bool, detail: str }`（**镜像 `POST /settings/smtp/test`**；后端用内置极小图发一条 Chat Completions 探针，确认连通 + 多模态；**响应/日志不含 key**）。

**核心 schema 形状**（落 `schemas/expense.py` + `schemas/setting.py`）
- `ExpenseInput { expense_date: date, category_id: uuid, supplier_name?: text, vat_treatment_id: uuid, vat_rate_id: uuid, net_amount: Decimal(≥0), vat_amount: Decimal(≥0), deductible?: bool, reference?: text, note?: text }`
  - 校验：`net_amount`/`vat_amount` ≥ 0；`reference`/`note`/`supplier_name` 是 `text`（红线 10）。**只收原始输入，不收 `gross`/`base_*`/`is_draft`/快照字段**（算钱与快照在后端，红线 1）。
- `ExpenseRead { id, expense_date, category_id?, category_name?, supplier_name?, vat_treatment_id?, vat_treatment_code, vat_treatment_label, vat_treatment_effect, vat_rate_id?, vat_rate_percent, vat_rate_label, net_amount, vat_amount, gross_amount, deductible, currency, exchange_rate, base_net_amount, base_vat_amount, base_gross_amount, reference?, note?, is_draft, recurring_expense_id?, attachment_count, created_at, updated_at }`。
- `ExpenseListItem { id, expense_date, category_name?, supplier_name?, net_amount, vat_amount, gross_amount, deductible, is_draft, attachment_count }`。
- `ExpenseListResponse { items: ExpenseListItem[], total }`。
- `ExpenseAttachmentRead { id, expense_id, filename, mime_type, byte_size, created_at }`（**不暴露 storage key / 磁盘路径**）。
- `ExpenseAttachmentListResponse { items: ExpenseAttachmentRead[] }`。
- `RecurringExpenseInput { name: text, category_id, supplier_name?, vat_treatment_id, vat_rate_id, net_amount, vat_amount, deductible?, note?, frequency: "MONTHLY"|"QUARTERLY"|"YEARLY", start_date: date, end_date?: date, max_occurrences?: int, active?: bool=true }`。
- `RecurringExpenseRead { ...input 回显..., id, next_run_date, occurrences_generated, last_generated_at?, active, created_at, updated_at }`。
- `RecurringExpenseListResponse { items, total }`。
- `ExpenseAIPrefill { expense_date?: date, supplier_name?: str, net_amount?: Decimal, vat_amount?: Decimal, vat_rate_percent?: Decimal, suggested_category_name?: str, suggested_category_id?: uuid, raw_model_note?: str, confidence?: str }`（**全可空** —— 模型识别不出的留空，前端能填多少填多少；不含任何凭证。模型回**分类文本名** `suggested_category_name`，服务端 best-effort 按名匹配出 `suggested_category_id`；VAT treatment 不在 prefill 里，留用户选）。
- `AiSettings { enabled: bool=false, base_url: str="https://api.openai.com/v1", api_key: str="", model: str="", receipt_prompt: str="" }`（GLOBAL，明文 JSONB；`receipt_prompt` 空→回落 `DEFAULT_RECEIPT_PROMPT`）；`AiSettingsRead { enabled, base_url, api_key_set: bool, model, receipt_prompt }`；`AiSettingsUpdate { enabled?, base_url?, api_key?, model?, receipt_prompt? }`；`AiTestResult { ok: bool, multimodal: bool, detail: str }`。镜像 `SmtpSettings`/`SmtpSettingsRead`/`SmtpSettingsUpdate`。

## 算钱与分存规则（M8 钉死 · `services/expense`）
> schema 只校验形状，所有金额/快照/默认值由 service 算。与 M5 计税引擎**无关**（开支不重算税，净额/税额按票面存）。

- **分存（§7.4.1）**：`net_amount`、`vat_amount` 两者皆为**权威录入值**，进库前各自 `quantize_to_minor_unit`（EUR=2 位，D11）。**不校验 `vat == net × rate`**（D8）。
- **合计**：`gross_amount = quantize_to_minor_unit(net_amount + vat_amount)`。
- **可抵扣默认（D9）**：`deductible` 缺省 → `expense_category.default_deductible`，再缺省 → `True`；显式传以传入为准。
- **快照（D7/D12）**：落库时快照 `category_name`（FK `SET NULL`）、`vat_treatment_code/label/effect`（FK `RESTRICT`）、`vat_rate_percent/label`（FK `RESTRICT`）——删字典项不破坏历史。
- **单一本位币（D10）**：`currency = company.base_currency`、`exchange_rate = 1`、`base_net_amount=net_amount`、`base_vat_amount=vat_amount`、`base_gross_amount=gross_amount`；请求若隐含非本位币 → `422`。
- **校验**：`net/vat ≥ 0`；`category_id`/`vat_treatment_id`/`vat_rate_id` 必须属本公司且 `vat_treatment.side == PURCHASE`（销售侧 treatment 用于开支 → `422`）。

## storage 抽象（M8 钉死 · `services/storage.py` · 与 M9 共用）
> 红线 7（路径不可穿越 / 输入清洗）+ 红线 9（不开 inbound 控制入口）。

- **接口**（最小集，M9 PDF 复用）：`save(namespace: str, key: str, content: bytes, content_type: str) -> str`（返回不透明 storage key）/ `open(key) -> bytes`（或流）/ `delete(key)` / `exists(key)`。
- **`LocalStorage` 实现**：根 = `config.storage_root`（默认容器内 `/data/storage`，dev 默认 `./var/storage`，**与 DB/前端构建产物分卷**）；布局 `receipts/{company_id}/{expense_id}/{attachment_id}.{ext}`；写盘前**规范化并校验路径在根之下**（拒绝 `..` 穿越）；文件名只用受控 UUID + 白名单扩展名，**不信任客户端 filename**（仅作展示存 DB）。
- **上传校验**（镜像 `services/assets.py`）：MIME 白名单 = `image/png`、`image/jpeg`、`image/webp`、`application/pdf`；体积上限 `config.max_receipt_bytes`（默认 10 MB）；按**实际 magic bytes / Content-Type** 双重把关，拒绝伪装。
- **删除一致性**：删 attachment / 删 expense 时，service 在删 DB 行的同一逻辑里调 `storage.delete(key)`；磁盘删失败要记日志但不阻塞 DB 事务（孤儿文件可由后续清理兜底，优先保 DB 一致）。

## AI 票据管道（M8 钉死 · `services/ai.py` · OpenAI 兼容 Chat Completions）
> 红线 5（凭证类型化设置）+ 红线 9（不开自更新/控制入口）+ 红线 7（票据图当不可信输入）。

- **客户端（D6）= `httpx`（async），不用任何厂商 SDK**：直接 `POST {base_url}/chat/completions`（OpenAI 兼容契约），body 里 messages 带 text + 多模态 `image_url`（base64 data URL），读 `choices[0].message.content`。**`base_url`/`api_key`/`model` 全来自配置，不写死任何 provider/模型 id。** 我们自己定义**请求体 + 响应子集的小 Pydantic 模型**（mypy 友好）；**HTTP 调用可注入/可 mock**（service 接收一个 `httpx.AsyncClient` 或工厂），单测用 `respx`/`MockTransport`、**绝不打真网络**。超时/错误映射自己控（连接失败→`502`+回退信号）。**理由**：调用面极小且稳定、async-native（httpx 已在栈内）、provider-agnostic = HTTP 契约而非 SDK、少一个会 churn 的重依赖。
- **凭证（D1）**：`_get_ai_config(session)` = DB `SETTING_KEY_AI` → env 兜底（`config.ai_base_url`/`ai_api_key`/`ai_model`/`ai_enabled`），镜像 `services/email.py::_get_smtp_config`。**key 绝不进日志、绝不进任何响应体 / `ExpenseAIPrefill` / `AiTestResult`**；`AiSettingsRead` 只回 `api_key_set: bool`。
- **提示词（D15 · 可配置）**：最终发给模型的用户消息文本 = `(settings.receipt_prompt or DEFAULT_RECEIPT_PROMPT)` + **系统固定追加的输出契约 footer**（`_OUTPUT_CONTRACT` 常量，枚举我们解析依赖的 JSON 键，要求「只回一个 JSON 对象、识别不出的键省略」）。**用户能改的只是抽取指令部分，输出 JSON 契约不可编辑**——这样改 prompt 不会带崩解析。`DEFAULT_RECEIPT_PROMPT` 是仓库里的常量（含荷兰票据/BTW 语境提示）。可选：端点若支持则带 `response_format={"type":"json_object"}`，但不硬依赖（解析本就容错）。
- **输入规整成图（D16）**：`_attachment_to_images(attachment, bytes) -> list[image_bytes]`——`image/*` 直接 `[bytes]`；`application/pdf` 用 `pypdfium2` 逐页 `render(scale=…)` → `Pillow` 编码（最多 `config.ai_pdf_max_pages` 页）；其它 MIME → `422`。**电子版/扫描版 PDF 同一处理**。
- **抽取流程（D5）**：`extract_from_attachment(session, company_id, attachment_id)` → 取本公司附件（跨公司 `404`）→ 若 AI `enabled=false`/无 key → 明确错误（前端回退手填，不抛 500）→ 读 storage 文件 → `_attachment_to_images`（非图非 PDF → `422`）→ 每张图 base64 装进一个 `image_url` part + 上面的有效提示词 → 调模型 → **容错解析**（剥 code fence、`json.loads`、各字段 best-effort 转 `Decimal`/`date`，未知键忽略，**全字段可空**）→ 服务端按名字 best-effort 把模型返回的分类文本匹配到本公司 `expense_category.id`（匹配不到只回文本）→ 组 `ExpenseAIPrefill`（解析失败/超时 → `502` + 可回退信号）→ **返回，不落库**。VAT treatment 不让模型猜（跨境判定不可靠），留用户选、默认 `NL_DOMESTIC_PURCH`。
- **测试探针（D14）**：`test_ai_config(cfg)` → 用配置 `httpx` 发一条带**内置极小图片**（仓库里固定的几十字节 PNG，**不外发用户数据**）+ 短文本（如「reply OK」）的 Chat Completions 请求 → 正常返回 ⇒ `{ok:true, multimodal:true}`；鉴权/连接失败、模型不存在、模型拒绝图片 ⇒ `ok`/`multimodal` 对应置 false + `detail` 给原因。供 `POST /settings/ai/test` 调。
- **安全**：票据图视作不可信；模型返回的文本/数字解析时做范围/类型校验，不直接信任；不把模型原文未过滤地塞进会渲染的地方（红线 7，PDF 在 M9，但 prefill 文本进前端走 Vue 默认转义）。

## 数据模型 / 迁移
> UUID PK；根挂 `company_id`（红线 2，`RESTRICT`）；子表 FK `CASCADE`（红线 3）；字典 FK `SET NULL`/`RESTRICT` + 快照；金额列 `NUMERIC(18,3)`；文本列 `text`（红线 10）。**三张新表**（`expense` / `expense_attachment` / `recurring_expense`）+ 一个新枚举 `RecurringFrequency`，**不动既有表**。可拆成「步骤 1 一条（expense）+ 步骤 2 一条（expense_attachment）+ 步骤 3 一条（recurring_expense）」三条 additive 迁移，各步自带。

- **`expense`**：`id`；`company_id` FK→`company.id`(`RESTRICT`) index；`expense_date` Date NOT NULL index；`category_id` FK→`expense_category.id`(`SET NULL`) nullable index；`category_name` text nullable（快照）；`supplier_name` text nullable；`vat_treatment_id` FK→`vat_treatment.id`(`RESTRICT`) nullable；`vat_treatment_code`/`vat_treatment_label`/`vat_treatment_effect` text NOT NULL（快照）；`vat_rate_id` FK→`vat_rate.id`(`RESTRICT`) nullable；`vat_rate_percent` `NUMERIC(6,3)` NOT NULL（快照）；`vat_rate_label` text NOT NULL（快照）；`net_amount`/`vat_amount`/`gross_amount` `NUMERIC(18,3)` NOT NULL；`deductible` Boolean NOT NULL；`currency` String(3) NOT NULL；`exchange_rate` `NUMERIC(18,8)` NOT NULL server_default `1`；`base_net_amount`/`base_vat_amount`/`base_gross_amount` `NUMERIC(18,3)` NOT NULL；`reference` text nullable；`note` text nullable；`is_draft` Boolean NOT NULL server_default `false`；`recurring_expense_id` FK→`recurring_expense.id`(`SET NULL`) nullable（标记来源；删模板不删历史开支）；`creator_id` FK→`user.id`(`SET NULL`) nullable；timestamps。索引 `ix_expense_company_id`/`ix_expense_expense_date`/`ix_expense_category_id`。
- **`expense_attachment`**：`id`；`company_id` FK→`company.id`(`RESTRICT`) index；`expense_id` FK→`expense.id`(`CASCADE`) NOT NULL index；`storage_key` text NOT NULL（不外露）；`filename` text nullable（客户端原名，仅展示）；`mime_type` text NOT NULL；`byte_size` Integer NOT NULL；`sha256` text nullable（留位 dedup）；`creator_id` FK→`user.id`(`SET NULL`) nullable；timestamps。
- **`recurring_expense`**：`id`；`company_id` FK→`company.id`(`RESTRICT`) index；`name` text NOT NULL；模板字段（`category_id` FK `SET NULL` + `category_name`、`supplier_name`、`vat_treatment_id` FK `RESTRICT` + 快照、`vat_rate_id` FK `RESTRICT` + 快照、`net_amount`/`vat_amount`/`gross_amount` `NUMERIC(18,3)`、`deductible`、`note`，与 expense 同口径）；`frequency` `RecurringFrequency`（MONTHLY/QUARTERLY/YEARLY）；`start_date` Date NOT NULL；`end_date` Date nullable；`max_occurrences` Integer nullable；`occurrences_generated` Integer NOT NULL server_default `0`；`next_run_date` Date NOT NULL index；`last_generated_at` DateTime nullable；`active` Boolean NOT NULL server_default `true`；`creator_id` FK→`user.id`(`SET NULL`)；timestamps。索引 `ix_recurring_expense_company_id`/`ix_recurring_expense_next_run_date`。
- **删除安全**：删 `expense` → DB cascade 删 `expense_attachment` 行 + service 删磁盘文件（红线 3 + D12）；删 `expense_category`/`vat_rate`/`vat_treatment` 行为同 invoice/payment 范式（分类 `SET NULL` 保 name 快照，税率/treatment `RESTRICT`）；删 `recurring_expense` → 已生成开支 `recurring_expense_id` `SET NULL`（历史账目独立保留）；删 `user` → `creator_id` `SET NULL`。
- **RLS**：继续留口不开；服务层集中用当前用户 `company_id`（红线 2）。

---

## 原子步骤清单
> 每步 = 一个原子改动（CI 绿即可合 `main`），过 roadmap §5 DoD。**每步自包含**：给清要镜像的既有文件 + 不变量 + 必覆盖测试 + 盲审要点。**`services/expense`/`recurring`/AI 解析等算钱/状态逻辑必须有单测。**
> 切分意图：先把「记账数据底座 + 分存/默认/快照正确」立稳（可独立单测）→ 叠收据 storage → 叠周期生成 → 叠 AI 票据填写 → 前端 + 收尾。AI / 外部模型细节集中在步骤 4。

### 步骤 1 · `expense` 表 + 迁移 + `services/expense` CRUD + schemas
- **目标**：能记一笔开支并看到后端算好的 `gross`、解析好的 `deductible` 默认与全套快照。
- **契约**：`ExpenseInput`/`ExpenseRead`/`ExpenseListItem`/`ExpenseListResponse`；`POST/GET /expenses`、`GET/PUT/DELETE /expenses/{id}`。
- **实现任务**：
  - **后端**：
    - `models/_enums.py`：无需新增（`RecurringFrequency` 留到步骤 3）。
    - `models/expense.py`：`Expense` ORM（按上「数据模型」；`_MONEY=Numeric(18,3)` 等镜像 `models/invoice.py`/`models/payment.py`），在 `models/__init__.py` 注册。**步骤 1 的 `recurring_expense_id` 列先建为普通 nullable UUID 列、FK 留到步骤 3 建表后补**（或步骤 1 不建该列、步骤 3 additive 加列——实现者择一，盲审只认「不破坏 additive 性」）。
    - `schemas/expense.py`：上述 schema（镜像 `schemas/invoice.py` 的 Decimal/校验风格）。
    - `services/expense.py`：`create_expense`/`list_expenses`/`get_expense`/`update_expense`/`delete_expense`——分存 + `gross` 计算 + `deductible` 默认解析 + 全快照 + 单一本位币 + `vat_treatment.side==PURCHASE` 守卫（见「算钱与分存规则」）；`company_id` 注入；列表过滤/分页/排序镜像 `services/payment.py::list_payments`。`delete_expense` 删磁盘文件的钩子留到步骤 2（步骤 1 暂无附件）。
    - `api/expenses.py`：薄路由（owner-only、`company_id` 注入），挂进 `api/__init__.py`。
  - **前端**：仅 `npm run codegen`（UI 放步骤 5）。
- **迁移**：建 `expense` + 索引（一条 additive；不动既有表）。
- **测试（必覆盖）**：`gross = net + vat` 且按分量化；`net×rate ≠ vat` 也能存（D8 不校验等式）；`deductible` 默认解析三档（分类 True/False/None→fallback True、显式覆盖）；快照落对（删分类后 `category_name` 仍在）；销售侧 treatment 被拒（`422`）；非本位币被拒（`422`）；`base_* = 原值`、`exchange_rate=1`；列表过滤（分类/日期/可抵扣/`is_draft`/`q`）+ 分页 `total` + 排序；跨公司 `404`；owner-only；负额被拒。
- **审查要点（盲审）**：① 分存 + `gross` + 默认 + 快照逻辑在 `services/`、被单测覆盖（红线 1）；② **不校验 `vat==net×rate`**（D8）；③ `vat_treatment.side==PURCHASE` 守卫存在；④ `company_id` 由 service 注入、无散落 `where company=`（红线 2）；⑤ 金额 `Decimal`/`NUMERIC(18,3)`、落盘 `quantize_to_minor_unit`（D11）；⑥ 单一本位币（D10）；⑦ `text` 列用对（红线 10）；⑧ 字典 FK ondelete 与快照符合 D7/D12；⑨ 契约与 `schema.d.ts` 无漂移。
- **DoD**：见 roadmap §5。

### 步骤 2 · storage 抽象 + 收据上传/下载/删除（`expense_attachment`）
- **目标**：给一笔开支传票据图/PDF，预览、下载、删除；删开支级联清附件 + 磁盘文件。
- **契约**：`ExpenseAttachmentRead`/`ExpenseAttachmentListResponse`；`POST/GET /expenses/{id}/attachments`、`GET /attachments/{id}/content`、`DELETE /attachments/{id}`。
- **实现任务**：
  - **后端**：
    - `config.py`：加 `storage_root: str`（dev 默认 `./var/storage`）、`max_receipt_bytes: int`（默认 10 MB）。
    - `services/storage.py`：`Storage` 抽象 + `LocalStorage`（见「storage 抽象」节；路径穿越防护、白名单扩展名、magic-bytes/MIME 双校验）。镜像 `services/assets.py` 的校验风格。
    - `models/expense_attachment.py` + 注册；迁移建表（`CASCADE` 于 expense）。
    - `schemas/expense.py`：加附件 schema（**不外露 `storage_key`/路径**）。
    - `services/expense.py`：`add_attachment`（multipart→校验→`storage.save`→落元数据行）、`list_attachments`、`get_attachment_content`（取本公司附件→`storage.open`→流）、`delete_attachment`（删行 + `storage.delete`）；并在 `delete_expense` 里删该 expense 全部附件磁盘文件（D12）。
    - `api/expenses.py`/`api/attachments.py`：路由（`UploadFile`，镜像 M2 logo 上传；owner-only、`company_id` 注入）。
  - **前端**：`npm run codegen`。
- **迁移**：建 `expense_attachment` + 索引（一条 additive）。
- **测试（必覆盖）**：上传合法 PNG/JPEG/WebP/PDF 成功、落元数据 + 落盘；非白名单 MIME / 超体积 → `422`；伪装扩展名 / `..` 路径被拒（红线 7）；`GET .../content` 返回原 MIME + inline；删附件清 DB + 磁盘；**删 expense 级联清附件行 + 磁盘文件**；跨公司附件 `404`；owner-only；`storage_key`/路径不出现在任何响应。
- **审查要点（盲审）**：① 收据走磁盘 storage 抽象、**未塞 `binary_asset`**（D2）；② 路径不可穿越、文件名不信任客户端（红线 7）；③ 删 expense → 附件行 cascade + 磁盘文件被 service 删（红线 3 + D12）；④ `storage_key`/磁盘路径不外露；⑤ storage 接口形状能被 M9 PDF 复用（`save/open/delete/exists` + namespace）；⑥ 契约无漂移。
- **DoD**：见 roadmap §5。

### 步骤 3 · 周期性开支（`recurring_expense` + APScheduler 第二个 job）
- **目标**：建一条月/季/年模板，到点幂等生成 `is_draft=true` 的开支；可暂停、可设上限；手动「立即生成」可用。
- **契约**：`RecurringExpenseInput`/`RecurringExpenseRead`/`RecurringExpenseListResponse`；`POST/GET /recurring-expenses`、`GET/PUT/DELETE /recurring-expenses/{id}`、`POST /recurring-expenses/{id}/run-now`。
- **实现任务**：
  - **后端**：
    - `models/_enums.py`：加 `RecurringFrequency`（MONTHLY/QUARTERLY/YEARLY）。
    - `models/recurring_expense.py` + 注册；迁移建表 + 给 `expense.recurring_expense_id` 补 FK（若步骤 1 未建该列则此处 additive 加列 + FK `SET NULL`）。
    - `schemas/expense.py`（或新 `schemas/recurring_expense.py`）：上述 schema。
    - `services/recurring_expense.py`：CRUD + **纯函数 `compute_next_run(frequency, from_date)`**（月/季/年步进，**默认用 stdlib 手算**——加 N 个月后用 `calendar.monthrange` 把超界日退到月末，如 1/31 + 1 月 → 2/28；**不引新依赖**，除非实现者明确选择加 `python-dateutil`。**可独立单测**）+ `generate_due_recurring_expenses_all(session)`（遍历 `active` 且 `next_run_date <= today` 且未超上限的模板 → 克隆出 `is_draft=true` 开支 [`recurring_expense_id` 指回模板、复制快照字段] → 推进 `next_run_date`/`occurrences_generated`/`last_generated_at` → 达上限/截止 → `active=false`；**幂等**：同一周期只生成一次，事务内推进游标）。复用步骤 1 的 expense 落库口径，**不重复算钱实现**。
    - `main.py`：在 `lifespan` 的 scheduler 上加第二个 cron job `_generate_recurring_expenses_job`（镜像 `_expire_quotes_job`：拿 session、调 `generate_due_recurring_expenses_all`、`commit`/`rollback`、日志）；config 加 `scheduler_recurring_expenses_hour`（默认 2，错开 quote-expiry 的 1 点）。
    - `api/recurring_expenses.py`：CRUD + `run-now`（直接调同一生成函数，仅作用于该模板）。
  - **前端**：`npm run codegen`。
- **迁移**：建 `recurring_expense` + 索引 + （如需）`expense.recurring_expense_id` FK（additive）。
- **测试（必覆盖）**：`compute_next_run` 月/季/年步进（含跨年、月末日如 1/31→2/28 的退让，纯函数单测）；`generate_due_*` 幂等（连跑两次不重复生成同周期）；到点生成 `is_draft=true` 且字段/快照克隆正确、`recurring_expense_id` 指回；暂停（`active=false`）跳过；上限（`max_occurrences` / `end_date`）到达后停并置 `active=false`；`run-now` 与定时同口径；跨公司隔离；owner-only。**集成测试**镜像 `tests/test_quote_convert_reactivate_integration.py` 风格。
- **审查要点（盲审）**：① 生成的开支 `is_draft=true`（D3）；② 幂等（游标在事务内推进，连跑不重复）；③ 算钱口径复用步骤 1、无重复实现；④ scheduler job 镜像 `_expire_quotes_job` 的事务/异常处理；⑤ 删模板不删历史开支（`recurring_expense_id` `SET NULL`）；⑥ `company_id` 收敛；⑦ 契约无漂移。
- **DoD**：见 roadmap §5。

### 步骤 4 · ⭐ AI 票据智能填写（`services/ai` OpenAI 兼容管道 + AI 设置 + 连通性测试）
- **目标**：对一张已上传票据（**图片 / PDF**，PDF 后端栅格化成图）调 OpenAI 兼容 Chat Completions 视觉模型，返回预填字段（不落库），人工确认后走步骤 1 入库；AI 关闭/失败优雅回退手填；设置页能**测试**连通性 + 多模态。
- **契约**：`ExpenseAIPrefill`/`AiSettings`/`AiSettingsRead`/`AiSettingsUpdate`/`AiTestResult`；`POST /expenses/ai-extract`、`GET/PUT /settings/ai`、`POST /settings/ai/test`。
- **实现任务**：
  - **后端**：
    - `pyproject.toml`：把 `httpx` 从 dev 组**提升为主依赖**；加 `pypdfium2` + `Pillow`（PDF 栅格化，D16；纯 wheel、无系统包）。**不加 `openai`/`anthropic`/`pdf2image`/`PyMuPDF`**。
    - `config.py`：加 `ai_base_url: str="https://api.openai.com/v1"`、`ai_api_key: str=""`、`ai_model: str=""`、`ai_enabled: bool=false`、`ai_pdf_max_pages: int=3`、`ai_pdf_render_scale: float`（或 DPI）（env 兜底）。
    - `schemas/setting.py`：`SETTING_KEY_AI="ai"` + `AiSettings`/`AiSettingsRead`/`AiSettingsUpdate`/`AiTestResult`（**镜像 `SmtpSettings`/`SmtpSettingsRead`/`SmtpSettingsUpdate`** + `base_url`/`receipt_prompt` 字段）。
    - `services/ai.py`：`_get_ai_config`（DB→env 兜底，镜像 `services/email.py::_get_smtp_config`）；`DEFAULT_RECEIPT_PROMPT` + `_OUTPUT_CONTRACT` 常量；`_attachment_to_images(...)`（`image/*` 直用；`application/pdf` → `pypdfium2` 逐页 render（上限 `ai_pdf_max_pages`）→ `Pillow` 编码；其它 MIME → `422`）；`httpx.AsyncClient` 调用封装（`POST {base_url}/chat/completions`，请求/响应自定义小 Pydantic 模型，**client 可注入/可 mock**，`base_url`/`model`/`prompt` 全来自配置、不写死）；`extract_from_attachment(...)`（取本公司附件→AI 关闭/无 key→明确错误→读 storage 文件→`_attachment_to_images`→每图 `image_url` + 有效提示词调 Chat Completions→**容错解析**→服务端按名匹配分类→**返回不落库**）；`test_ai_config(cfg) -> AiTestResult`（内置极小图探针，见「AI 票据管道」节）。
    - `api/settings.py`：加 `GET/PUT /settings/ai` + `POST /settings/ai/test`（owner-only，镜像 `/settings/smtp` + `/smtp/test`，**读时打码**）。
    - `api/expenses.py`：加 `POST /expenses/ai-extract`。
  - **前端**：`npm run codegen`（UI 步骤 5）。
- **迁移**：无（AI 设置走既有 `setting` 表 key-value）。
- **测试（必覆盖，全部 mock `httpx`（`respx`/`MockTransport`），不打真网络）**：fake 响应 → prefill 字段映射 + 分类按名匹配正确；模型回带 code fence / 缺字段 / 多余字段的 JSON → 容错解析（全字段可空、未知键忽略）；连接/解析失败/超时 → `502` + 回退信号；`enabled=false` 或无 key → 明确错误码（非 500）；跨公司附件 → `404`；**PDF 栅格化**：电子版 PDF / 多页 PDF（验证页数上限 `ai_pdf_max_pages` 生效、每页一个 `image_url` part）/ 扫描版（image-only）PDF 都能转成图喂模型（用小样本 PDF fixture，`pypdfium2` 真渲染、不打网络）；真正不支持的 MIME（如 `text/csv`）→ `422`；`AiSettingsRead` 打码（`api_key_set` 而非明文、`base_url`/`receipt_prompt` 照回）；**key 不进日志、不进任何响应体**（含 `ExpenseAIPrefill`/`AiTestResult`）；`PUT` 省略 `api_key` 保留原值、空串清除（镜像 SMTP）；`receipt_prompt` 空 → 用 `DEFAULT_RECEIPT_PROMPT`，非空 → 用用户值，且**两者都被追加 `_OUTPUT_CONTRACT`**；`test` 端点：成功 → `{ok:true,multimodal:true}`，鉴权失败/模型拒图 → 对应 false + detail。
- **审查要点（盲审）**：① 凭证存取**照搬 SMTP 范式**、读打码、env 兜底（D1）；② **key 绝不入日志 / 响应**（grep `api_key`/`ai_api_key` 出现位置）；③ 客户端 = **`httpx` 自构造 OpenAI 兼容 Chat Completions**、无 `openai`/`anthropic`/`requests`、`base_url`/`model`/`prompt` 来自配置不写死、**可注入、单测不打真网络**（D6）；④ 提示词 = 用户值或默认 + **系统固定追加输出契约 footer（用户不可改）**、解析容错（D15）；⑤ PDF 经 `pypdfium2` 栅格化成图后才走 image_url、页数有上限、**未引系统 poppler/PyMuPDF**（D16）；⑥ prefill **不落库**、入库仍走步骤 1（D5）；⑦ 失败/关闭优雅回退（不抛裸 500）；⑧ 真正不支持的 MIME 被 `422` 挡；⑨ 票据图当不可信输入、解析有类型/范围校验（红线 7）；⑩ 测试探针用内置图、不外发用户数据（D14）；⑪ 契约无漂移。
- **DoD**：见 roadmap §5。

### 步骤 5 · 前端 + 收尾（i18n + UX + 部署自测点）
- **目标**：开支全流程可视化操作；AI 设置进齿轮面板；EN/ZH 齐。
- **契约**：对齐步骤 1–4 的 `schema.d.ts`。
- **实现任务**：
  - **后端**：必要小修（错误码/文案/排序/docstring）。
  - **前端**：
    - `stores/expenses.ts` / `stores/recurringExpenses.ts`；开支列表页（过滤：分类/日期/可抵扣/草稿/搜索 + 分页）；开支编辑器（分类/供应商/净额+税额/税率/PURCHASE treatment/可抵扣，**金额展示取后端 `gross`，前端不本地权威算钱**，红线 1）；收据面板（上传 + 原图 `<img>`/PDF 内嵌预览 + 下载 + 删除）；**「拍票据自动填」入口**（先传票据 → 调 `ai-extract` → 把 `ExpenseAIPrefill` 回填编辑器 → 用户确认后保存；AI 关闭/失败时按钮禁用或提示回退手填）；周期性开支管理页（CRUD + 暂停 + 「立即生成」+ 生成的草稿在开支列表以 badge 标 `is_draft`，可一键确认转正）。
    - 齿轮设置面板加 **AI 分类**（复用 M2.5 预留位）：填 `base_url` + `model` + API key（写后只显 `api_key_set`）+ 启用开关 + **提示词 textarea**（`receipt_prompt`，空占位显示默认、可「恢复默认」）+ **「测试」按钮**（调 `POST /settings/ai/test`，回显连通 + 是否多模态 + detail）。
    - 注意 `prod build` 下动态 prop 按钮不丢 `@click`（记忆 [[vue-loading-prop-vif-prod-bug]]）。
  - **迁移**：无。
  - **i18n**：`expenses.*`/`recurring.*`/`receipt.*`/`ai.*` 进 `en.json` + `zh.json`。
- **测试 / 自检**：`npm run build`；`schema.d.ts` 无漂移。（无逐步人工 walkthrough——见文末里程碑级自测点。）
- **审查要点（盲审）**：① 前端**不本地权威算钱**，`gross`/`base_*` 取后端（红线 1）；② 编辑器只发原始输入（不发 `gross`/快照/`base_*`）；③ AI key 在前端只写不回显明文（只 `api_key_set`）、`base_url`/`model` 可见；④ AI 关闭/失败 UI 回退手填、不卡死；⑤「测试」按钮把 `AiTestResult` 反馈清楚（连通 + 多模态）；⑥ 收据预览不外露 storage 路径；⑦ `prod build` 动态按钮不丢 `@click`；⑧ i18n key 齐、EN/ZH 都有。
- **DoD**：见 roadmap §5。

## follow-on（M8 主线收尾后单列一轮 · 不计入 M8 验收 · D4）
> **⭐ AI 供货价单识别 → 灌 M4 产品目录**：复用步骤 4 的 `services/ai` 视觉管道，把供应商价格单（图/PDF/Excel）→ 识别 → 按 **SKU upsert** 进 `product`（改价覆盖、无 SKU 始终新建，见记忆 [[product-sku-upsert]]），给 M6.5 成本核算供数。Excel 解析按需加 `openpyxl`。进入这一轮时再 JIT 补契约/测试/盲审要点（同本文体例）。

## 🟢 部署自测点（里程碑验收 · 作者末轮一次性人工 walkthrough）
> 逐步不再人工走（逐步靠测试 + 盲审）；这一组是**整个 M8 主线完成后**作者人工审计用。本地集成：`docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build`；浏览器 `http://localhost:${APP_HOST_PORT:-8000}`。

1. **记一笔开支**：填分类 / 供应商 / 净额 + 进项税额 / 税率 / PURCHASE 处理类别 / 可抵扣（默认取分类，可改）→ 保存；`gross = net + vat` 落到分；快照字段正确。
2. **可抵扣默认**：换一个 `default_deductible` 不同的分类，新建时默认值随之变；逐笔覆盖生效。
3. **收据上传/预览/删除**：上传一张票据图 + 一个 PDF → 预览（图内嵌、PDF 内嵌）+ 下载；删一张附件；**删整笔开支 → 附件磁盘文件一并消失**。
4. **AI 设置 + 测试 + 提示词**：齿轮面板 AI 分类填 `base_url`/`model`/key + 开启 → 点「测试」实际探一次 → 回显「连通 + 模型支持图片输入」；填个不支持多模态/错 key 的配置 → 测试明确报失败原因；改一下 `receipt_prompt` 保存 → 下次抽取用新提示词、清空 → 回落默认。
5. **拍票据自动填（图片 + PDF）**：分别上传 ① 图片票据、② 电子版 PDF、③ 扫描版 PDF → 「自动填」都能预填净额/税额/税率/供应商/日期/分类建议（PDF 经后端栅格化）→ 编辑确认后保存（**确认前不入库**）；多页 PDF 只取前 N 页；关闭 AI / 模型失败 → 回退纯手填、不卡死。
6. **周期性开支**：建一条月度模板 → 「立即生成」（或等定时）出一笔 `is_draft=true` 草稿 → 在开支列表确认转正；暂停后不再生成；设上限到达后自动停。
7. **隔离 / cascade / 单币种**：跨公司取开支 / 附件 → `404`；非本位币金额被拒（`422`）；删模板不删历史开支。
8. **设置安全**：AI key 写后只显「已设置」、刷新不回显明文；后端日志 / 响应不含 key。
9. **CI 四关全绿；`schema.d.ts` 无漂移；`docker build` 通过。**

## 验收结论（收尾时回填）
- **完成日期**：2026-06-14。
- **验收**：5 步主线（expense CRUD+分存 / storage 收据 / 周期开支 / AI 票据填单 / 前端收尾）orchestrator 模式逐步实现，每步盲审 + 返工收敛；自动化测试 ruff + mypy --strict（88 files）+ 单测 599（集成另计，其中 test_expense_integration 42）+ frontend build + `schema.d.ts` 无漂移全绿。
  作者末轮人工 walkthrough 在自测点上又发现一批可用性/识别问题，单列一轮 **walkthrough refinements**（一并压进 commit `e7e723d`）修掉：① 可抵扣随分类联动（`@update:value` 修复，另有一处分类 `default_deductible=NULL` 是数据问题非代码）；② 收据存储改 bind-mount 宿主目录 + uid 1000 跑容器，方便备份（`docker-compose.yml`）；③ AI 连通性探针图 1×1→64×64（视觉网关拒收 <8px 小图，曾误报 not supported）；④ 抽取时注入当前日期（修「future 2025」误判）；⑤ AI 把收据摘要写进开支 `note`（新增 `summary` 字段，**解释性输出跟随界面语言**，专有名词/分类名保持原文）；⑥ 选税率 + 填净额自动算 VAT（后端 `POST /expenses/calculate`，VAT 仍可覆盖）；⑦ 提示词语义由「自定义整段替换默认」改为「**默认常驻 + 自定义作为附加指令追加**」（设置界面标签改 Additional Instructions / 附加指令）。
  - **自测点**：1–5 人工通过（含上述 refinements 复测）；**6 周期性开支** 作者现阶段不用、暂不人工走（视为完成，回头有机会再补）；**7 隔离 / cascade / 单币种**（跨公司 `404`、非本位币 `422`、删模板不删历史开支）由集成测试覆盖；**8 设置安全**（key 写后只显已设置、日志/响应不含 key）通过；**9 CI 四关 + 无漂移 + docker build** 本地 DoD 等价四关全绿，远端 CI 同套关待作者最终确认。
- **已知遗留 / 顺延项**：AI 供货价单 → 目录（follow-on，单列一轮）；进项 VAT 申报聚合（2a/4a/4b/5b）→ M10；部分可抵扣百分比 → 顺延；多币种开支 / 开支侧 FX → 顺延；供应商作为独立实体 / 挂客户 → 顺延；服务端缩略图 → 不做；银行流水导入 → vNext；**周期性开支自测点 6 作者暂未人工走**（不影响验收，作者后续按需补测）。（PDF 票据的 AI 抽取**已纳入** M8——栅格化成图，见 D16。）
