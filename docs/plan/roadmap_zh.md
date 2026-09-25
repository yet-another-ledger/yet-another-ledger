# Yet Another Ledger · v1 主路线图（Master Roadmap）

> 🌐 [English](roadmap.md) · **中文**

> **这是什么**：把已拍板的 v1 范围（P0–P7）整理为 **M0–M13** 的**可施工总览**，并纳入后续拟做的收据收件箱与 Agent 记账 **M14–M15**。各里程碑遵循原子化、前后端可并行及完成后可部署自测的原则。
>
> **这不是什么**：不是逐行施工图。本文停在「里程碑 + 方法论 + 约束」这一层；每个里程碑的**原子步骤清单**在动手时才落到 `docs/plan/milestones/M<x>.md`（JIT 细化，模板见 `milestones/_TEMPLATE.md`）。
>
> **权威来源**：方向、范围、领域模型、决策记录现以**本路线图 + 各 `milestones/M<x>.md`（已冻结的逐里程碑决策）**为准；荷兰 VAT/BTW 申报口径以 `docs/insight/btw-aangifte-2026-guide.md`（税局官方申报说明的结构化指南）为准。（早期上游「分析文档」已于 2026-06-15 从仓库移除——内容已吸收进上述文档；旧里程碑文档里残留的「分析文档 §x」字样仅为历史痕迹。）
>
> **架构母版**：`~/workspace/trading-journal`（同构、已上线）。骨架、约定、Dockerfile、CI 全部对齐它；差异仅在「换 PostgreSQL + 发票领域」。

---

## 0. agent 开工必读（每次进场先看这 5 条）

1. **先读约束**：本文 [§2 全局约束] 是纪律红线，违反任何一条都要先停下来问。
2. **契约先行**：动手前先定/改 API 契约（Pydantic schema），再分头写后端实现与前端界面。见 [§1.1]。
3. **算钱只在后端**：前端只收原始输入，所有金额由后端 `services/pricing` 权威计算。见 [§2] 第 1 条。
4. **一步 = 一个原子改动 + 过 DoD**：原子、可独立部署、附测试，CI 绿即可合 `main`（单人开发不强制 PR）。模板见 [§5]。
5. **改了契约就重生成 TS 类型**：`npm run codegen`，并确保 CI 的 drift 检测过。见 [§1.1]、[§5]。

---

## 1. 开发方法论（四支柱）

### 1.1 Contract-first（契约先行）→ 前后端可并行

每个功能的**第一个动作**是把 API 契约定下来：在后端写 Pydantic 请求/响应模型 + 路由签名（可先返回桩数据），FastAPI 自动产出 OpenAPI。契约一锁：

- **后端线**：写 `models → schemas → services → api` 的真实实现 + 测试。
- **前端线**：`npm run codegen` 把 OpenAPI 拉成 `frontend/src/api/schema.d.ts`（**已提交进仓库**），前端对着类型写 store / view，先连 mock、最后对接真接口。

两条线并行，靠「类型 + OpenAPI」这一份契约保证不漂。CI 的 `codegen-freshness` 关会强制 `schema.d.ts` 与后端一致。

### 1.2 Walking Skeleton + 纵向薄切片 → 每个里程碑都能部署自测

- **M0 不是"堆基础设施"**，而是打通一条最薄的端到端链路：单容器起来、FastAPI 托管前端、健康检查、一个能打开的页面、Postgres 连上、迁移能跑、CI 绿。**第一天就能"进部署页面点一下"**。
- 之后每个里程碑加**一条纵向可见的功能**（DB → API → UI 一条龙），而不是"先写完所有后端再写前端"。每个里程碑结尾都有明确的 **部署自测点**（见 [§4]）。

### 1.3 原子步骤 + 统一 Definition of Done → 可维护、可 review

每个原子步骤是一组小而自洽的改动，结构永远是母版那套分层（`models / schemas / services / api`，算钱在 `services`），过统一的 DoD（见 [§5]）。**单人开发，不强制走 PR**：自测 + CI 绿即可直接合 `main`（想做人工 review 时仍可开分支/PR，留这个口）。好处：每个步骤都小而同构，哪天要回看也扫一眼就懂。

### 1.4 仓内 Markdown 计划文档 → vibe coding 友好

计划全部以 Markdown 落在 `docs/plan/`，随代码版本化、agent 与人都能读。本文是总览；里程碑细节 JIT 落到 `milestones/M<x>.md`。结构见 [§7]。

---

## 2. 全局约束（Guardrails · 红线）

> 源自分析文档 §7.3 避坑清单 + 决策记录。**每个 agent 开工必读，违反先停。**

1. **算钱在后端**：前端只传 `{item_id?, name, description, quantity, unit_price, discount, tax_category_id, ...}` 等原始输入；后端 `services/pricing` 负责 行小计→折扣→计税(含税/不含税/复合/定额)→单据合计→base 换算，统一产出并落盘。金额一律 **`Decimal`**（DB `NUMERIC`，scale≈3），**定死舍入规则与位置**（逐行 vs 合计舍入口径固定）。
2. **多租户用 Postgres RLS，别手动 scope**：即便 v1 单租户，也把数据访问收敛、核心表预留 `company_id`，别散落 `where company=`。详见 [§3.3]。
3. **别手写级联删除**：用 DB 外键 + ORM cascade（SQLAlchemy `relationship(cascade=...)` / `ondelete="CASCADE"`），杜绝孤儿数据。
4. **编号并发安全**：别 `max+1`；用 DB 序列 / 唯一约束 + 重试；且支持**自定义起始与跳号**（迁移旧系统衔接）。
5. **设置别 stringly-typed**：三层设置类型化访问（Pydantic/枚举）+ 缓存，别满地 `'YES'/'NO'`。
6. **税表结构规范化**：别用一张宽表挂一堆可空 FK；用规范多态或「单据级税表 / 行级税表」分表。
7. **渲染用户输入要清洗**：进 PDF/HTML 前过滤（XSS/SSRF），沿用 InvoiceShelf 近期的 sanitizer 思路。
8. **汇率锁快照**：外币按**开票日**锁 EUR 税基（VAT 合规），收款日另算现金/汇兑；历史不漂移（分析文档 §7.4.5）。
9. **不做应用内自更新**：升级走重新部署容器镜像，不给网络留控制容器的入口。
10. **`description` 用 `text`**：别再犯 255 上限的错。
11. **OpenAPI→TS 类型生成**：沿用母版做法，前后端类型一致、契约不漂；CI 强制 drift 检测。
12. **VAT 数据驱动、不写死枚举**：税率与 VAT 处理类别是**用户可增删改的记录**（NL 默认 21/9/0 仅作种子）；「类别→申报格子」映射是国别特定的，与税率表解耦（分析文档 §7.4.2）。

---

## 3. 技术栈与骨架（对齐母版）

### 3.1 技术栈

| 层 | 选型 |
| --- | --- |
| 后端 | **FastAPI** + **SQLAlchemy 2.0 (async)** + **Alembic** + **fastapi-users** + **pydantic-settings**；包管理 **uv**；Python 3.12 |
| 数据库 | **PostgreSQL**（开发期即用，不用 SQLite；asyncpg 驱动）+ **行级安全 RLS** |
| 质量门 | **ruff**（line 100；E/F/I/B/UP/ASYNC）+ **mypy --strict** + **pytest**（asyncio auto） |
| 前端 | **Vue 3 + TypeScript** + Pinia + Vue Router + Vite + **Naive UI** + **ECharts**；**openapi-typescript** 生成 `schema.d.ts` |
| 部署 | **单容器**：三阶段 Dockerfile（前端 `vite build` → 后端 `uv` 装依赖 → runtime 托管 `static/` + uvicorn）；entrypoint 跑 `alembic upgrade head` |
| CI | GitHub Actions：backend-quality / codegen-freshness / frontend-build / docker-build；tag 触发多架构镜像发布 |

> **与母版唯一的实质差异**：母版用 `sqlite+aiosqlite`，本项目从 M0 起就用 `postgresql+asyncpg`（为 RLS 铺路）。其余骨架、Dockerfile 分层、CI 四关、codegen 流程**照搬**。

### 3.2 目录骨架（Python 包 = `jai`）

```
backend/src/jai/
  main.py            # FastAPI app 装配、路由挂载、静态托管 + SPA fallback
  config.py          # pydantic-settings：DATABASE_URL / SMTP / STATIC_DIR / ...
  db.py              # async engine / session / Base；RLS 会话上下文（后续）
  auth/              # fastapi-users：users / backend / deps
  models/            # SQLAlchemy ORM（含 _enums.py、Money/Decimal 约定）
  schemas/           # Pydantic 请求/响应（与 models 分离；不进算钱）
  services/          # 业务逻辑：pricing / numbering / fx / reports ...（算钱在这）
  api/               # 路由（/api/v1/*）：薄控制器，调 services
backend/alembic/     # 迁移
frontend/src/        # api(schema.d.ts) / stores / views / components / router / ...
Dockerfile           # 三阶段单容器
.github/workflows/   # ci.yml / release.yml
docs/                # insight(分析) + plan(本路线图 + 里程碑)
```

### 3.3 「多租户友好 schema + 单租户简单逻辑」落地基线

- v1 **单公司单用户**实现，但 schema 不写死成单例：把「公司/业务主体」建成一张表（v1 仅一行），核心业务表预留归属关系。
- `company_id` 现在就挂还是日后 Alembic 补列，**留到各里程碑建表时定**（默认倾向：核心业务表从 M2 起就挂 `company_id`，给 RLS 留位）。
- RLS / 多用户 RBAC / 多公司切换 UI 等**应用层复杂度 v1 直接不做**，只在 schema 留口。

---

## 4. 里程碑地图 M0–M15（含后插的 M2.5 · 设置 UX 重构 / M6.5 · 成本核算 / M11.5 · 报价定金）

> 依赖：**M0 → M1 → M2 →（M2.5）→ {M3, M4}**，然后分两条可并行的线 ——
> ① **单据线**：M5 → M6 → **M6.5** → M7；② **开支线**：M8（只依赖 M2 本位币 + M4 字典，可选挂 M3 客户，**不依赖发票/报价/收款**）。两线汇合于 M9 → M10，随后进入 **M11（私人交通工具商业里程）→ M11.5（报价定金与最终发票结算）→ M12（预付款/最终发票与贷项通知单）→ M13（Document Artifact 历史补传）→ M14（收据收件箱与审阅）→ M15（Agent 自动收件）**。
> 可并行：M3‖M4；单据线‖开支线（即 M8 可与 M5/M6/M7 同时进行）。**M2.5 是纯前端 UX 修复，非阻塞**——挂在 M2 之后即可，与 M3/M4 并行做也行，不挡任何功能线。每格结尾的「🟢 部署自测点」是该里程碑的验收信号。

### M0 · 地基骨架（walking skeleton）｜对应 P0
- **目标**：单容器跑通的最薄端到端链路。
- **关键内容**：仿母版搭 `jai` 后端骨架 + Vue 前端骨架；接入 **PostgreSQL + asyncpg**；Alembic 基线迁移；三阶段 Dockerfile + entrypoint；CI 四关；`/api/health`；**Money/Decimal 货币基础类型**（`NUMERIC` 约定 + 舍入工具）；i18n 脚手架（EN/ZH 骨架）；`openapi-typescript` codegen 接好。
- **两种运行方式（M0 都要打通，基础 `docker-compose.yml` 含 `app` + `postgres`，DB 不发布宿主机端口；app 只发布到 `127.0.0.1:${APP_HOST_PORT:-8000}` 给本机反代，容器内固定 8000；本地开发叠加 `docker-compose.dev.yml` 只绑定 Postgres 到 `127.0.0.1`，端口可由 dev-only `POSTGRES_DEV_PORT` 调整）**：
  - **开发态**：前端 `npm run dev`（Vite）｜后端 `uv run uvicorn jai.main:app --reload`｜数据库 **`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres`（只起 Postgres 这一个 service）**。
  - **部署态**：`docker compose up -d`（起 **单容器 app = 前端构建产物 + 后端**，外加 **Postgres**；生产从 GHCR 拉镜像，本地集成用 dev override `up --build`），migration service 自动 `alembic upgrade head`。
- **建表/预留**：Alembic 起始；约定 RLS 会话钩子留空实现位。
- **🟢 部署自测点**：本地集成 `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` 起 app + Postgres，生产 `docker compose up -d` 从 GHCR 拉镜像；浏览器打开 `http://localhost:${APP_HOST_PORT:-8000}` 看到（空）占位页 + `/api/health` 返回 ok；CI 全绿。

### M1 · 认证 + 邮件底座｜对应 P1
- **目标**：能注册/登录的私有应用，且邮件能发出去。
- **关键内容**：fastapi-users 用户名+密码（**Argon2**）；**MFA(TOTP)，记账软件必须有**；登录/登出 UI；受保护路由 + 空壳 dashboard。
- **邮件底座（本里程碑一并做掉，后续 M9 直接复用）**：用 Python 现成包实现 **SMTP 发送**；**密码重置邮件**；**设置表基础**（即三层设置的单表 + `level`，先用来存 SMTP 配置）+ **SMTP 配置设置页**（前端填写并保存）。
- **建表/预留**：`user` 绑 `company_id` + `role` 字段建表即留（RBAC 后补，v1 owner 全权）；**设置表（key-value + level）在此落地**，M2 在其上扩公司/用户层。
- **🟢 部署自测点**：注册 → 登录 → 绑定 TOTP → 进到空 dashboard；在设置页填 SMTP → 触发密码重置 → 收到邮件；登出后受保护页跳登录。

### M2 · 公司档案 + 三层设置（补全）｜对应 P1
- **目标**：能配置「你的业务主体」。
- **关键内容**：单例**业务档案**（name/logo/VAT 号/地址/本位币/编号规则）；在 **M1 已落地的设置表**上补全 **三层语义**（global/company/user，scope id，类型化访问 + 缓存，按 user→company→global 回退）+ 公司/用户级设置 UI 页。
- **建表/预留**：`company` 表落地（v1 一行）；设置表的 `level`/scope 设计为多公司友好。
- **🟢 部署自测点**：编辑公司抬头/logo/本位币并持久化；改一个用户级偏好看回退生效。

### M2.5 · 设置入口统一 + 设置 UX 重构（Affine 式可展开面板）｜本路线图新增（UX 修复，不新增后端功能）
- **目标**：把目前散落、图标语义混乱的设置入口收敛成「右上角**一个齿轮 icon → 一个统一的可展开设置面板**」，对齐 Affine 的设置体验。
- **依赖**：M2（公司档案 + 三层设置已落地）。**纯前端重构**，不动后端契约、不碰算钱；**非阻塞**，可与 M3/M4 并行。
- **现状痛点（为什么做）**：
  - 右上角「人头」图标点进去其实是 Preference，旁边又有一个独立「设置」入口、里面只有 SMTP；
  - 从 Preference 进去后右上角图标又变成没有 icon 的 Company／其它，**入口四处分散、图标与内容语义不一致**。
- **边界原则（定位，先认）**：齿轮面板**只装「偏好 / Preference」类设置 + 系统配置**（SMTP、主题…）；**业务主体身份**（抬头 / logo / VAT 号 / 地址 / 本位币 / 编号规则）属于 Company，放在右上角 **Company icon 下的 Company 设置**里，**不进齿轮面板**。两者职责互不重叠。
- **关键内容**：
  - **单一入口**：右上角只留**一个齿轮 icon** 作为设置总入口；与右上角**专门的 Company icon（业务主体身份区）明确区分**，Company 那个不混进设置。
  - **可展开面板（不是纯 Dropdown）**：点齿轮进入一个 Affine 式 **Expandable Menu / 设置面板**——左侧分类、右侧详情，点开即就地编辑。
  - **设置分类收编**（统一进这一个面板）：
    1. **偏好类设置（Preference）**：用户/公司级的偏好开关与默认值（语言、默认值…），**不含业务主体身份**（那是 Company 的事）；
    2. **邮件设置**：SMTP 等，点开即就地填写保存（复用 M1/M2 已有接口）；
    3. **主题 Theme**：暗黑模式、默认主题等现有零散项**全部整合进来**；
    4. **未来扩展位**：后续 AI 等设置项统一从此面板进入（**先留分类位**，本里程碑不实现具体功能）。
- **不做**：不新增后端字段/接口（若需也只做契约对齐，不引入算钱逻辑）；自定义主题、多公司切换不在此里程碑。
- **follow-up（收尾，已排）· 界面语言账号级持久化**：M2.5 主体把语言做成会话级（沿用旧 🌐 行为，刷新回默认）；收尾补一个 follow-up 让语言**像主题一样跟随账号**——给 USER 级 `UserPreferences` 加一个 `locale` 字段、复用 `GET/PUT /settings/me`，前端照搬 `useTheme` 的「localStorage 缓存 + 服务端为准」。这是对上面「零后端」边界**唯一一处有意识的小扩展**（一个字段、无算钱、要重生成 `schema.d.ts`），细节见 `milestones/M2.5.md` 步骤 4。
- **🟢 部署自测点**：右上角只剩**一个齿轮 + 一个 Company icon**；点齿轮打开可展开面板，在面板内分别完成「公司偏好 / SMTP / 主题切换」三类设置并持久化；全程不再出现「人头变 Preference、图标消失」的错乱。

### M3 · 客户｜对应 P2（可与 M4 并行）
- **目标**：能管理客户档案。
- **关键内容**：Customer CRUD + 列表；账单/收货**地址**；每客户默认币种（有交易后锁定）；**国家 + VAT 号**（为 M10 的 ICP / 反向征收判定铺路）；长尾字段用 **JSONB** 承载。
- **建表/预留**：`customer.company_id`；地址用 `type`(BILLING/SHIPPING)。
- **🟢 部署自测点**：新建/编辑/删除客户，填地址与 VAT 号，列表搜索。

### M4 · 字典 / 主数据｜对应 P2（可与 M3 并行）
- **目标**：单据要用的字典就绪。
- **关键内容**：**税种/VAT 处理类别**（数据驱动，NL 21/9/0/免税/反向/EU-B2B/出口作种子，见分析文档 §7.4.2）；收款方式；开支分类（对齐 NL/EU 口径）；币种 + 汇率基础（手填，provider 接口留空）；**产品/材料目录（成本核算 M6.5 的数据底座）**——条目带「采购成本(不含税) + 品类 + 单位 + 该品类默认 Margin Rate」，手动 CRUD + 供应商 Excel 粘贴/导入；margin 与品类一律**数据驱动可改**（红线 12），不写死枚举。
- **建表/预留**：税类别表与「申报格子映射」**解耦**；汇率 provider 抽象接口；产品目录的成本字段是「当前价」，历史快照由用它的 estimate 自己锁（见 M6.5）。
- **🟢 部署自测点**：维护税率/收款方式/开支分类，种子数据可见可改；新建几条产品/材料并设默认 margin，Excel 导入一批供货价。

### M5 · 定价引擎 + 发票核心｜对应 P3（v1 的"心脏"）
- **目标**：能开出一张金额由后端算准的发票。
- **关键内容**：**`services/pricing` 权威计算**（行小计→行/单据折扣→按单/按行计税→含税/不含税→合计→base 换算，定死舍入）；`POST /invoices/calculate` 预览端点；发票 CRUD + 列表；**编号**（模板化 + 可自定义起始 + 跳号 + 并发安全）；**双状态**（生命周期 + 收款状态）；行项目（自由填写 `description` 用 `text`、数量必填、单位可选、可选目录项）。
- **建表/预留**：税表规范化（单据级/行级分表或正规多态）；`unique_hash` 字段留位（公开链接 v1 不启用）。
- **🟢 部署自测点**：建发票、加多行、切按单/按行计税、看后端算出的小计/税/合计；改编号起始号生效。

### M6 · 报价 + 转换 + 内容模板｜对应 P3
- **目标**：报价闭环 + 单据内容复用。
- **关键内容**：报价 CRUD（与发票同构）；**简化状态** draft/sent/accepted/rejected/expired（去掉 viewed）；**到期自动置 expired**（后端定时，APScheduler）；**Convert 报价→发票**；**文档内容模板**（常见工种报价/发票一键填充）；**标准内容块**（保修/T&C/银行信息/付款条款，公司级默认、单据可覆盖）；单据 **Notes**（自由备注 + 可复用模板）。
- **🟢 部署自测点**：建报价、套用内容模板、标记 accepted、一键转发票；到期报价被定时置 expired。

### M6.5 · 成本核算 / 报价辅助（内部 estimation → 报价）｜本路线图新增（作者自用核心工作流，替掉现有 Excel）
- **目标**：用一张**内部**「成本 → 卖价」工作表辅助报价定价（蓝领 / 新能源安装口径）。
- **依赖**：M4 的**产品/材料目录** + M5 **定价引擎**（复用其 VAT/合计层，**不重算税**）+ M6 **报价实体**。排在单据线 M6 之后、M7 之前。
- **关键内容**：
  - **Estimate 实体 + 统一行模型**：每行 `Total = Price × Amount`、`Margin Amount = Total × Margin Rate`、`行卖价(不含税) = Total + Margin Amount`；**labor / shipping / 差旅 / overhead 都是 Margin Rate = 0 的普通行**（不另设行类型）；差旅、overhead 一般挂**单据级各一行**。
  - **三个滚动汇总**：Total Margin（仅设备利润）/ **Total Excl. VAT**（Σ 行卖价）/ Total Incl. VAT（**交给 M5 引擎加 21% VAT**，costing 自己不算税）。
  - **`services/costing` 权威计算**：`Decimal` + 定死舍入（逐行 vs 合计口径在本里程碑细化时钉死）；**算钱逻辑必须单测**（红线 1）。
  - **estimation → 报价联动（非一一对应）**：估算行可**分组**，每组生成**一条报价行**——只带**公开描述（品牌 / kWh 等可公开参数）+ 不含税价（= 该组 Σ 行卖价）**；报价行再走 M5 出含税合计。
- **建表/预留**：Estimate 行**快照**当时的 cost（与 M4 目录解耦，目录后续改价不影响历史）；estimate 与 quote 用「分组 → 报价行」的弱关联，不强制一一对应。
- **本里程碑特有护栏（红线 7/8 的延伸，写代码前先认）**：
  1. **客户面零泄漏**：Estimate 的 cost / Margin / 时薪等字段**永不**序列化进报价 / PDF / 公开链接（红线 7 延伸）。
  2. **成本快照**：更新目录价**不回灌**历史 estimate，历史不漂移（同红线 8）。
- **🟢 部署自测点**：建一张 estimate，加若干设备行（带 margin）+ 人工/运费行（margin 0），看后端算出的不含税卖价与 Total Margin；圈一组生成一条**公开报价行**（确认客户面看不到任何 cost/margin/时薪）；该报价走 M5 出 21% 含税合计。

### M7 · 收款｜对应 P4（可与 M8 并行）
- **目标**：发票能收款、状态自动流转。
- **关键内容**：Payment 实体（关联发票 / 独立）；**部分/多次付款**（首/中/尾款）；收款方式；**收款时锁汇率**（收款日 EUR 口径）；自动重算 `due_amount` 与 `paid_status`。
- **🟢 部署自测点**：对一张发票分两次收款，看 UNPAID→PARTIALLY_PAID→PAID 自动流转。

### M7.5 · 货币舍入口径修正（落到「分」）｜本路线图新增（M5 算钱口径修正，跨单据线/开支线）
- **目标**：让**面向客户 / 对账 / 申报的货币量**统一落到货币最小单位（v1 = EUR = 2 位 / 分），使「前端显示 = 应付额 = 后端对账 = 供应商发票 / 银行流水」自洽；**单价与中间计算保留 ≥3 位**精度不变。
- **缘起**：M7 收款 walkthrough 暴露——发票含税总额存为 3 位（如 `F2026-009 = 3865.166`），UI 显示 `3865.17`，而收款按「分」走永远凑不平。根因是 M5 把**应付总额**也只量化到 3 位、从未落到分（红线 1「舍入位置定死」指的就是这里）。
- **依赖 / 位置**：改 **M5 `services/pricing`**（+ M6.5 对客面 costing、M8 expense 落地时对齐）；排在 **M7 之后、M8 之前**（M8 开支同口径，供应商发票/银行流水本就到分）。
- **关键决策（已冻结 · 详见 `milestones/M7.5.md`）**：**行级到分**（方案 B 伞下子粒度——`数量×unit_price` 乘完即到分、逐行 net/VAT/total 到分、单据=各行相加；多行同率不做组级分摊；`F2026-009`→`3865.16`）；唯 `unit_price` 保留 ≥3 位；舍入方向随 `amounts_include_vat`；适用全部文档级出参（发票/报价/估算对客面/开支）；**不改列类型 / 不改契约 / 无迁移**（`NUMERIC(18,3)` 容得下 2 位）；**不舍到整欧元**（VAT 须按分申报）；引入 `currency_minor_unit`（EUR=2 硬编码 + 口子）；**无历史重算**（项目未上线、全测试数据，旧单据重建即可）；`services/costing`、`services/payment` 零改动。
- **🟢 部署自测点**：重算后 `F2026-009` 同形态发票总额落到分、可按显示金额收满 `COMPLETED`；多行多税率发票每 VAT 组到分且 excl+vat=total 自洽、与供应商发票/银行流水对得上。

### M8 · 开支｜对应 P4（独立线：仅依赖 M2+M4，可与整条单据线 M5/M6/M7 并行）
- **目标**：能记账并智能填单。
- **依赖**：M2（本位币）+ M4（开支分类/收款方式/币种）；可选挂 M3 客户。**不依赖发票/报价/收款**，拿到 M4 即可与单据线并行开工。
- **关键内容**：Expense CRUD + 分类；**收据上传**（图片/PDF，本地存储 + storage 抽象）；**周期性开支**（固定成本按周期自动生成）；**⭐ AI 票据智能填写**（票据图 → 视觉大模型 → 自动填净额/税额/税率/供应商/日期/分类；外部依赖：Claude 等视觉模型 API）；每笔标「是否可抵扣」。
- **（follow-on）⭐ AI 供货价单识别 → 灌入产品目录**：**复用本里程碑的视觉模型管道**，把供应商发的供货价格单（图 / PDF / Excel）→ 自动识别 → 灌进 **M4 的产品/材料目录**（给 M6.5 的成本核算供数）；排在 M8 AI 基建落地之后。
- **🟢 部署自测点**：上传一张票据照片，AI 自动填好开支字段，保存归类；建一条周期性开支看自动生成。

### M8.5 · 开支记账字段补全（对齐作者 NL 记账 Excel）｜本路线图新增（M8 数据模型补全，不新增算钱）
- **目标**：给开支补三个**纯记录**字段，让数据模型反映作者的荷兰个体户记账 Excel——**付款来源**（私人/公司账户）、**业务使用比例**、**折旧年数**。
- **依赖 / 位置**：在 **M8** 开支线之上做 additive 字段扩展；排在 **M8 之后、M9 之前**。**不新增算钱**——当年 Actual Expense / 可退 VAT 按年 / 季度聚合 / BTW 格子等派生口径全部顺延 **M10**（依赖「在报哪一年」，属报表引擎职责）。
- **关键内容**：`paid_by`（PRIVATE/BUSINESS 指示）、`business_percentage`（0–100）、`depreciation_years`（≥1）三列加到 `expense`（+ `recurring_expense` parity）；编辑器/列表 UI + i18n；契约改 → `npm run codegen`。`deductible` 语义不变（= 能否退 VAT）；个人抬头不可退 VAT 由录入法处理（Net 填 Gross、VAT 改 0），不新建模。详见 `milestones/M8.5.md`（D1–D9 已冻结）。
- **🟢 部署自测点**：新建开支可填私人/公司、业务%、折旧年并持久化；M8 期旧开支迁移回填默认（Business/100/1）；范围校验（%∈[0,100]、年≥1）；列表可见三字段；CI 绿 + `schema.d.ts` 无漂移。

### M9 · 输出：PDF（邮件底座已在 M1）｜对应 P5
- **目标**：能把单据交付给客户。
- **关键内容**：**PDF 生成**（一套模板，Jinja2 + WeasyPrint 候选；用户输入清洗）+ 下载（无公开链接，手动发）；**复用 M1 的 SMTP 底座**，加单据**邮件正文模板/占位符** + **Email log**（无已读回执）+ 把 PDF 作附件发出；收款收据 PDF（低优）。
- **建表/预留**：PDF 模板留 CSS 接口（自定义模板 v1 不做）。
- **🟢 部署自测点**：下载一张发票 PDF（抬头/行项目/税/合计正确）；用 M1 配好的 SMTP 把发票邮件 + PDF 附件发出，并在 Email log 看到记录。

### M10 · 报表 / 仪表盘｜对应 P6
- **目标**：报税与经营看得见。
- **关键内容**：盈亏 **P/L**；**⭐ VAT 申报汇总**（按季度 + VAT 类别聚合进 BTW 格子 1a/1b/1e/2a/3a/3b/4a/4b/5a/5b/5c + 生成 **ICP 清单**，口径见分析文档 §7.4，具体数字口径实现时细化）；开支报表；**Dashboard**（ECharts 图）。
- **⚠️ 来自 M4 的待办（必做）**：M4 已建 `vat_treatment.report_box` 列但**留空**。本里程碑要把 `(treatment×rate)→BTW 格子` 的映射口径**对照荷兰税务局（Belastingdienst）官网、与作者逐条共定后**再填表落地——**不得由 agent 自作主张**。详见 `milestones/M4.md` 的「JIT review 已定」与记忆 `vat-model-two-axis`。
- **🟢 部署自测点**：选一个季度，导出 VAT 汇总与 ICP；Dashboard 显示收入/支出/利润图。

### M11 · 里程支出（私人交通工具商用）｜开支线扩展
- **目标**：私人拥有或私人租用交通工具发生商业行程时，只填行程日期和单程公里数（可选往返），后端按生效日费率生成正确 Expense。
- **关键内容**：Expense 页面增加 Purchase/Mileage tabs；公司级可编辑交通工具类型字典；通用按生效日费率 + 可选类型专属覆盖；2024/2025 €0.23、2026 €0.25 的可编辑种子；可选起止地址/目的/备注；后端 Decimal 距离×费率权威计算；Mileage 分类 + Expense 投影进入现有 P/L/Dashboard/Expense Report，VAT=€0；追溯费率修正必须先预览、再确认并留审计。已有 Expense 与 Travel 分类完全不动。详见 `milestones/M11_zh.md`（D1–D18，2026-08-19 冻结）。
- **边界 / follow-on**：M11 只为私人交通工具生成申报；公司车辆走实际成本、留到后续，行程模型留 additive 扩展点。Google Places/Routes 地址联想与路线距离是后续可选 follow-on——M11 不发外部地图请求，手填公里数始终权威。
- **🟢 部署自测点**：配置通用/类型专属费率；创建一条 2026 年汽车行程，单程 12.5 km + 往返，确认 25 km → €6.25；核对 Purchase/Mileage tabs 与现有报表；预览并确认一次追溯费率修正、查看审计；BTW 合计不变。

### M11.5 · 报价定金与最终发票结算｜单据线扩展
- **目标**：最终发票尚未生成时，在已接受报价上记录一笔或多笔定金；转换时原子转挂到最终发票；按收款日确认 VAT，并避免最终发票发出时重复申报。
- **关键内容**：永久保留报价来源的 quote-origin payment；确定性的混合税率 VAT 快照；报价→DRAFT 发票转挂和付款状态重算；最终发票编辑与生命周期守卫；报价阶段非 VAT 收款凭证；最终发票 PDF 逐笔付款；BTW 定金确认与最终发票抵扣；完整的报价/发票/全局付款前端流程。2026-08-28 walkthrough refinement 增加按 locale 单语显示的收据警示与按来源审计的收据邮件。只支持 `NL_DOMESTIC` 定金。详见 `milestones/M11.5_zh.md`（D1–D15，2026-08-27 冻结）。
- **边界 / follow-on**：M11.5 本身不做正式预付款发票、standalone customer credit、退款/负数/超额付款、改挂无关发票、百分比定金计算器、跨境/reverse-charge/export advance、历史税务快照补建或已申报期间更正工作流。M12 现负责独立建模的正式 Advance/Final、绑定来源的 Credit Note 和关联 Credit 的 Refund 路径；receipt-only Quote 定金的退款/重新应用仍延期。
- **🟢 部署自测点**：接受一张含税 €8,000 的境内报价；记录 €1,600 和 €4,000 定金；下载 EN/ZH 报价收据并分别核对匹配的单语非 VAT 警示；发送本地化收据并查看来源单据的审计日志；仅转换一次得到显示已付 €5,600、应付 €2,400 的 DRAFT 发票；验证编辑/删除/再次转换/发出守卫；发出完整发票并收 €2,400 尾款；确认跨季度 BTW 提前确认定金且项目累计只计税一次。

### M12 · 发票生命周期扩展：Advance/Final Invoice 与 Credit Note｜单据线扩展
- **状态**：🟢 已于 2026-09-02 完成、验收并冻结。全部编排步骤和盲审均已收敛；作者完成测试环境中的风险导向 walkthrough，并以自动化证据验收其余低频路径。M13 Document Artifact follow-on 随后于 2026-09-05 完成并验收。详见 `milestones/M12_zh.md`。
- **目标**：把当前 invoice 模型扩展成可审计的单据家族，覆盖一次性 Standard Invoice、分期正式 Advance/Final Invoice、现有个人客户 receipt-only 定金路径，以及用于更正或冲销已发出发票的关联 Credit Note。
- **关键内容**：Quote 锁定的 `DIRECT_INVOICE`/`RECEIPT_ONLY`/`FORMAL_ADVANCE` 模式；`STANDARD`/`ADVANCE`/`FINAL`/`CREDIT_NOTE` 类型；gross/percentage Advance 与一张受守卫 Final；规范化 application 和不可变剩余 credit basis；全额/部分且绑定来源的 Credit；独立 Credit 编号；正数且关联 Credit 的 Refund 台账；replacement/compensation/project-cancellation 流程；相互独立的 lifecycle/settlement/credit 状态；BTW/ICP/P&L/Dashboard 只计一次；发出时交易方快照；保留实际 Download/Send 的精确 PDF 成品；统一列表和完整单据链时间线。
- **冻结边界**：正式 Advance/Final 只支持 `NL_DOMESTIC`；Credit 继承来源现有的全部 VAT treatment。Standalone/unallocated credit、receipt-only Quote 定金退款/重新应用、overpayment、selected-line Advance、多币种/FX、荷兰语 locale 和应用内 VAT filing lock 继续延期。更正只产生结构化 warning。
- **原子交付 / 验收**：11 个实现步骤覆盖底座/迁移、mode+chain 审计、Advance、Final、Credit、重开/取消、Refund 结算、报表、输出/artifact、前端和收尾。自动化覆盖完整 18 项矩阵；作者的风险导向 walkthrough 实测 Direct、receipt-only、50/50、20/50/30 的 21%/0% 混合税率、取消/Credit/Refund 和报表对账；较少使用的 9%、FX、ICP 以自动化证据验收，不再重复人工操作。

### M13 · Document Artifact 历史补传｜单据输出 follow-on
- **状态**：🟢 已于 2026-09-05 完成、验收并冻结。4 个编排步骤、盲审/返工循环及自动化收尾门禁均已收敛；作者通过 Standard Invoice Happy Path，并以自动化证据验收未执行的负向及 Advance/Final/Credit Note 人工路径。详见 `milestones/M13_zh.md`。
- **目标**：让一张尚无任何保留 artifact 的已开具正式 Invoice，接收一份来自旧 JAI 部署、旧开票系统或外部工具的精确历史 PDF。
- **关键内容**：全部四种正式 Invoice kind 仅零 artifact 时可上传 PDF；不可变精确字节 + `UPLOAD` provenance；作为当前 locale/fingerprint 的 canonical Download/Send artifact 复用；后续结算/locale/renderer 变化仍可产生新 artifact；固定提示词、显式可选触发且不阻断的 AI 字段对照 EN/ZH checklist；并发安全的 upload/Download/Send 首动作；复用现有 workflow 的紧凑 UI。
- **冻结边界**：存在任何 artifact 后不可上传；无 replace/delete/primary selector、清理 CLI、artifact/AI audit trail、AI 结果落库、Refund Confirmation 上传、OCR 导入或会计数据变更。保留已有多 artifact 历史。原先的笼统 wrap-up 占位已取消，被移出的运维想法放在 §4.aa。
- **原子交付 / 验收**：四个步骤已交付 contract/enum/文件校验、精确上传+canonical 输出复用、可选 AI 参考对照，以及前端/回归收尾。最终自动化通过 Ruff、mypy、1243 项默认测试和 1074 项 PostgreSQL integration 测试、OpenAPI codegen freshness、148 项前端测试、EN/ZH key 对称、前端与 Docker build 以及 CJK PDF smoke。walkthrough 还产出并验证了按用途限定的 AI 输出预算修正：票据提取 1024 tokens、artifact 比对 4096、能力探测 512。
- **🟢 部署自测点**：向零 artifact 的已开具 Invoice 上传旧 PDF；验证可选 AI warning 从不阻断确认；普通 Download/Send 复用上传的精确字节；改变结算状态后验证出现新 artifact，且原件仍 byte-identical 并可下载。

### M14 · 收据收件箱与审阅｜开支线 follow-on
- **状态**：⬜ 未开始。下述仅为方向；产品决策、API 契约及原子步骤待实施前与作者共同细化。
- **目标**：先上传收据，再创建 Expense；持久保存图片/PDF 原件及 AI 识别建议，之后审阅并确认成完整 Expense。不再为上传收据创建占位 Expense。
- **关键内容 / 边界**：按公司隔离、供网页与未来 Agent 共用的待处理收据流程；复用现有收据验证、存储和 AI 识别。缺失或不确定的字段保留待审阅提示。待处理收据不是已确认 Expense，不进入开支、P/L 或 VAT 报表。确认时复用现有后端 Expense 与金额规则；失败和弃置上传的处理需明确设计。
- **🟢 部署自测点**：网页无需先建 Expense，即可分别上传图片和 PDF；查看已保存原件与 AI 建议；补正缺失字段后各确认一次；验证待处理记录不影响报表，确认后的 Expense 保留对应收据。

### M15 · Agent 自动收件｜依赖 M14
- **状态**：⬜ 未开始。下述仅为方向；鉴权、部署拓扑、API/MCP 契约及原子步骤待实施前与作者共同细化。
- **目标**：让获授权的 Agent（首个场景为通过 Telegram 交互的 Hermes）自动把收到的收据提交到 M14 收件箱，并返回处理结果及审阅链接。接入方式也应供 Codex 等其他支持 MCP 的 Agent 使用。
- **关键内容 / 边界**：在共用后端收件流程之上提供小范围业务 MCP，配以可撤销、权限受限的 Agent 凭据；限制获准触发的 Telegram 发送者/聊天；传递附件；对重复消息保证幂等；明确失败/重试状态并可追踪。AI 建议始终待审阅，作者在网页确认后才正式入账。不直接写数据库，也不自动计入 Expense/VAT 报表。
- **🟢 部署自测点**：向获授权的 Telegram 聊天发送图片和 PDF；Hermes 各提交一次并回复待处理结果及审阅链接，重复投递不重复建档；在网页审阅确认；未授权消息和处理失败都不会生成已确认 Expense。

---

## 4.x 路线图之外（vNext）· 外部银行流水接入（仅备忘，不在 M0–M15）

> **状态**：明确**不做**于当前至 M15 的路线图；这里只留一笔备忘，方便日后重拾项目时心里有数。**现在不预留任何 DB schema**——将来要加列/加表都是 additive 迁移，旧数据不受影响，成本可接受，不必提前留位。

- **是什么**：接一个**外部交易供应商**，通过 API 自动拉取合作银行的交易流水（transaction），省去手工录入。**provider 无关**：Plaid（YNAB 背后用的就是它）、GoCardless Bank Account Data（前身 Nordigen，EU 开放银行、AIS 免费）、Tink / TrueLayer… 都可，选型到时再定。
- **两个用途**：
  - **收款对账**：读流水 Description（可借大模型识别 Invoice Number 等）→ 尝试匹配现有发票 → 关联 / 建议一笔 Payment（接 M7）。
  - **开支导入**：把流水直接导成开支，先填已知字段（金额 / 日期 / 对方）；做账时再用 **M8 的 AI 票据填写**补全净额 / 税率 / 类别（接 M8）。
- **同步语义（关键产品取向，YNAB 式）**：
  - **只从首次连接之后开始同步**；**历史旧数据不回灌**也没关系，连接时把现状 consolidate 一下即可。
  - **匹配与否不强求**：对账是 best-effort 的便利功能，不是账目正确性的前提。
  - **银行流水不是唯一真相**：小商户会有**不走银行账户的现金收支**，所以两边**不要求完全真实同步**；流水只是「方便的录入来源」之一。具体对账 / 去重逻辑到时再议。
- **落地取向（真做时遵守）**：**轮询而非 webhook**（自托管不开 inbound 入口，红线 9）；provider 走**抽象接口**（同 M4 汇率 provider）；凭证走 M2 类型化设置 + at-rest 加密。同步频率低（一天一两次足够），pay-as-you-go 成本友好。

---

## 4.y 路线图之外（vNext）· PDF 文档/抬头模板自定义（仅备忘，不在 M0–M15）

> **状态**：作者 2026-06-14 M9 walkthrough 提出、明确**顺延**。M9 的 OUT「自定义 / 多套 PDF 模板 → 顺延（一套模板族 + CSS 接口）」在此细化。**现在不预留 schema**，将来都是 additive。
>
> **已提前落地一部分（2026-06-29）**：最小子集——公司 `legal_name` 字段 + 发票/报价/收据 PDF 每页页脚自动渲染一句「trade name of」声明——页脚开头由原本的商号替换为 `{trade} is a trade name of {legal}`（随 locale EN/ZH，仅在填了 `legal_name` 时出现；空白/纯空格视为未填）——已提前实现（迁移 0025、label `trade_name_disclosure`；orchestrator 单步实现，盲审零 finding）。**完整模板编辑器**（可自由调顺序的 `{{ }}` 占位符块）仍属 vNext，见下文。

- **是什么**：把发票/报价 PDF 模板做成**设置里可编辑**，**仿 M9 的 email 模板**——纯文本 + `{{ }}` 占位符（如 `{{COMPANY_NAME}}` / `{{EMAIL}}` / `{{ADDRESS}}` / `{{LEGAL_NAME}}` …），且这些块的**位置 / 顺序可自由调整**。**主要针对抬头**（公司身份块），例如可选加一句「Trade name of <legal name>」，有时加、有时不加。
- **已有可复用**：typed 设置 + 三层 locale 解析链 + 设置面板（齿轮可展开面板）+ 占位符引擎，都是 M9 email 模板那套现成基建（doc_type × locale）。
- **大头是安全（红线 7）**：用户输入进 PDF = XSS/SSRF 面，需沿用 / 加强清洗——参考 2026-06-14 修过的 `{{ css | safe }}` 字体转义与 SVG `<style>` 内联清洗两处坑；正文走「纯文本 + 显式占位符 + 转义」而非任意 HTML/CSS。外加模板编辑器 UI + 预览 + 无值回退内置默认。
- **粒度**：按单据类型（invoice / quote）× 语言（EN / ZH）分别配，沿用 email 模板结构。

## 4.z 路线图之外（vNext）· 客户地址自由文本块（仅备忘，不在 M0–M15）

> **状态**：作者 2026-06-14 M9 walkthrough 提出、明确**顺延**。

- **是什么**：在客户**结构化地址**（街道 / 门牌 / 邮编 / 省 / 市 / 国家）**下方**，加一个**自由输入文本框**。
- **场景**：**双语客户**——结构化格子按现有字段填拉丁 / 英文地址；自由文本框里**整段再抄一遍另一种文字**（如中文）的完整地址。
- **落地草图（真做时）**：`address`（或 customer）模型加一个 additive 文本列（红线 10：`text`）+ schema + 客户编辑器 UI + 发票 / 报价 PDF 在结构化地址块下渲染该自由文本（保留换行、autoescape）。范围有界，但横跨 模型 + UI + PDF 多处。

## 4.aa 路线图之外 · 运维加固与维护积压（仅备忘）

> **状态**：作者于 2026-09-02 取消了原未拆步的 M13“收尾 / GA 前体检”占位，并将 M13 改为范围有界的 Document Artifact 历史补传。下列事项**不是 M13 验收条件**，也没有已排期里程碑。只在某项具有明确价值和范围时，再把它提升为单独设计的里程碑。

- **运维积压**：备份/恢复脚本与完整恢复演练（`pg_dump` + 上传收据 volume）、扩充部署/运维文档，以及今后基于具体 finding 而非笼统 polish 的安全/性能加固。
- **维护积压**：M8.5 Expense 前端展示 refinement、M10 Dashboard 死 `_MONTH_LABELS`/未用 `dashboard.loadError`、可选报表 PDF/CSV 导出，以及每次变更已强制 symmetry 门禁之外的广泛 EN/ZH 总复查。
- **持续规则**：i18n symmetry、安全回归覆盖、适用时的性能/query-count 检查、Docker build 和文档对称仍属于日常 Definition of Done；把笼统清理移到这里不会削弱这些门禁。
- **迁移基线化仍为已取消（2026-06-19 决定）**：`v0.1.0` 已于 2026-06-17 上线自用，生产库已有真实数据，`alembic_version` 跟随现行迁移链。保留累积的线性 Alembic 迁移；新库完整重放，今后只做 additive 迁移。
- **Release `latest` 处理仍为已完成（2026-06-17）**：`release.yml` 使用 `flavor: latest=auto`；只有稳定 semver tag 移动 `:latest`，预发布不会。

---

## 5. 每个原子步骤的模板 + Definition of Done

> 细化里程碑时，每个原子步骤都按这个模板拆（见 `milestones/_TEMPLATE.md`）。**单人开发不强制 PR**：自测 + CI 绿即可合 `main`；想做人工 review 时再开分支/PR。

**一个原子步骤包含：**
- **目标**：一句话。
- **契约**：本步新增/改动的 API schema（OpenAPI 片段）——**先定**。
- **后端任务** / **前端任务**：两栏，可并行（都对着上面的契约）。
- **迁移**：Alembic（如涉及建表/改列）。
- **测试**：后端 pytest（services 计算逻辑必测）；必要时前端基本校验。

**Definition of Done（每步都要全过）：**
- [ ] `ruff check` + `mypy --strict` 通过
- [ ] `pytest` 通过（pricing/numbering/fx/报表等**算钱逻辑必须有单测**）
- [ ] 若改了契约：`npm run codegen` 重生成 `schema.d.ts`，CI drift 检测过
- [ ] `frontend` 构建通过（`vue-tsc + vite build`）
- [ ] **部署冒烟**：`docker build` 通过；该里程碑的「🟢 部署自测点」能手动点到
- [ ] **CI 四关绿**（绿即可合 `main`，单人开发不强制 PR）
- [ ] 遵守 [§2 全局约束]，无违反项
- [ ] 自查：命名/分层与母版一致，`description` 用 `text`，算钱在 `services`

---

## 6. 部署与自测 loop

- **开发态**：前后端分离——前端 `npm run dev`（Vite，代理 `/api`），后端 `uv run uvicorn jai.main:app --reload`，数据库用 `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d postgres`（只起 Postgres 这一个 service，DB 端口只绑定 `127.0.0.1`，host 端口可由 dev-only `POSTGRES_DEV_PORT` 调整）。
- **部署态 / 里程碑验收**：生产 `docker compose up -d`（**单容器 app + Postgres**，从 GHCR 拉镜像；migration service 自动 `alembic upgrade head`），本地集成用 `docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build` → 浏览器走该里程碑的「🟢 部署自测点」。
- **CI**：每次 push 跑四关；`main`/PR 额外跑 docker-build；打 `v*` tag 发布多架构镜像。绿即可合 `main`（单人开发不强制 PR）。
- 每个里程碑结束在 `docs/plan/milestones/M<x>.md` 记一行验收结论 + 已知遗留。

---

## 7. 计划文档结构 & JIT 细化流程

```
docs/
  insight/btw-aangifte-2026-guide.md # 荷兰 VAT/BTW 申报口径（权威，税局官方说明的结构化指南）
  plan/
    roadmap.md                       # 本文：总览 + 方法论 + 约束 + M0–M15
    milestones/
      _TEMPLATE.md                   # 里程碑细化模板
      M0.md, M1.md, ...              # 动手前 JIT 产出（原子步骤清单）
```

**JIT 细化流程（每进入一个里程碑）**：
1. 对 agent 说：「读 `docs/plan/roadmap.md` 的 §2 约束 + 目标里程碑那一格，再读分析文档对应章节，然后用 `milestones/_TEMPLATE.md` 把 M<x> 拆成原子步骤。」
2. 评审该里程碑的原子步骤清单（必要时回填分析文档里"待细化"的产品决策，如单位是否可选、VAT 数字口径等）。
3. 逐步骤实现，每步过 [§5] DoD。
4. 里程碑收尾：走「🟢 部署自测点」，记录验收结论。

---

## 进度追踪

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 地基骨架 | 🟢 完成 |
| M1 | 认证 + 邮件底座 | 🟢 完成（2026-06-05；步骤 1–4 + 部署自测点 1–7 全过） |
| M2 | 公司档案 + 三层设置（补全） | 🟢 完成（2026-06-08；步骤 1–4 + 部署自测点 1–6 全过） |
| M2.5 | 设置入口统一 + 设置 UX 重构（Affine 式可展开面板） | 🟢 完成（2026-06-08 步骤 1–3 + 自测点 1–6 + SMTP radio follow-up；2026-06-09 步骤 4 语言账号级持久化收尾 · 全过） |
| M3 | 客户 | 🟢 完成（2026-06-09；步骤 1–4 + 部署自测点 1–8 全过） |
| M4 | 字典 / 主数据（+ 产品/材料目录） | 🟢 完成（2026-06-10；步骤 1–5 + 部署自测点 1–8 全过） |
| M5 | 定价引擎 + 发票核心 | 🟢 完成（2026-06-11；步骤 1–4 + 部署自测点 1–11 全过） |
| M6 | 报价 + 转换 + 内容模板 | 🟢 完成（2026-06-11；步骤 1–6；自测点 1–9 手动通过，10–11 集成测试覆盖，12 待 CI） |
| M6.5 | 成本核算 / 报价辅助（内部 estimation → 报价） | 🟢 完成（2026-06-12；步骤 1–5 + 部署自测点 1–8 全过；987 测试绿） |
| M7 | 收款 | 🟢 完成（2026-06-13；orchestrator 5 步，每步盲审+返工收敛；ruff/mypy/单测 404/集成 641/codegen 无漂移/build 全绿；人工 walkthrough 自测点 1–8 通过，#9 单币种 UI 待 FX 前端落地后补、隔离/cascade 由集成测试覆盖）。收款 sub-cent 边界（3 位总额不可按分收满）顺延 M7.5 |
| M7.5 | 货币舍入口径修正（落到「分」） | 🟢 完成（2026-06-13；orchestrator 3 步逐步盲审收敛，步骤2 一处 docstring fixup、步骤1/3 零 finding；**行级到分**、`F2026-009`→`3865.16`；ruff/mypy/单测 426/集成 644（+3 F2026-009 收满回归）全绿；零迁移/零契约/无 codegen；payment/costing/estimate 服务代码零改动；作者人工 walkthrough 自测点 1–3 通过、无 finding） |
| M8 | 开支（含 AI 填单 + AI 供货价单识别，可与单据线并行） | 🟢 完成（2026-06-14；orchestrator 5 步逐步盲审收敛 [expense+分存 / storage 收据 / 周期开支 / AI 票据填单 / 前端收尾]；末轮 walkthrough refinements 单列一轮压进同一收尾 commit [可抵扣随分类联动 / 收据 bind-mount uid1000 / AI 探针 64×64 / 注入当前日期 / 摘要写 note 跟随界面语言 / 选率自动算 VAT 后端端点 / 提示词「默认常驻+自定义追加」]；ruff/mypy/单测 599/集成/build/无漂移全绿；自测点 1–5 人工通过、7 集成覆盖、8 通过，6 周期性开支作者暂不用未走（不影响验收）、9 远端 CI 待确认。AI 走 OpenAI 兼容 Chat Completions（`httpx` 自构造、非 SDK；base_url/model/key/提示词 用户自填 + 多模态测试；PDF 用 pypdfium2 栅格化成图统一走 image_url）。**follow-on 未做**：AI 供货价单 → M4 目录） |
| M8.5 | 开支记账字段补全（付款来源 / 业务% / 折旧年，对齐 NL 记账 Excel） | 🟢 完成（2026-06-14；orchestrator 2 步逐步盲审收敛，两步**零 finding / 零返工**；3 个**纯存储** additive 字段 [`paid_by` / `business_percentage` / `depreciation_years`]，`expense` + `recurring_expense` parity，迁移 0021 NOT NULL+server_default 自动回填；**不新增算钱**——当年摊销 / 可退 VAT 按年 / 季度 / BTW 全顺延 M10；ruff/mypy/单测 626[+27]/集成切片 79[+19]/build/无 codegen 漂移全绿；作者人工 walkthrough：后端/迁移/契约/周期 parity 通过，旧数据迁移回填因已删旧数据+未上线略过实测、walkthrough 发现的**前端展示问题移入未排期的 §4.aa 维护积压**；D1–D9 与作者逐列对照 Excel 共定） |
| M9 | PDF（邮件底座在 M1） | 🟢 完成（2026-06-14；orchestrator 8 步逐步盲审收敛，步骤 1/4/5 各 1 轮返工[Content-Disposition RFC6266 filename / 收据标签键 / 集成缺建公司]、步骤 2/3/6/7 零 finding，每步一 commit；WeasyPrint+Jinja，发票/报价/收据 PDF 按 locale 下载[解析链 override>customer>company>en]、公司级可编辑邮件模板+占位符引擎、email_log 发送[附件+抄送, SENT/FAILED 脱敏]、迁移 0022 customer.locale / 0023 email_log、前端下载+发送对话框+Email log；ruff/mypy/默认 760/集成 785/build/i18n EN-ZH 对称 1001/docker build+镜像内中文 PDF 渲染 全绿。**作者人工 walkthrough 通过**，walkthrough 提出并已修[均 Opus 盲审无 finding、各自 commit]：① PDF 应用内预览[`0f75310`，同 commit 含发票/报价版式：删 Description 列+Item 加粗描述下挂+全 2 位小数 money2/pct+`css\|safe` 字体修复] ② SVG logo `<style>` 类内联清洗[`4cfd369`，class-styled logo 不再纯黑，**需重新上传 logo**] ③ 多页每页页脚[`b9090ae`]；改后默认 802/PDF 集成 138 全绿。**顺延**：完整 PDF 抬头模板自定义→§4.y、客户地址自由文本块→§4.z、公开链接/unique_hash、已读回执、收据邮件/多笔汇总收据、PDF 缓存/队列、NL 语言 PDF、VAT 报表→M10、渐变 SVG logo 不支持） |
| M10 | 报表 / 仪表盘（含 VAT 申报） | 🟢 完成（2026-06-15；orchestrator 5 步逐步盲审收敛 [P/L → ⭐BTW 申报汇总 → ICP → 开支报表 → Dashboard]，每步一 commit `89ab353`→`273ed75`；ruff/mypy/单测 966+集成 788/codegen 无漂移/build/docker build 全绿。**税法决策 2026-06-15 与作者对照官方指南 `docs/insight/btw-aangifte-2026-guide.md`（Opus 通读 41 页）逐条共定冻结**：NL ruleset 按 `company.country_code` 选+其它国 fallback+banner、hoog/laag/zero 税率档位落盘默认 21/9/0、5b 全额抵+私用走 1d（年末按 business% 算）、5a/净应缴为辅助合计（官方只命名 5b、不标 5c）、EU 内采购=4b（非 art.23 进口）、非欧盟进口/境内反向征收/OSS/herziening/KOR 均 N/A v1、报表带免责声明。**作者导入 2026 Q1–Q2 数据人工 walkthrough 通过**，walkthrough 发现并已修 [均 Opus 盲审无 finding、各自 commit]：① Expense 日期选择器 off-by-one [`1a5a94a`] ② 对外单据抬头泄漏客户花名→派生 billing_name [`853f07c`] ③ P/L 月/季粒度换成 MTD/QTD/YTD 周期预设+高亮由区间派生 [`df2ba13`]。详见 `milestones/M10.md` 验收结论。**顺延**：多币种 ICP/3b 列分叉留 FX；Dashboard 死常量/未用键移入未排期的 §4.aa 维护积压） |
| M11 | 里程支出（私人交通工具商用） | 🟢 完成（2026-08-21；orchestrator 步骤 1–5 盲审收敛；完整自动化门禁全绿；作者 walkthrough 验收；两项 walkthrough UX 修复均经零 finding 复审；见 `milestones/M11_zh.md`） |
| M11.5 | 报价定金与最终发票结算 | 🟢 完成（2026-08-28；2026-08-27 base 实现的恢复式编排盲审与里程碑跨步骤总审均收敛至无 finding；编排式收据邮件 refinement 增加本地化单语警示与按来源审计的邮件，经三轮 fixup/复审收敛，并通过 Ruff/mypy/默认 1067/integration 872/migrations 14/codegen 无漂移/build/i18n 1236/Docker；作者人工 walkthrough 验收无 finding；无法重建 base milestone 历史逐 Step commits） |
| M12 | Advance/Final Invoice 与 Credit Note | 🟢 完成（2026-09-02；编排 Step 1–11 与盲审均收敛；Ruff/mypy/默认 1125/integration 1065、迁移、codegen、前端 Node/Vitest 122 + build + EN/ZH 1423、Docker/CJK PDF 全绿；作者风险导向 walkthrough 与最终测试数据只读审计通过；5 项 walkthrough refinement 已修复） |
| M13 | Document Artifact 历史补传 | 🟢 完成（2026-09-05；编排 Step 1–4 与盲审均收敛；AI 输出预算 walkthrough 修正复审零 finding；Ruff/mypy/默认 1243/integration 1074、迁移、codegen、前端 148 + build + EN/ZH 1458、Docker/CJK PDF 全绿；作者 Standard Invoice Happy Path 通过，其余人工路径以自动化证据验收） |
| M14 | 收据收件箱与审阅 | ⬜ 未开始；仅列高层范围，具体设计待 JIT 细化 |
| M15 | Agent 自动收件（MCP；Hermes/Telegram 优先） | ⬜ 未开始；依赖 M14，具体设计待 JIT 细化 |

> 图例：⬜ 未开始 ｜ 🟡 进行中 ｜ 🟢 完成（已过部署自测点）
