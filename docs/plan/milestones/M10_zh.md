# M10 · 报表 / 仪表盘（含荷兰 VAT/BTW 申报）

> 🌐 [English](M10.md) · **中文**

> 进入本里程碑前 JIT 产出。先读 `docs/plan/roadmap.md` §2 全局约束（**尤其红线 1 算钱在 services / 红线 12 VAT 数据驱动**）+ M10 那一格；荷兰 VAT/BTW 申报口径以 **`docs/insight/btw-aangifte-2026-guide.md`（权威）** 为准；以及落盘字段来源 `milestones/M5.md`（发票 VAT 快照）、`milestones/M8.md`+`M8.5.md`（开支 VAT / `business_percentage` / `depreciation_years`）。
>
> **状态**：**🟢 完成（2026-06-15 作者人工 walkthrough 通过；见文末「验收结论」）**（2026-06-14 起草；2026-06-15 与作者对照官方指南逐条共定全部税法决策 → orchestrator 5 步逐步盲审收敛实现 → 作者导入 2026 Q1–Q2 数据 walkthrough 通过）。roadmap M10 格的「⚠️ 来自 M4 的待办」——`(treatment × rate) → BTW 格子` 映射——已**对照荷兰税务局官方指南 `docs/insight/btw-aangifte-2026-guide.md`（Opus 通读 41 页）+ 作者实践共定**，见下「✅ 产品/税法决策」。`vat_treatment.report_box` 仍保留为声明性提示，权威映射在 `services/reporting` 的 NL ruleset 纯函数（D-MAP-IMPL）。
>
> **缘起**：v1 报税与经营看得见——P/L 盈亏、**BTW 季度申报汇总（格子 1a…5c）+ ICP 清单**、开支报表、Dashboard（ECharts）。同时**消化 M8.5 顺延来的派生口径**：`business_percentage` 砍可退 VAT + 砍成本、`depreciation_years` 跨年摊销——这些「依赖在报哪一年」的算钱全部归本里程碑（见 `M8.5.md` D1/D3/D4 与第 60 行「留给 M10 对账」提醒）。

---

> **计划后续** [M20](M20_zh.md) 计划将固定资产折旧纳入 P&L 和 Dashboard 的共用投影，并支持年度确认混合用途的暂估损益成本。

## 目标与范围

- **目标**：选一个季度能导出 BTW 申报汇总与 ICP；选任意区间能看 P/L 与开支报表；Dashboard 用 ECharts 展示收入/支出/利润。全部金额由后端 `services/reporting` 权威计算（`Decimal` + 定死舍入，红线 1），算钱逻辑必须单测。

- **纳入（IN）**：
  1. **`services/reporting` 报表引擎**（新模块）：纯读，聚合既有落盘数据，不新增业务写入。
  2. **P/L 盈亏报表**：区间内 销项净额（发票）− 开支实际入账额（含 `business_percentage` × `depreciation_years` 摊销）= 利润；按月/季时间序列 + 汇总。
  3. **⭐ BTW 申报汇总**：按季度 + `(vat_treatment × vat_rate)` 聚合进荷兰申报格子 **1a/1b/1e/2a/3a/3b/4a/4b/5a/5b/5c**（具体映射 🔴待共定）；销项取自发票行级 VAT 快照、进项取自开支 VAT 快照（× `business_percentage`）。
  4. **ICP 清单（Opgaaf ICP）**：`requires_icp` 的 EU-B2B 销售按**客户 VAT 号 + 国家**分组，给出每季每客户的净额（对账 BTW 3b）。
  5. **开支报表**：区间内按 `expense_category` 聚合净额/VAT/毛额 + 可抵/不可抵拆分。
  6. **Dashboard**：替换现 `Dashboard.vue` 占位页，ECharts 出 收入/支出/利润 时间序列 + 关键 KPI（本季/本年）。
  7. 全部报表页 i18n EN/ZH；契约新增 → `npm run codegen` 无漂移（红线 11）。

- **不纳入（OUT / 顺延）**：
  - **官方申报表的电子报送 / Digipoort / XBRL 对接** → 不做（v1 只产出供作者**手动填税局表**的汇总数字）。
  - **客户销售报表 / 商品销售报表** → 顺延（roadmap 7.1F 标 🔜，价值低）。
  - **`report_box` 改成可视化「映射编辑器」UI / 多国申报表结构** → 不做（红线 12 的解耦在**服务层 NL ruleset**里满足即可，多国是 vNext；见 D-MAP）。
  - **EU-B2C 跨境远程销售的 OSS/3c 阈值自动切换** → **作为独立子决策**（D-OSS），默认**不做自动切换**、按 NL 税率进 1a/1b 并在报表上标注，除非作者确认有此业务量。
  - **KOR 小规模免税方案** → v1 不启用（§7.4.6，作者正常收 VAT），留 OUT；本里程碑不实现 KOR 归零逻辑。
  - **herzieningsregeling（投资品进项 VAT 调整规则）** → **OUT**（D-DEP）。进项 VAT 购入当季全额退即可；逐年追踪业务比例变动 + 10% 阈值的多年纠偏（动产 5 年 / 不动产 10 年）v1 不做。若 `business_percentage` 日后跨年变动，v1 不自动修正历史抵扣。
  - **境内反向征收（1e 供方 / 2a 接收方 / `NL_REVERSE`）** → **N/A v1**。作者实践：分包一律走正常 21%（小工/分包开含税票、作者抵进项、向客户正常收税），**不用境内 verleggingsregeling**；种子未含，格子留位不计算（§7.4.7、2026-06-15 作者确认）。
  - **非欧盟进口（`IMPORT_NON_EU` → 4a / vergunning artikel 23）** → **v1 不实现**。「进口」专指**欧盟外**进口（欧盟内 B2B 买货是 intra-community verwerving → 4b，与 art.23 无关）；作者目前无欧盟外进口业务，将来有再 additive（设计已厘清：持 art.23→4a 自核+5b 抵，无→海关代缴+5b）。
  - **多国申报 ruleset（NL 以外）** → vNext。v1 只实现 NL ruleset，其它国家 fallback NL + banner（D-COUNTRY）。
  - **公司车私用 forfait（catalogusprijs 的固定 %）自动算** → 不做（官方 PDF 未给公式）；1d 只按 `business_percentage` 自动算，公司车定额私用作者手工调（D-1D）。
  - **现金口径 / 汇兑损益报表**（收款日汇率）→ 不做（v1 单一本位币 EUR，发票/开支 `exchange_rate=1`，base_* 即 EUR；多币种现金口径属 FX 后续）。
  - **报表导出 PDF/CSV 文件** → 本里程碑只做**屏幕展示 + 可复制数字**；导出格式继续无排期地放在[路线图 §4.aa](../roadmap_zh.md)，除非作者要求单独定界里程碑。

- **对应文档**：`docs/insight/btw-aangifte-2026-guide.md`（BTW 申报口径权威）；roadmap M10、§2 红线 1/6/12；`M4.md`（report_box 留空的约定）、`M5.md`（发票 VAT 快照列）、`M8.5.md`（business%/折旧顺延本里程碑）。

---

## ✅ 产品 / 税法决策（2026-06-15 与作者逐条共定，已冻结）

> 这些曾是「不得 agent 自作主张」的税法决策；**2026-06-15 作者对照官方指南 `docs/insight/btw-aangifte-2026-guide.md` review 后已全部拍板**（D-MAP/D-DEP/D-PCT/D-OSS/D-1D/D-COUNTRY/D-BOX5/D-DISCLAIMER + 默认项 D1–D6）。可施工。

### D-MAP（🔴 核心 · 必须对照 Belastingdienst 逐条确认）· `(treatment × rate) → BTW 格子` 映射

> **这是 M4 留给 M10 的那张表。** 关键事实：格子 1a/1b/1e 由**税率**区分，而 `vat_treatment` 是**税率无关**的（两轴设计）——所以映射不是「treatment→单格子」一对一，而是 **`(side, effect, requires_icp, rate%)` → 格子**，必须由 rate 参与。因此**单靠 M4 的 `report_box` 列存不下**（一列存不了「21→1a / 9→1b / 0→1e」）。
>
> **📕 官方口径权威已落地**：`docs/insight/btw-aangifte-2026-guide.md`（Opus 通读税局《Toelichting bij de aangifte omzetbelasting (btw) 2026》全 41 页的结构化指南，逐格定义 + 逐行对照本提案 + 页码依据）。**最终 D-MAP 以该指南 + 作者 review 为准**；下表是初稿，官方指南已对它做出 3 处关键修正（**待作者 review 后并入**）：
> 1. **5a/5c/5d/5e 编号在 2026 toelichting 里不存在**——只官方命名 `5b`（voorbelasting）；应缴/应退合计由申报程序自动算（我们若展示 5a/5c 是「展示性合计」，5d/5e 不建格子）。⇒ 契约 `VatReturnReport.boxes` 与步骤 2 据此调整。
> 2. **行 9（IMPORT_NON_EU→4a）须按是否持 `vergunning artikel 23` 分叉**：有 art.23 → 4a 自核 + 5b 抵；无 → 进口 VAT 海关代缴、不进 4a、凭单据进 5b。
> 3. **行 4（EU_B2C→1a/1b）只在未超 €10,000 远程销售阈值时成立**（作者已确认无此业务量 → v1 保持 1a/1b + 静态注记，不建阈值监控；3c 置 0）。
> 另：1e 还含「境内反向征收**供方侧**净额」、2a 含「EU 不动产服务（4b 例外）」、1d 私用补税**不能简单置 0**（有公司车私用等情形须年末填）——细节见指南 §2/§4。
>
> **初稿提案（已对照 `btw-aangifte-2026-guide.md` 校验、作者 2026-06-15 review 冻结；下表为最终口径）：**

| # | 来源 side | treatment code（现有种子） | effect | rate | → 格子 | 报入该格子的量 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 销售 | `NL_DOMESTIC` | APPLY_RATE | 21% | **1a** | 净额（taxable base）+ 销项 VAT |
| 2 | 销售 | `NL_DOMESTIC` | APPLY_RATE | 9% | **1b** | 净额 + 销项 VAT |
| 3 | 销售 | `NL_DOMESTIC` | APPLY_RATE | 0% | **1e** | 净额（无 VAT） |
| 4 | 销售 | `EU_B2C` | APPLY_RATE | 21/9% | **1a/1b** | 净额 + 销项 VAT（v1 按 NL 税率；OSS 见 D-OSS） |
| 5 | 销售 | `EXPORT_NON_EU` | ZERO_EXPORT | 0% | **3a** | 净额 |
| 6 | 销售 | `EU_B2B_REVERSE` | ZERO_REVERSE | 0% | **3b** | 净额（**并进 ICP 清单**，对账 D-ICP） |
| 7 | 开支 | `NL_DOMESTIC_PURCH` | APPLY_RATE | 任意 | **5b** | 全额可抵进项 VAT（`vat × deductible`，**不预扣 business%**；私用部分走 1d，见 D-PCT/D-1D；净额不单列入格子） |
| 8 | 开支 | `EU_B2B_REVERSE_PURCH` | ZERO_REVERSE | 任意 | **4b + 5b** | 4b：自核（净额 + 自核 VAT）；5b：同额抵扣 → 净影响 0。**✅ = 作者从比利时 B2B 买货那笔（intra-community verwerving）**，与 art.23/进口无关 |
| 9 | 开支 | `IMPORT_NON_EU` | APPLY_RATE | 任意 | **N/A v1（不实现）** | 仅「**欧盟外**进口」才用，作者目前**无此业务** → v1 不实现。设计已厘清：持 `vergunning artikel 23` → 4a 自核 + 5b 抵；无 → 进口 VAT 海关代缴、不进 4a、凭单据进 5b。将来有需求再 additive |
| 10 | 开支 | `EU_B2C_PURCH` | APPLY_RATE | 任意 | **不映射任何格子** | ✅ 官方明确：在别的 EU 国作为消费者付的外国 VAT **不得进荷兰申报**（指南 §4 问题④）；既不入格子也不抵 5b |
| 11 | 辅助 | — | — | — | **销项合计**（≈5a，不标号） | = `1a+1b+1c 的 VAT + 1d + 2a + 4a + 4b 自核 VAT`（参考位，与税局网站自动算的数核对；D-BOX5） |
| 12 | 权威 | — | — | — | **5b** | = Σ 全额可抵进项 VAT（7/8 的进项 + 2a/4a/4b 自核可抵；私用缩减不在此、在 1d） |
| 13 | 辅助 | — | — | — | **总应缴/应退**（`Totaal te betalen/terug te vragen`，**不叫 5c**） | = 销项合计 − 5b（税局措辞；申报时税局工具自动算，D-BOX5） |
| 14 | 保留位 | `NL_DOMESTIC`（境内反向征收 1e 供方 / 2a 接收方） | — | — | **1e 供方 / 2a 接收方** | **N/A v1**：作者分包一律走正常 21%（小工/分包开含税票，作者抵进项；§7.4.7），**不用境内 verleggingsregeling**；1e 反向征收供方侧 + 2a 留位不计算（1e 仍接「境内 0% 销售」，见行 3） |

> **6 个待拍板点 → 2026-06-15 作者 review 已全部解决**：① 1a/1b/1e 按 hoog/laag/zero 分档✓（数值走 D-COUNTRY 落盘口子，默认 21/9/0）；② EU_B2C 按 NL 税率进 1a/1b✓（无 OSS 业务，D-OSS）；③ 进口 = 欧盟外才用、作者无此业务 → N/A v1（行 9）；④ EU_B2C_PURCH 不进申报✓（行 10）；⑤ 5a 含 2a/4a/4b 自核 VAT✓（D-BOX5）；⑥ 1c/3c 留格子默认置 0、1d 按当年 expense 算（D-1D）。
> **落地形态**：映射写成 **`services/reporting` 里的 NL ruleset 纯函数**（rate-aware、按 `company.country_code` 选国别 ruleset，见 D-COUNTRY/D-MAP-IMPL），**不靠 M4 的 `report_box` 单列**。`report_box` 列保留为「人类可读声明 / UI 提示」，不作权威计算源。

### D-DEP（✅ 已冻结 2026-06-14 · 作者定 + Belastingdienst 查实；2026-06-15 作者二次查实再确认「VAT 当期直接抵、成本按年摊」）· 折旧 `depreciation_years` 在 P/L 与 VAT 里怎么算
> 承接 `M8.5.md` 第 60 行的「留给 M10 对账」提醒。两个分支已分别拍板：
> - **P/L 成本** = `net × business% ÷ depreciation_years`（**按年直线摊销**，起始日用 `expense_date`）。与作者 Excel 的「Actual Expense」列一致（M8.5 D6）。`depreciation_years = 1` 即当年全摊。
> - **进项 VAT（5b）= 购买当季一次性全额申报退回**，**不随折旧年数分摊**（business% 私用缩减走 1d，见 D-PCT/D-1D）。Belastingdienst 查实：投资品进项 VAT 在购入期全额抵扣；VAT 分摊只是 *herzieningsregeling*（投资品调整规则）——动产盯 5 年、不动产 10 年，逐年回看应税/业务使用比例相比首年是否**变动**才在 5b/rubriek 1 做纠偏。**v1 herzieningsregeling 判 OUT**（见 OUT 列），首年全额退即可。

### D-PCT（✅ 已冻结 · 作者定「同时砍可退 VAT 和 P/L 成本」；2026-06-15 review 后细化「VAT 侧改走 1d，不在 5b 双算」）· `business_percentage` 砍什么
> §7.4 + M8.5 D3：业务资产有自用部分时，**可抵进项 VAT 与 P/L 成本都按 `business_percentage` 缩减**。**净效果不变；唯一调整 = VAT 侧的缩减改在 1d 体现，不再预扣 5b**（否则 5b 已缩减又在 1d 回补 = 双算）。
> - **P/L 成本** = `net × business% ÷ depreciation_years`（不变）。
> - **5b 进项 VAT** = `vat_amount × deductible`（**全额**，每期；`deductible=false` 则完全不进 5b）。**不在 5b 预先按 business% 缩减**。
> - **私用部分的 VAT 缩减改由 1d 年末回补**（见 D-1D）——这正是荷兰申报表的官方做法（5b 全额抵、年末在 1d 补私用），且满足作者「1d 按当年 expense 表算」的诉求。年净额 = 全额 5b − 1d 回补 = 业务部分，与「预扣 5b」口径同钱、但格子对得上官方表。
> - **销项侧（发票）不涉及 business%**——销售全额计销项，business% 只作用于开支侧。

### D-OSS（✅ 已冻结 2026-06-14 · 作者定「不做、判 OUT」）· EU-B2C 远程销售 OSS/3c 阈值
> 作者确认无跨境远程 B2C 销售业务量。**v1 不做自动 OSS 切换**——`EU_B2C` 一律按 NL 税率进 1a/1b（D-MAP 行 4），报表上加一行**静态注记**「如有跨境远程销售超 €10,000 OSS 阈值需另行 OSS 申报」。3c 格子 v1 一律置 0。

### D-1D（✅ 已冻结 2026-06-15 · 作者定「1d 按当年 expense 表算」）· 私用补税 1d
> 1d（privégebruik，私用补税）= **当年（年度最后一期 / Q4）一次性回补**被全额抵扣过的进项 VAT 里属于私用的部分，**直接从当年 `expense` 表的 `business_percentage` 推**：
> - **1d = Σ（当年 deductible 开支）[ `vat_amount × (1 − business% / 100)` ]**（只算 `deductible=true` 的；外币用 `base_vat_amount`）。
> - `business% = 100` 的开支贡献 0 ⇒ 全部 100% 业务用时 1d = 0。
> - **只在年度最后一期申报里出现**（官方：「alleen in de laatste aangifte van het jaar」，指南 §2）；Q1–Q3 的 5b 全额、不出 1d。
> - **基数已够**（M8.5 已落 `business_percentage`），无需新字段。
> - ⚠️ **不自动算的部分**：公司车私用的 **forfait（如 catalogusprijs 的 2.7%）**等「按定额而非 business% 的私用项」官方 PDF 未给公式（指南 §3.5/§6 已标）——v1 不自动算，作者有公司车私用时在 1d 手工调整。

### D-COUNTRY（✅ 已冻结 2026-06-15 · 作者定）· 申报政策按公司注册国选 ruleset，税率档位留落盘口子
> - **报税政策（格子映射 ruleset）固化在代码里、按国别拆分**，运行时由 `company.country_code` 选：`NL` → NL ruleset；将来 `DE` → DE ruleset……**当前只实现 NL**，其它国家**一律 fallback 到 NL ruleset**。符合红线 12「类别→格子映射国别特定、与税率表解耦」。
> - **报表页常驻一条 banner**：提醒「本系统目前仅实现荷兰（NL）BTW 申报政策，其它国家暂未支持」（见 D-DISCLAIMER 一并呈现）。
> - **税率档位 hoog/laag 默认在代码固化（hoog=21 / laag=9 / zero=0），但留一个落盘设置口子可改**（万一荷兰改税率，改设置即可、不重新部署）。NL ruleset 按「文档行的 `vat_rate_percent` ↔ 配置的 hoog/laag/zero 阈值」把行归入 1a/1b/1e；命中非 hoog/laag/zero 的非零税率 → 1c。**实际税率值仍走 M4 数据驱动的 `vat_rate` 字典**（红线 12），这个口子只存「哪个值算 hoog / 哪个算 laag」的分档阈值（COMPANY/GLOBAL 级 typed setting）。

### D-BOX5（✅ 已冻结 2026-06-15 · 作者实践 + 官方指南）· 第 5 章只产出 5b；合计用税局口径措辞，不标「5a/5c」编号
> 作者实践：申报表填好 1/2/3/4 后，第 5 章「应缴销项合计」由税局报税工具**自动算**，作者**只需填进项税额（voorbelasting = 5b）**抵掉。官方指南查实：2026 toelichting **只命名 5b**；合计由程序自动算、**税局网站只显示「Totaal te betalen / terug te vragen」，没有「5c」这个标号**，更无 5d/5e。
> - 系统**权威产出 = 各明细格子（1a/1b/1c/1d/1e/2a/3a/3b/3c/4a/4b 的净额/税额按各自规则）+ 5b（进项税额）**——这些是作者要手填进税局工具的数。
> - **两个辅助合计（非官方逐格、不标 5a/5c 编号，仅供对账 + Dashboard）**：
>   - **销项合计**（`verschuldigde omzetbelasting`，≈传统 5a，作参考位与税局网站自动算的数核对）= `1a+1b+1c 的 VAT + 1d + 2a + 4a + 4b 的自核 VAT`（含自核，问题⑤已确认）；
>   - **总应缴 / 应退**（用税局措辞 `Totaal te betalen / terug te vragen`，**不叫 5c**）= 销项合计 − 5b。
> - **5d/5e 不建格子。**

### D-DISCLAIMER（✅ 已冻结 2026-06-15 · 作者定）· 报表免责声明
> 项目将开源上架。**BTW/ICP 等税务报表页必须常驻免责声明**：本系统输出仅为**记账辅助**，**不构成税务/会计建议**，实际申报请咨询会计师 / 以税局为准；开源项目不承担申报准确性责任。与 D-COUNTRY 的「仅支持 NL」banner 一并呈现。

### D1–D6（默认提案 · 作者不反对即冻结）
- [ ] **D1 · 纯读报表、零业务写入**：`services/reporting` 只 SELECT 聚合既有落盘列，**不新建业务表、不落派生结果**（报表实时算）。新增的只有报表 API 路由 + schema。算钱仍在 services + 单测（红线 1）。
- [ ] **D2 · 申报基准 = factuurstelsel（按单据日期）**：BTW/ICP 按**季度**（`year` + `quarter` 1–4），销项按 `invoice_date`、进项按 `expense_date` 归期（§7.4.8）。P/L 与开支报表用**任意 `from`/`to` 区间** + 季度/年快捷预设。
- [ ] **D3 · 纳入口径**：发票计入 = `status ∈ {SENT, COMPLETED}`（DRAFT/CANCELLED 排除）；开支计入 = 全部**已确认**开支（`is_draft = false`，排除周期性草稿）。v1 无贷项单（负数发票），不处理。
- [ ] **D4 · 金额口径 = 本位币 EUR**：一律取 `base_*` 列（`base_taxable_amount` / `base_vat_total` / `base_net_amount` …），v1 `exchange_rate=1` 即等于面值；天然满足「落盘以 EUR 为准」（§7.4.1）。
- [ ] **D5 · ICP 取数 = 报表期实时 join 客户**：发票未快照客户 VAT 号；ICP 清单按 `invoice → customer` 实时取 `customer.vat_id` + 账单地址 `country_code`（客户 VAT 号稳定，v1 可接受）。缺 VAT 号的 EU-B2B 销售在 ICP 报表上**高亮告警**（申报必填）。
- [ ] **D6 · 舍入**：报表层先**按笔取已落盘的到分金额**（M7.5 已让发票/开支落到分）再求和，合计不再二次舍入；百分比/占比展示用 2 位。避免重算引入漂移（红线 1「舍入位置定死」）。

---

## 契约（先行）

> 全部 `GET`、owner-only、纯读。新增 `api/reports.py`（薄路由）→ `services/reporting`。schema 进 `schemas/report.py`。改契约后 `npm run codegen`。

- `GET /api/v1/reports/profit-loss?from=&to=&granularity=month|quarter` → `ProfitLossReport`
  `{ from, to, revenue_net, expense_actual, profit, series: [{ period, revenue_net, expense_actual, profit }], by_category?: [...] }`
- `GET /api/v1/reports/vat-return?year=&quarter=` → `VatReturnReport`
  `{ year, quarter, from, to, is_last_period_of_year, boxes: { "1a": {base, vat}, "1b": {base, vat}, "1c": {base, vat}, "1d": {vat}, "1e": {base}, "2a": {base, vat}, "3a": {base}, "3b": {base}, "3c": {base}, "4a": {base, vat}, "4b": {base, vat}, "5b": {vat} }, totals: { output_vat_total: {vat}, net_payable_or_refundable: {vat} }, warnings: [str], disclaimer: str }`
  > 权威格子在 `boxes`（含 `5b`）；`totals.output_vat_total`（≈5a 销项合计，参考位）/ `totals.net_payable_or_refundable`（税局措辞「Totaal te betalen/terug te vragen」，**不叫 5c**）为辅助合计（D-BOX5，非官方逐格、不标 5a/5c 编号）；`1d` 仅 `is_last_period_of_year=true` 时非 0（D-1D）；`disclaimer` + 仅-NL banner（D-DISCLAIMER/D-COUNTRY）。1c/3c v1 恒 0 但保留。
- `GET /api/v1/reports/icp?year=&quarter=` → `IcpReport`
  `{ year, quarter, lines: [{ customer_id, customer_name, country_code, vat_id, net_amount }], total_net, warnings: [str] }`（缺 vat_id/country 进 warnings）
- `GET /api/v1/reports/expenses?from=&to=` → `ExpenseReport`
  `{ from, to, by_category: [{ category_id, category_name, net, vat, gross, deductible_net, non_deductible_net }], total_* }`
- `GET /api/v1/reports/dashboard?year=` → `DashboardSummary`
  `{ kpi: { ytd_revenue, ytd_expense, ytd_profit, current_quarter_vat_payable }, monthly: [{ month, revenue, expense, profit }], top_expense_categories: [...] }`

> 金额字段一律 `Decimal`（Pydantic `condecimal`/序列化为 string，沿用既有 money schema 约定）。

---

## 数据模型 / 迁移

- **原则上零迁移**（D1：纯读报表）。
- **唯一可能的 additive 动作**：D-MAP 冻结后，是否把确定的格子值回填进 `vat_treatment.report_box`（作为 UI/文档提示）——**这是可选项**，权威映射在 NL ruleset 函数里（D-MAP-IMPL）。若作者要填，则一条 additive data migration 给现存公司更新 `report_box`，**不改列、不改结构**。
- 不新增业务表；报表全部 query-time 聚合。

### D-MAP-IMPL · 映射落地形态（架构决策）
- VAT 格子映射写成 **`services/reporting/btw.py` 的 NL ruleset 纯函数**：输入 = 单据行的 `(side, vat_treatment_effect, requires_icp, vat_rate_percent, taxable_base, vat_amount)` 快照 → 输出 = 格子归集。理由：① 1a/1b/1e 依赖 rate，单列存不下；② 5a/5b/5c、4b→5b 自核对冲是**算法**（求和 + 相减）不是平表查找；③ 红线 1 要求算钱可单测——纯函数最易测。
- 红线 12「类别→格子映射与税率表解耦、国别特定」由「ruleset 模块按国家拆分」满足；v1 只实现 NL ruleset，多国是 vNext（届时可升级为 DB 映射表）。`report_box` 列继续作声明性提示，不作权威源。

---

## 原子步骤清单
> 每步 = 一个原子改动（CI 绿即可合 main），过 roadmap §5 DoD。算钱逻辑（聚合 / 摊销 / 格子归集）**必须单测**（红线 1）。**所有税法决策已于 2026-06-15 冻结（见上「✅ 产品/税法决策」），可直接施工。**

### 步骤 1 · 报表引擎骨架 + P/L 盈亏报表
- **契约**：`GET /reports/profit-loss`。
- **后端**：`services/reporting/__init__.py` + `pl.py`（区间内销项净额 = Σ 发票 `base_subtotal_excl_vat`/`base_taxable_amount`，纳入口径 D3；开支实际入账额 = Σ `base_net_amount × business% ÷ depreciation_years`，摊销口径 D-DEP/D-PCT）；按 month/quarter 分桶时间序列；`schemas/report.py`；`api/reports.py`。
- **前端**：`stores/reports.ts` + `views/reports/ProfitLoss.vue`（区间选择器 + 汇总卡 + 时间序列表/图）；路由 + 导航入口；i18n。`npm run codegen`。
- **迁移**：无。
- **测试**：pytest——P/L 聚合（含折旧摊销分年、business% 缩减、DRAFT/CANCELLED 排除、is_draft 开支排除、空区间）；happy + corner。
- **DoD**：见 roadmap §5。

### 步骤 2 · ⭐ BTW 申报汇总（NL ruleset 格子归集）—— **门：D-MAP 等已冻结（2026-06-15 共定）**
- **契约**：`GET /reports/vat-return`。
- **后端**：`services/reporting/btw.py`（**NL ruleset 纯函数**，按 `company.country_code` 选 ruleset，非 NL fallback NL + banner，D-COUNTRY）。归集口径（D-MAP 冻结表）：发票行级 VAT 快照 → 1a/1b/1c/1e/3a/3b（hoog/laag/zero 阈值取自落盘设置，默认 21/9/0）；开支 VAT 快照 → 5b（**全额** `vat×deductible`，不预扣 business%）+ 4b（EU 自核）；**1d = 当年 deductible 开支 Σ`vat×(1−business%/100)`，仅年度最后一期**（D-1D）；辅助合计 `5a`/`5c`（D-BOX5）。`is_last_period_of_year` 由季度推。warnings（EU-B2B 缺 ICP / fallback 非 NL 国家）；`disclaimer`（D-DISCLAIMER）；schema `VatReturnReport`。**行 9（非 EU 进口/4a）/ 1e 反向征收供方 / 2a / 3c：v1 不产出（恒 0/留位）**。
- **前端**：`views/reports/VatReturn.vue`（年 + 季度 → boxes 表 + totals 5a/5c + **仅-NL banner + 免责声明** + warnings）；i18n。`npm run codegen`。
- **迁移**：无（或可选回填 `report_box` 声明性提示，D-MAP-IMPL）。另：hoog/laag/zero 分档阈值的落盘设置（D-COUNTRY）若用既有 typed settings 表则无迁移。
- **测试**：pytest——**重头戏**。逐条 D-MAP 行（1a/1b/1e 按阈值分档、3a/3b、4b 自核对冲、5b 全额进项、1d 私用回补[business%<100 & 仅年末]、5a/5c 求和与相减含自核 VAT、EU_B2C_PURCH 不入格、跨季度边界、空季度全 0、非 NL 公司 fallback+warning）。
- **DoD**：见 roadmap §5（VAT 算钱必测）。

### 步骤 3 · ICP 清单（Opgaaf ICP）
- **契约**：`GET /reports/icp`。
- **后端**：`services/reporting/icp.py`（`requires_icp` 的销售按 `customer.vat_id` + 国家分组求净额，对账 3b；缺 vat_id/country → warnings，D5）；schema `IcpReport`。
- **前端**：`views/reports/Icp.vue`（季度选择 → 客户分组表 + 缺号高亮 + 与 BTW 3b 合计核对提示）；i18n。`npm run codegen`。
- **测试**：pytest——按客户聚合、多发票合并、缺 VAT 号告警、非 EU-B2B 不计入、与 3b 净额一致性。
- **DoD**：见 roadmap §5。

### 步骤 4 · 开支报表（按分类）
- **契约**：`GET /reports/expenses`。
- **后端**：`services/reporting/expenses.py`（区间内按 `category_id`/`category_name` 快照聚合 net/vat/gross + 可抵/不可抵拆分）；schema `ExpenseReport`。
- **前端**：`views/reports/ExpenseReport.vue`（区间 → 分类表 + 占比）；i18n。`npm run codegen`。
- **测试**：pytest——分类聚合、category 删除后用 name 快照归并、可抵/不可抵拆分、空区间。
- **DoD**：见 roadmap §5。

### 步骤 5 · Dashboard（ECharts）
- **契约**：`GET /reports/dashboard`（或复用上述端点组合）。
- **后端**：`services/reporting/dashboard.py`（KPI：YTD 收入/支出/利润、本季应缴 VAT(5c)；月度时间序列；Top 开支分类）；schema `DashboardSummary`。
- **前端**：重写 `views/Dashboard.vue`（替换占位欢迎页）——ECharts（已装 `echarts`/`vue-echarts`）出 收入/支出/利润折线/柱状 + KPI 卡 + Top 分类；i18n。`npm run codegen`。
- **测试**：pytest——KPI 与时间序列聚合（与 P/L / BTW 数字一致）。
- **DoD**：见 roadmap §5。

> **可并行**：步骤 1（P/L 引擎骨架）落地后，步骤 3/4 与步骤 2 可并行（都建在 `services/reporting` 上）；步骤 5 依赖前四步的聚合，放最后收口。

---

## 🟢 部署自测点（里程碑验收）
> `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d` 起 app + Postgres，备好一批跨季度的发票（含 NL 21/9/0、EU-B2B、出口）+ 开支（含可抵/不可抵、折旧大件、私用比例）后手动走：

1. **P/L**：选一个区间，看收入−支出=利润；折旧大件只入当年摊销额、私用比例已砍；时间序列分桶正确。
2. **⭐ BTW 申报汇总**：选一个季度，看 1a/1b/1e/3a/3b/4a/4b/5a/5b/5c 各格子的净额与 VAT；**对照同季度的发票/开支手算核对**；5c = 5a − 5b；切换季度数字随之变。
3. **ICP**：同季度导出 ICP，EU-B2B 客户按 VAT 号分组，净额合计 = BTW 3b；缺 VAT 号的客户被高亮告警。
4. **开支报表**：选区间，按分类看净/税/毛 + 可抵不可抵拆分。
5. **Dashboard**：进首页看 ECharts 收入/支出/利润图 + KPI（本季应缴 VAT 与步骤 2 的 5c 一致）。
6. CI 四关绿 + `schema.d.ts` 无漂移 + 前端 build + docker build 通过。

---

## 验收结论（2026-06-15 回填）
- **完成日期**：2026-06-15。
- **实现方式**：orchestrator 模式 5 步逐步盲审收敛（implementer=Sonnet·high / reviewer=Opus·extra-high），每步一 commit：P/L=`89ab353`、BTW=`b7aa26a`、ICP=`a38ff98`、开支报表=`60b7728`、Dashboard=`273ed75`。门禁：ruff / mypy --strict / 默认 966 + 集成 788 单测 / codegen 无漂移 / 前端 build / docker build 全绿。
- **验收**：部署自测点 1–6 由作者导入 **2026 Q1–Q2 实测数据** 人工 walkthrough 通过，作者确认「整体功能没问题、M10 已完成」。
- **walkthrough 期间发现并修复**（均经 Opus 盲审无 finding、各自独立 commit）：
  1. Expense 日期选择器 off-by-one——CET/CEST 下 `toISOString()` 把本地午夜减成前一天；改用 `v-model:formatted-value` + `value-format="yyyy-MM-dd"`，与发票/报价/付款范式一致（`1a5a94a`）。
  2. 对外单据抬头泄漏客户内部「花名」——新增纯函数 `resolve_billing_name`，抬头按 `company_name → contact_name → name` 派生；invoice/quote/receipt 三模板统一用 `billing_name`；客户表单加「内部识别名」说明 + 发票抬头实时预览（`853f07c`）。
  3. P/L 顶栏「月/季粒度」单选在单一周期区间时仍强制高亮，且缺 MTD/QTD/YTD——改为 本月/本季/本年 周期预设，高亮由当前区间派生（自定义区间不高亮），明细表固定按月（`df2ba13`）。
- **已知遗留 / 顺延项**：
  - 多币种下 ICP（用 `base_taxable_amount`）与 BTW 3b（用 `taxable_amount`）会分叉；v1 `exchange_rate=1` 恒等，留 FX 落地后处理。
  - Dashboard 死常量 `_MONTH_LABELS`、未用 i18n 键 `dashboard.loadError` 已从取消的笼统 M13 移入未排期的 roadmap §4.aa 维护积压。
  - 第 5 章口径以 **D-BOX5** 为准（官方只命名 5b；5a/净应缴为辅助合计，不标 5c）——上文「部署自测点」段仍保留早期「5c」措辞为历史痕迹，实现以 D-BOX5 为准。
