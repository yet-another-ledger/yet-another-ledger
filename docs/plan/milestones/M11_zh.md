# M11 · 里程支出（私人交通工具商用）

> 🌐 [English](M11.md) · **中文**

> **状态：🟢 完成 · 2026-08-19 与作者共定冻结；实现于 2026-08-20 完成；作者 walkthrough 于 2026-08-21 验收。** 本里程碑为私人拥有或私人租用交通工具增加商业里程工作流，**不**实现公司车辆记账。实现前先读 `docs/plan/roadmap_zh.md` §2 全局约束，以及 M8/M8.5/M10 已冻结的开支与报表决策。
>
> **税务锚点（规划时的现行口径）**：荷兰税务局说明，私人交通工具用于商业行程时，**2026 年可按 €0.25/km** 从利润中扣除；**2024 与 2025 年为 €0.23/km**。所得税口径下，通勤（`woon-werkverkeer`）属于商业里程。该定额已包含燃油、保险、过路费、停车等成本，不能再通过此里程申报重复扣除。来源：[Belastingdienst · Zakelijk gebruik privévervoermiddel](https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/winst/inkomstenbelasting/inkomstenbelasting_voor_ondernemers/zakelijk-gebruik-privevervoermiddel)。

> **当前扩展（2026-11-10）** [M18 里程 MCP 录入](M18_zh.md)已在 v0.14.0 交付，使用独立 `mileage:create` 权限。自动化门禁及作者委托的后端验收已通过；当前交付状态见 [M18](M18_zh.md) 和[路线图](../roadmap_zh.md)。这不改变 M11 已验收的范围或里程计算。

## 执行模型

> 遵循 `AGENTS_zh.md`：默认是人工模式；只有作者明确指定时才进入 orchestrator 模式。orchestrator 模式下，下面每一步各落一个实现 commit，随后盲审 / 返工收敛并逐步 autosquash；全部步骤完成后才生成里程碑级报告。

## 依赖与现有能力

- **M8 / M8.5 Expense 底座**：`expense` 已接入 P/L、Dashboard、Expense Report 和 BTW 报表，并带分类/税/币种快照，以及 `paid_by`、`business_percentage`、`depreciation_years`。
- **M4 数据驱动字典**：开支分类、VAT 税率和处理类别都是公司级可编辑数据；M11 新增同样数据驱动的交通工具类型字典。
- **M7.5 金额约定**：面向用户的金额用 `Decimal` + `ROUND_HALF_UP` 落到币种最小单位；距离×费率的中间结果至少保留三位小数。
- **M10 报表**：里程申报必须作为商业成本进入 P/L 与普通开支合计，同时对 BTW 申报贡献零 VAT。
- **类型化设置**：公司默认值复用现有三层 settings service + Pydantic model，不允许散落裸字符串设置。

## 目标与范围

- **目标**：私人交通工具发生商业里程时，只录行程日期和单程公里数，并可勾选往返；系统根据带生效日、可编辑的费率计算并保存可扣除支出。
- **纳入（IN）**：
  - 私人拥有或私人租用交通工具产生的所有商业行程，包括通勤、拜访客户/供应商、前往项目现场。
  - 公司级可维护交通工具类型，预置 汽车 / 摩托车 / 自行车 / 其他；默认汽车。
  - 一套通用的按生效日费率，以及可选的交通工具类型专属费率。
  - 独立 Mileage 里程支出分类；不转换现有开支，也不改现有 Travel 分类。
  - 一条里程行程关联一笔自动生成的 Expense，让现有财务报表继续统一读取 Expense 投影。
  - 可选出发地址、到达地址、行程目的、备注；手填单程公里数始终是权威输入。
  - 费率追溯变更时，先显式预览、再确认批量重算，并保留审计轨迹。
  - Expense 页面使用 Purchase expenses / Mileage expenses 两个标签页，各自有合适的列和编辑器。
- **不纳入（OUT / 顺延）**：
  - 公司拥有/公司租赁车辆的记账。这类车辆走实际成本，行程日志服务于不同的 BTW/bijtelling 目的，不能生成本里程碑的定额 Expense；行程模型为以后做 additive 扩展留口。
  - Google Places Autocomplete / Routes API 或其他地址/路线提供商。未来 follow-on 可增加每 instance 凭证、后端代理、标识归属、建议路线距离与手填 fallback；M11 只存可选地址文本。
  - 基于里程表的闭环行程登记、私人绕路、多中途点、GPS 跟踪、车辆资产档案、周期通勤模板、里程导出文件。
  - 自动获取法定费率。种子值是可编辑数据，不承诺自动跟随未来法律更新。

## 已冻结产品与架构决策

- [x] **D1 · 产品名与边界**：界面名称 = **Mileage expense / 里程支出（私人交通工具商用）**。覆盖所有私人交通工具的商业里程，不只通勤，也不限汽车。
- [x] **D2 · 私人先做，公司车辆后做**：M11 对外写 API 只创建 `PRIVATE` 行程。行程有独立身份，Expense 一对一链接可空，使未来公司车辆工作流可复用行程记录而不生成定额 Expense。
- [x] **D3 · Expense 仍是报表投影**：每条 M11 私人行程原子生成一笔 `expense(kind=MILEAGE)`。现有报表继续读取 Expense，不去 union 第二张财务表。
- [x] **D4 · 旧数据不动**：迁移把所有已有 Expense 回填为 `PURCHASE`；金额、分类、VAT 快照、附件、周期模板、报表历史均不重算。现有 Travel 仍由用户自行管理。
- [x] **D5 · 页面内分标签**：顶层只保留一个 Expenses 导航；页面内为 `Purchase expenses` 与 `Mileage expenses` 两个 tab。Purchase 保持现有收据导向的列表/编辑器；Mileage 使用距离/费率导向的列和简表单。
- [x] **D6 · 录入口径**：行程日期、交通工具类型、单程距离（>0）必填。`round_trip=false` 默认关闭；勾选后由后端算 `total_distance_km = one_way_distance_km × 2`。起止地址/目的/备注均为可选 `text`。
- [x] **D7 · 交通工具类型数据驱动**：类型是公司级可编辑记录，不是硬编码 enum。预置 汽车 / 摩托车 / 自行车 / 其他，默认汽车；停用类型保留历史可读，但新行程不可再选。
- [x] **D8 · 按生效日的费率层级**：规则包含 `effective_from` 与可空 `transport_type_id`。先取 `<= trip_date` 的最新类型专属规则；没有则取最新通用规则。没有有效规则属于配置错误，绝不隐式按零处理。
- [x] **D9 · 初始可编辑种子**：通用规则预置 `2024-01-01 → 0.230` 与 `2026-01-01 → 0.250`（本位币/公里）。交通工具类型默认继承，除非存在类型专属覆盖。
- [x] **D10 · 历史快照**：每条行程保存所选类型名称、规则 id（历史 FK 可空）、生效日、每公里费率、总距离和计算金额。普通规则编辑不会静默改写已保存行程。
- [x] **D11 · 追溯修正必须显式确认**：新增/修改/删除过去生效的规则后，用户先看不一致行程和总差额，再明确应用。应用时重新校验不透明 preview token；预览过期返回 `409` 并要求重看。
- [x] **D12 · 费率调整审计**：每次已应用修正保存行程 id、旧/新规则快照、旧/新费率、旧/新金额、操作者与时间。应用在一个事务中更新行程 + 关联 Expense + 审计行。
- [x] **D13 · Mileage 分类**：为已有与未来公司预置独立 `Mileage` 开支分类（`default_deductible=false`，与无进项 VAT 处理一致），设为公司里程默认，并允许 owner 在设置中换成其他有效分类。已有开支永不搬入该分类。
- [x] **D14 · 会计投影**：自动 Expense 使用公司本位币、`exchange_rate=1`、`net=gross=里程金额`、`vat=0`、`deductible=false`、`paid_by=PRIVATE`、`business_percentage=100`、`depreciation_years=1`，且不要求 supplier/receipt。这里的 `deductible=false` 是现有的**进项 VAT** 标记——本来就没有进项 VAT 可退；它不妨碍该金额作为所得税商业成本进入 P/L。
- [x] **D15 · VAT 表达数据驱动**：预置 PURCHASE 侧处理 `NL_PRIVATE_TRANSPORT_MILEAGE`（`effect=EXEMPT`、`deductible=false`、无 report box），并使用公司 0 VAT 税率；快照明确表达 €0 VAT。必要 VAT 主数据缺失时，创建返回明确配置错误，service 不凭空硬编码 code。
- [x] **D16 · 金额计算只在 service**：前端只传日期/类型/距离/往返与可选文本。后端算总距离、解析费率、计算金额，并只在最终金额处量化到币种最小单位。
- [x] **D17 · 编辑/删除不变量**：只有 mileage endpoint 能改变 Mileage Expense 的财务字段；通用 Expense PUT 对 `MILEAGE` 返回 `409`。删除 Expense 根记录由 DB cascade 清理关联私人行程/审计行，不手写子表级联。
- [x] **D18 · 外部地图干净顺延**：M11 不落 provider key、坐标、place id、provider 响应缓存，也不发外部请求。未来路线建议可预填距离，但用户确认的距离仍是落库权威值。

## 契约（先行）

> 精确命名可沿用仓库现有单复数习惯，但下面的 shape 与行为已经冻结。每次契约变化都重生成 `frontend/src/api/schema.d.ts`。

### 现有 Expense 契约扩展

- `ExpenseKind = "PURCHASE" | "MILEAGE"` 加入 `ExpenseRead` 与 `ExpenseListItem`。
- `GET /api/v1/expenses` 增可选 `kind`；Purchase tab 固定请求 `PURCHASE`，Mileage 行不得泄漏进收据导向表格。
- 现有 `POST /expenses` 只创建 `PURCHASE`；现有 `PUT /expenses/{id}` 遇 Mileage 返回 `409` 并指向 mileage endpoint；现有 DELETE 保持为 Expense 根删除路径。

### Mileage 默认值、交通工具类型与费率

- `GET/PUT /api/v1/settings/mileage-defaults` → `MileageDefaultsRead/Update { expense_category_id, default_transport_type_id }`（COMPANY 级、owner-only；引用记录必须有效且属于本公司）。
- CRUD `/api/v1/mileage-transport-types`，使用 `MileageTransportTypeWrite { name, active }` 与 read/list model；公司内非空名称唯一。配置中的默认类型在换好另一个默认值前不可删除/停用。
- CRUD `/api/v1/mileage-rates`，使用 `MileageRateWrite { transport_type_id?, effective_from, rate_per_km }`；`transport_type_id=null` 表示通用费率。`(company, 类型或通用, effective_from)` 唯一，费率必须 `>0`。

### 计算与 Mileage CRUD

- `POST /api/v1/mileage-expenses/calculate`：`MileageCalculationRequest { trip_date, transport_type_id?, one_way_distance_km, round_trip }` → `MileageCalculationRead { one_way_distance_km, total_distance_km, rate_rule_id, rate_effective_from, rate_per_km, amount, currency }`；不落库。
- `POST /api/v1/mileage-expenses`：计算字段 + 可选 `origin_address`、`destination_address`、`purpose`、`note` → `201 MileageExpenseRead`（行程 + 关联 Expense id/分类/金额快照）。
- `GET /api/v1/mileage-expenses` 支持 `q`、`transport_type_id`、闭区间日期、分页、按日期/创建时间排序；返回 Mileage tab 所需的目的/地址/类型/距离/费率/金额列。
- `GET/PUT/DELETE /api/v1/mileage-expenses/{trip_id}` 均按公司隔离。PUT 从原始输入重算；只有解析出的费率变化时才写费率调整审计。
- `GET /api/v1/mileage-expenses/{trip_id}/rate-adjustments` 返回按时间排序的审计列表。

### 追溯重算

- `POST /api/v1/mileage-expenses/rate-recalculation/preview`：比较本公司所有 Mileage 行程与当前有效规则，返回 `preview_token`、影响条数、旧/新合计、差额和分页明细。类型专属覆盖应挡住无关通用费率变更。
- `POST /api/v1/mileage-expenses/rate-recalculation/apply`：请求 `{ preview_token }`，锁定并重查受影响行；预览已旧则 `409`，否则原子更新行程 + Expense 投影 + 审计行，并返回实际汇总。

## 数据模型与迁移

- **`expense.kind`**：类型化 `PURCHASE|MILEAGE`，`NOT NULL`、server default `PURCHASE`；按 tab 查询需要时与 company/date 组合索引。
- **`mileage_transport_type`**：`id`、`company_id`、`name`（`text`）、`active`、时间戳；`(company_id, name)` 唯一。
- **`mileage_rate`**：`id`、`company_id`、可空 `transport_type_id`、`effective_from`、`rate_per_km NUMERIC`、时间戳；通用/类型专属生效日分别唯一。删除类型由 DB cascade 删除其覆盖规则，历史行程靠快照保留。
- **`mileage_trip`**：独立 `id` + `company_id`；可空且唯一的 `expense_id` FK，`ON DELETE CASCADE`；M11 API 固定 ownership=PRIVATE；类型/费率 FK `SET NULL` + 快照；行程日期、单程/总距离、往返、计算金额、可选文本、creator/时间戳。
- **`mileage_rate_adjustment`**：公司/行程、旧/新规则/费率/金额快照、actor/时间戳；行程 FK `ON DELETE CASCADE`。
- **类型化公司设置**：`expense.mileage.defaults` 保存分类/default-type UUID；service 在读写时验证归属与 active。
- **加法迁移（当前 head 后下一条线性 revision）**：
  - 只增表/列/索引/FK；旧 Expense 得到 `PURCHASE`，不重算。
  - 按公司/名称或稳定 code 插入或复用 Mileage 分类、交通工具类型、通用默认费率、无进项 VAT 处理与公司默认；未来公司 onboarding 幂等预置同一套。
  - 不删除/重命名 Travel，也不推测任何旧 Expense 属于 Mileage。

## 计算与持久化规则

1. 校验有效且属于公司的分类/类型，以及正数、有限的 Decimal 距离。
2. 按往返标记派生 `total_distance_km`；距离保留三位小数精度。
3. 按 D8 解析费率，并锁定规则/类型快照。
4. 算 `raw_amount = total_distance_km × rate_per_km`；只有最终金额通过 `quantize_to_minor_unit` 量化。
5. 按 D14 固定会计字段及关联分类/VAT 快照创建/更新 Expense 投影。
6. 行程与 Expense 原子提交；任何查找/计算失败都不能留下其中任一半条记录。

## 原子步骤清单

### 步骤 1 · 契约 + 数据底座 + 费率解析/计算

- **契约**：声明全部新 schema/route，落 Expense kind 扩展，重生成 `schema.d.ts`。
- **后端**：enum/model/迁移/种子；类型化 mileage defaults；有效费率解析、往返距离、最终金额纯函数。
- **前端**：本步不做功能 UI，只接生成类型。
- **必测**：迁移回填/种子/幂等；类型专属/通用 fallback 与生效日边界；无规则；Decimal 精度；往返×2；半进位到分；跨公司/default 校验。
- **盲审要点**：加法迁移对生产安全；旧 Expense 值不动；前端无金额计算；种子是可编辑数据；通用规则的 nullable 唯一性正确。
- **DoD**：roadmap §5。

### 步骤 2 · Mileage CRUD + Expense/报表投影

- **后端**：计算 endpoint、Mileage CRUD/list、行程→Expense 原子投影、通用 Expense 更新守卫、DB cascade、报表回归。
- **必测**：create/get/update/list/delete；可选文本；停用/删除类型的快照；分类快照；租户隔离；失败回滚；P/L/Dashboard/Expense Report 纳入金额；BTW box 不变；Purchase 列表排除 Mileage。
- **盲审要点**：所有财务字段只在 service 计算；不建第二套报表聚合；不手写子表 cascade；€0 VAT 不得进 5b；request 不接 company_id。
- **DoD**：roadmap §5。

### 步骤 3 · 追溯费率预览/应用 + 审计

- **后端**：不一致扫描、合计/差额、确定性不透明 preview token、加锁重查/应用事务、调整历史 endpoint。
- **必测**：补 `2026-01-01 → 0.250` 找出旧 0.230 快照；类型覆盖优先；零影响；旧 token `409`；并发修改行程/规则；原子回滚；审计旧/新值；重复应用后无不一致。
- **盲审要点**：保存费率绝不静默改历史；应用只改 Mileage 行程财务快照及其 Expense 投影；preview/apply 按当前公司隔离；审计不含秘密。
- **DoD**：roadmap §5。

### 步骤 4 · 设置 + Expense tabs + Mileage 编辑器

- **前端**：
  - Settings panel 新增 Mileage：默认分类/类型、交通工具类型 CRUD、通用/类型专属生效费率表、受影响记录预览/确认。
  - Expenses 页新增 Purchase/Mileage tabs（route/query 状态刷新后保留）；Purchase 保持原 UI；Mileage 表显示日期、类型、单程/往返/总 km、费率、金额、目的/路线摘要与操作。
  - Mileage editor 只发原始字段，日期/类型/距离/往返变化时调用后端 calculate，并显示权威预览；不要求收据面板。
  - 补齐 EN/ZH，重生成/校验 `schema.d.ts`。
- **必过**：`npm run build`、codegen freshness、EN/ZH key 对称。
- **盲审要点**：前端不做乘法/金额取整；往返默认 false；默认类型被删/停用时给可操作设置提示；Purchase UX 不回归；可选地址纯文本且不触发网络。
- **DoD**：roadmap §5。

### 步骤 5 · 收尾 + 全量回归 + 里程碑报告

- 跑 backend ruff/mypy/默认+集成测试、frontend build/codegen freshness、Docker build、迁移升级聚焦测试。
- 只收小 UX/错误文案，不再扩功能范围。
- 实现时重新确认文档与官方费率免责声明仍准确；若法定值已变，应同步改可编辑种子与文档，绝不在 service 静默硬编码。
- orchestrator 模式下，盲审收敛并完成逐步 autosquash 后，生成 `review-notes/M11-report.md`，包含下方完整 walkthrough。
- **DoD**：roadmap §5。

## 🟢 部署自测点（里程碑末尾一次性人工 walkthrough）

> 默认以 `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d` 启动。这些自测点现在作为验收契约定义；之后由 orchestrator 展开进 `review-notes/M11-report.md`。每一步完成后不要求单独人工 walkthrough。

1. **设置与历史费率**：Mileage 设置可见新分类、汽车默认、四个种子类型、2024/2025 €0.23 与 2026 €0.25；新增 Electric Car 类型和一个类型专属的过去/未来费率。
2. **核心计算**：创建 2026 汽车行程，单程 12.5 km + 往返 → 后端显示 25 km、€6.25；保存/刷新后所有快照不漂移。
3. **可选证据 + 类型覆盖**：保存起止地址/目的/备注，再用覆盖类型创建同日同距离行程，确认费率/金额不同且通用汽车费率不受影响。
4. **Expense 整合**：Purchase 与 Mileage tab 只显示各自记录；改 Mileage 距离/日期后由后端重算；删除后关联 Expense/审计 DB cascade；已有 Purchase 与 Travel 完全不变。
5. **追溯修正**：修改/新增过去生效规则，检查受影响行和总差额；先取消一次（数据不变），再重新预览并应用；确认 Expense/报表金额变化，且旧/新审计可读。
6. **报表与 VAT**：Mileage 金额进入所选期间的 P/L、Dashboard、Expense Report 的 Mileage 分类；BTW 申报数字/box 不增加进项 VAT。
7. **守卫/fallback**：无有效费率、默认类型停用、跨公司 id、旧 preview token 均明确失败，且不残留半条 trip/Expense。
8. **质量门**：ruff、mypy strict、默认+集成测试、codegen freshness、frontend build、Docker build 全绿。

## 验收结论

- **完成日期**：2026-08-21。实现于 2026-08-20 完成；作者 walkthrough 于 2026-08-21 验收。
- **实现方式 / 各步 commit**：由 orchestrator 执行步骤 1–5，并经盲审/返工收敛。各步 autosquash 后的 commit：步骤 1 `7670827`、步骤 2 `ced6c44`、步骤 3 `8328fb6`、步骤 4 `305d83c`、步骤 5 `debbae1`。Walkthrough findings 由 `73869ea`（生效日期文案/DatePicker）和 `1120ecf`（显式 Expense tab URL、图标操作、Mileage 保存返回流程）修复；两项均经功能盲审零 finding。
- **自动化验证**：backend `ruff check .` 与 `mypy --strict src` 通过（104 个 source files）；默认 pytest 通过（1050 passed、830 deselected；6 个既有 warning）；隔离 PostgreSQL 迁移 upgrade/downgrade/head 测试通过（10 passed）；聚焦 M11 的 PostgreSQL CRUD/费率重算集成测试通过（14 passed；1 个既有 FastAPI deprecation warning）；标准测试环境中的完整隔离 PostgreSQL integration suite 通过（830 passed、1050 deselected；62 个既有 FastAPI deprecation warning；228.14s）；walkthrough 修复后的 frontend production build、受控 OpenAPI→TS freshness、EN/ZH key 对称（1210 个 key）与 Docker build 均通过。
- **作者人工 walkthrough**：2026-08-21 验收。作者完成了实际业务流程走查并接受本里程碑。刻意慢网速下短暂的 stale-preview/Save-disabled 状态未再手动 throttle；其 latest-request-wins 与 save gate 行为已由实现状态检查和多轮 Clean Agent 审查覆盖，作者接受该证据。Mileage Settings 与 Expense/Mileage UX 的 walkthrough findings 均在验收前修复并通过零 finding 复审。
- **已知顺延**：Google Places/Routes 集成；公司车辆行程/记账；闭环里程表/GPS；路线模板/周期；导出。
