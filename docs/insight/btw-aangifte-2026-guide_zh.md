# 荷兰 BTW（增值税）季度申报表填写口径指南 — 2026

> 🌐 [English](btw-aangifte-2026-guide.md) · **中文**

> **来源**：荷兰税务局 Belastingdienst 官方文件《Toelichting bij de aangifte omzetbelasting (btw) 2026》/《Explanatory notes to the 2026 VAT return (turnover tax)》（订单号 OB061-1T62FD，版本 Mei 2026）。
> 本文件已**通读全部 41 页**：第 3–16 页是「**Voor ondernemers in Nederland**（荷兰境内 established 企业，权威，我们 eenmanszaak/ZZP 适用）」荷兰语版；第 17–28 页是「Voor *niet* in Nederland gevestigde ondernemers」荷兰语版；第 29–40 页是同一份「For entrepreneurs *not* established in the Netherlands」英语版；第 41 页为封底。
> **施工口径以第 3–16 页 NL-established 版为准**；英语段（29–40 页）仅用于核对术语英译。
>
> **重要边界声明**：本 toelichting 是「填写说明」，**不是**税法本身。它把每个格子「填什么」写得很清楚，但**对一部分机制（herzieningsregeling 的 5 年/10 年/10% 阈值、gemengd gebruik 的具体分摊算法、KOR 的回补规则细节）只给原则并指向 belastingdienst.nl 的其它页面，本文件不展开**。凡 PDF 未写明者，本指南一律标注「**PDF 未覆盖**」，不臆测。

---

> **M16 补充（2026-10-30）**：[§7](#7-m16-补充家用储能与外部销售原件) 单独记录储能及 self-billing 的官方网页口径，补充原 PDF 解读，不表示 PDF 已确认储能五年详细算法。实施边界见 [M16](../plan/milestones/M16_zh.md)。

## 0. 一句话结论（给赶时间的人）

- 我们 10 行映射提案里：**7 行 ✅ 与官方一致**、**2 行 ⚠️ 有出入或需补条件**、**1 行 ❓ PDF 明确说不进荷兰申报（即“留空/不报”是对的）**。
- 最关键的 3 处：
  1. **⚠️ 提案第 4 行（EU_B2C 一律按 NL 税率进 1a/1b）只在“不使用 OSS”且“未超 €10.000 远程销售阈值”时成立**；一旦超阈值，欧盟规则要求税基落在客户所在国（OSS / 该国申报），不进 1a/1b。PDF 第 11、14（3c）页明确。
  2. **⚠️ 提案第 9 行（IMPORT_NON_EU 走 4a 自核）只在持有 vergunning artikel 23（递延进口 VAT 许可）时成立**；没有 art.23 许可时，进口 VAT 由海关（Douane）当场征收、**不进 4a**，只能作为 voorbelasting 在 5b 抵扣。PDF 第 14 页明确。
  3. **❌ 我们提案默认的 5a/5c/5d/5e 编号在 2026 toelichting 里并不存在**：本说明书 section 5 只描述 **5b（voorbelasting，进项税额）**，最终应缴/应退由申报程序「automatisch」计算（对应申报表上的 5a 应缴合计 / 5c 净额，但 toelichting **不逐格命名 5a/5c**，更**完全没有 5d/5e**）。详见 §2「关于 5a/5c/5d/5e 的真相」。

---

## 1. 申报表整体结构

### 1.1 五大 rubriek（section）

PDF 把申报表问题分成 5 个 rubriek（第 10–15 页 / 第 35–40 页英文）：

| Rubriek | 荷兰语标题 | 英译 | 含义 |
|---|---|---|---|
| **1** | Prestaties binnenland | Domestic supplies | 你在荷兰境内做的（销售侧）供货/服务，按税率分档 + 私用 |
| **2** | Verleggingsregelingen binnenland | Domestic reverse-charge schemes | 你**作为接收方**、境内被反向征收（btw 被 verlegd 到你）的供货/服务 |
| **3** | Prestaties naar of in het buitenland | Supplies of goods/services to or in other countries | 出口（非 EU）、EU 内供货（ICP）、安装/远程销售 |
| **4** | Prestaties vanuit het buitenland aan u geleverd | Supplies provided to you from abroad | 你从境外**接收**、需在荷兰自核 btw 的进口 / EU 采购 |
| **5** | Voorbelasting | Input VAT | 进项税抵扣 + 结算（应缴/应退） |

### 1.2 申报周期（aangiftetijdvak）

- PDF 反复提到 **maand / kwartaal / jaar** 三种 tijdvak（如「Het tijdvak waarover u aangifte moet doen」会随年度通知信变化）。**具体哪种周期由税局在 aangiftebrief 里指定**，ZZP 默认通常是**季度（kwartaal）**。
- **即使本期无营业额或要退税，也必须申报**（"Doe altijd btw-aangifte als deze voor u klaarstaat"，第 4 页）。
- **空申报（nihilaangifte）**：只有当本期**完全没有**第 4/18 页列的那 7 类事件时，才回答「Nee」做空申报：
  - btw in rekening gebracht（开过销项 btw）
  - btw over zakelijke uitgaven in aftrek kunt brengen als voorbelasting（有进项可抵）
  - te maken gehad met verleggingsregeling（涉及反向征收）
  - goederen geleverd tegen het 0%-tarief（0% 供货）
  - goederen verworven uit andere EU-landen（EU 采购货物）
  - diensten afgenomen uit andere landen waarbij de btw verlegd is naar u（境外服务反向征收给你）
  - goederen ingevoerd en gebruik gemaakt van een vergunning artikel 23（用 art.23 进口）
  - btw over privégebruik moet aangeven（私用补税）
  - installatie-/afstandsverkopen binnen de EU verricht（EU 内安装/远程销售）

### 1.3 申报基准：开票 vs 收款

PDF 在多处给出**按 factuurdatum（开票日）**的口径，未给「按收款」选项：

- **ICP 货物供货（3b）**：「gaat u uit van de factuurdatum, ook als de feitelijke levering in een volgend tijdvak plaatsvindt」（按开票日，即使实物交付在下一期）—— 第 13、25、38 页。
- **ICP 服务（3b）/ 从 EU 接收的服务（4b）**：「gaat u uit van het tijdvak waarin deze diensten worden geleverd. De factuurdatum is hierbij niet van belang」（按服务**发生**期，与开票日无关）—— 第 13、14、38 页。
- **结论**：本说明书是「开票/发生制」口径，没有现金制（kasstelsel）说明（kasstelsel 属另一制度，PDF 未覆盖）。

> 补充：**坏账退税（oninbare vorderingen，第 7、21、33 页）**：客户欠款被认定不可收回（最迟到约定付款到期日后 1 年、或法定 30 天付款期后）时，把当初已申报的销项 btw 在 1a/1b 处**减回**，同时把对应营业额从 1a/1b 营业额里减掉。

### 1.4 四舍五入规则（Bedragen afronden）

第 10 页 / 第 23 页 / 第 35 页：

> **「Rond alle bedragen af op hele euro's. Dit mag u in uw voordeel doen. Zet bij negatieve bedragen een minteken (-) voor het bedrag.」**
> 英译：Round off all amounts to **whole euros**. You may round **off in your favour**. For negative amounts, place a minus sign (-) before the amount.

施工要点：**所有格子金额取整到整欧元（去掉小数）**，且**允许向对纳税人有利方向取整**；负数加前置「-」号。注意这是「申报表层面」的取整，不影响发票/行级的分级计算（行级仍按我们既定的 quantize-to-minor-unit 规则）。

---

## 2. 逐格（rubriek）精确定义 —— 重点

> 约定：**「净额 / Omzet」= 不含税的税基（PDF 称 kolom 'Omzet' 或 linkerkolom）**；**「税额 / Btw」= 增值税额（PDF 称 kolom 'Btw' 或 rechterkolom）**。

### 关于 5a / 5c / 5d / 5e 的真相（先讲，避免误导）

**2026 toelichting 全文里只出现并描述了 `5b Voorbelasting`**。Section 5 标题是「5 Voorbelasting」，其下只有一个被命名的格子 **5b**。

- **没有任何一处文字命名 `5a`、`5c`、`5d`、`5e`。**
- 申报表的「应缴销项合计（传统 5a）」「净应缴/应退（传统 5c）」由申报程序**自动算**：第 16 / 28 / 40 页「**Totaal te betalen of terug te vragen — Het aangifteprogramma berekent automatisch het bedrag dat u moet betalen of terugvraagt**」。
- 因此：**我们系统聚合时，只需要直接产出各个明细格子（1a/1b/1c/1d/1e/2a/3a/3b/3c/4a/4b 的净额与税额，以及 5b 的进项税额）**；「5a=销项合计」「5c=5a−5b」属于**展示性合计**，可由我们自己求和（见 §4 问题⑤）。**5d/5e 在本说明书中不存在**，按 PDF 口径**无须为它们建格子**（5d/5e 在历史/其它语境里曾用于 KOR 小规模减免结算等，但 2026 toelichting 未提，标「PDF 未覆盖」）。

> ⚠️ **需作者/会计确认**：实际申报表 UI（Mijn Belastingdienst Zakelijk）里是否仍以 5a/5c 标签呈现合计。本 toelichting 口径是「程序自动算合计」，不逐格命名。

---

### Rubriek 1 — Prestaties binnenland（境内供货，销售侧）

#### 1a — Leveringen/diensten belast met **hoog tarief**（21%）
- **填**：`Omzet` 列填**净额（税基）**，`Btw` 列填**销项税额**。（第 11、24、36 页：「Vul in de kolom 'Omzet' het bedrag waarover btw wordt berekend in. Vul in de kolom 'Btw' het btw-bedrag in.」）
- **归入**：在荷兰境内、按**高税率（hoog tarief = 21%）**征税的货物供货与服务。
- 净额 + 税额都填。

#### 1b — Leveringen/diensten belast met **laag tarief**（9%）
- **填**：净额 + 销项税额（同 1a 的 kolom 规则）。
- **归入**：境内按**低税率（laag tarief = 9%）**征税的供货/服务。

> ⚠️ 注意：PDF **没有把「21%」「9%」这两个数字写进 1a/1b 的定义**，只说「hoog/laag tarief」。具体税率数值要查 belastingdienst.nl 的 btw-tarieven 页。**2026 现行 hoog=21%、laag=9% 是常识，但本说明书本身不背书具体数字**（标「税率数值 PDF 未写明，按现行 21/9」）。
>
> 同档下的 **margeregeling（差额征税）**：二手货按利润差额征税时，`Btw` 列填的是「差额上的 btw」，营业额负差额不得抵正常营业额（第 11、24 页）—— 我们 v1 不做 marge，标「不实现」。

#### 1c — Leveringen/diensten belast met **overige tarieven, behalve 0%**（其它税率，0% 除外）
- **填**：净额 + 税额（隐含同 kolom 规则）。
- **归入**：唯一被点名的具体场景是「sportkantine 选择 **13% forfaitair** 对食堂总收入（含税）征税」（第 11 页：「Vul deze vraag in als u een sportkantine hebt en u ervoor kiest om een forfaitair btw-tarief van 13% te betalen over uw totale kantineontvangsten inclusief btw.」）。
- **对 ZZP 自由职业者基本用不到**（见 §4 问题⑥）。

#### 1d — **Privégebruik**（私用补税）
- **填**：**只填税额（btw die u moet betalen over het privégebruik）**，且**只在一年的最后一期申报里填**（第 11、24、35–36 页：「Vul deze vraag alleen in de laatste aangifte van het jaar in.」）。
- **作用**：纠正过去一年里对「公私混用」货物/服务多抵的进项；典型场景：
  - 公司资产车辆的私用（含 woon-werkverkeer 通勤，按 PDF 也算私用）；
  - 私用的 gas、water、elektra、telefoon；
  - fictieve levering/dienst（把公司货物撤出转私人资产 → btw 按该货物价值；停业时把货物留作私用 → 立刻在转入私产那一期申报，**不等年末**，见第 11 页 Let op!）。
- **算法细节（business% 怎么定、按哪种 forfait）PDF 不展开**，指向 belastingdienst.nl/btw-gebruik 与 hulpmiddel「Btw of btw-aftrek over uw auto berekenen」。标「PDF 未覆盖算法」。

#### 1e — Leveringen/diensten belast met **0% of niet bij u belast**（0% 或不在你处征税）
- **填**：**只填净额（omzet）**，**无税额**（第 12、24、36 页）。
- **归入**（第 12 页明确两类）：
  1. 「leveringen van goederen en diensten in Nederland die vallen onder het **0%-tarief**（zie tabel II bij de Wet op de omzetbelasting 1968），**behalve export (vraag 3a) of intracommunautaire leveringen (vraag 3b)**」——**境内 0% 供货，但出口和 ICP 不归这里（分别归 3a/3b）**。
  2. 「leveringen van goederen en diensten **waarbij de btw verlegd is naar een andere ondernemer**」——**你作为供方、把 btw 反向征收（verlegd）给另一个企业的境内供货**（即境内 verleggingsregeling 的**供方侧**，net 入 1e）。
- 第 12 页「Wanneer btw verleggen」列出境内反向征收适用场景（供方记 1e、接收方记 2a）：
  - zakendoen met het buitenland
  - onderaanneming en personeel uitlenen in sectoren **bouw, scheepsbouw, schoonmaakbedrijven, hoveniers**（建筑/造船/清洁/园艺的分包与派工）
  - levering van telecommunicatiediensten aan een andere ondernemer
  - handel in **mobiele telefoons, (computer)chips, spelcomputers, laptops en tablets**
  - onroerende zaken（选择 belaste levering 的不动产）
  - **afval en oude materialen**（含相关加工服务）
  - verplichte verlegging bij gas- en elektriciteitscertificaten
  - **executieverkopen**（强制拍卖）、**verkoop van goud**、**overdracht van emissierechten**

---

### Rubriek 2 — Verleggingsregelingen binnenland（境内反向征收，接收方侧）

#### 2a — Leveringen/diensten waarbij de btw naar u is verlegd（btw 被反向征收**到你**）
- **填**：**净额 + 税额都填**。你作为接收方，自己**算出**被反向征收的 btw（uitrekenen），把它当成「verschuldigde btw（应缴销项）」申报在 2a（第 12、25、37 页）。
- **归入**：在荷兰境内、有企业把货物/服务供给你、且把 btw verlegd 到你（发票上写「**btw verlegd**」+ 你的 btw-id）。适用场景同上 1e 的「Wanneer btw verleggen」列表（接收方视角）。
- **净影响**：「Het btw-bedrag dat u hebt aangegeven, kunt u onder voorwaarden weer als voorbelasting aftrekken bij vraag 5b … U betaalt per saldo dan geen btw. Toch moet u vraag 2a en 5b invullen.」——**2a 申报的应缴 btw，在满足抵扣条件下可在 5b 同额抵回，净额为零；但 2a 和 5b 都必须填**（第 12、25、37 页）。

---

### Rubriek 3 — Prestaties naar of in het buitenland（出口与跨境供货，销售侧）

#### 3a — Leveringen naar landen buiten de EU（**uitvoer / 出口非 EU**）
- **填**：**只填净额（omzet）**，无税额（第 13、25、37 页）。
- **归入**：从荷兰出口到**EU 以外**国家的货物营业额；**也包括**进入 douane-entrepot（海关保税仓）制度的货物。

#### 3b — Leveringen naar of diensten in landen binnen de EU（**ICP / EU 内供货**）
- **填**：**只填净额（het bedrag van de goederen en diensten die u … hebt geleverd naar of in andere landen binnen de EU）**，无税额（适用 0% 税率，第 13、26、38 页）。
- **关键约束（与 ICP 清单的关系）**：「**Het bedrag dat u bij deze vraag invult, moet u specificeren in de opgaaf ICP.**」——**3b 填的金额必须在 Opgaaf ICP（ICP 清单）里逐笔明细化**。即 **3b 总额 ≡ Opgaaf ICP 合计**（第 13、26、38 页）。
- **时点**：
  - ICP **货物**供货：按 **factuurdatum**（即使实物交付在下一期）。
  - ICP **服务**：按服务**发生**期，factuurdatum 不相干。
- **适用 0% 的条件**（第 13、26、38 页）：
  1. 能用 administratie 证明货物运到了另一 EU 国；
  2. 能证明供给了**持有效 btw-id**（非荷兰）的企业；
  3. **按时、正确、完整地报了荷兰 Opgaaf ICP**。
- **不进 3b/ICP 的服务**（第 14 页「Welke diensten mag u niet opnemen…」列表）：客户在其本国 vrijgesteld / 0% 的服务、走 OSS 的服务、与**不动产**相关的服务（如出租维护）、**personenvervoer（客运）**、文化/艺术/体育/科学/娱乐/教育的入场及现场服务、餐饮服务、短期交通工具租赁（≤30 天）/船舶租赁（≤90 天）—— 这些 B2B 服务**不按一般 reverse-charge 规则进 3b**（地点规则特殊）。

#### 3c — Installatie/afstandsverkopen binnen de EU（**EU 内安装 + 远程销售**）
- **填**：**只填净额（omzet）**（第 14、26 页）。
- **归入**（第 14 页）：
  - 在另一 EU 国**安装/组装**货物（installatie/montage）——btw 在安装/组装发生的那个 EU 国缴；
  - **afstandsverkopen（远程销售）当你不使用 OSS（eenloketsysteem）时**，且满足全部条件：
    - 客户是：particulier / 只做 vrijgesteld 的企业 / 非企业的法人；
    - 你（直接或间接）安排货物从荷兰运到客户；
    - 你**去年和/或今年**面向 particulieren 的远程销售 + 数字服务**超过了 €10.000 阈值**。
- **缴税地**：远程销售 btw 在**货物运输终点的 EU 国**缴（第 14 页「moet u btw betalen in het EU-land waar het vervoer van uw goederen eindigt」）。
- **与 OSS 关系**：**用 OSS 的远程销售营业额不在荷兰申报里填**（OSS 在 EU 国侧申报；荷兰申报「Hierin vult u alleen Nederlandse btw in」，第 11 页）。

---

### Rubriek 4 — Prestaties vanuit het buitenland aan u geleverd（你从境外接收，自核侧）

#### 4a — Leveringen/diensten uit landen **buiten de EU**（非 EU 进口，自核）
- **填**：**linkerkolom 填净额（waarde van de goederen/diensten）+ rechterkolom 填 btw**；**不需按不同税率拆分**（第 14、27 页：「U hoeft deze bedragen niet te splitsen naar de verschillende btw-tarieven.」）。
- **归入两种情形**（第 14 页 NL-established 版）：
  1. **U hebt goederen ingevoerd van buiten de EU én daarbij gebruikgemaakt van de verleggingsregeling bij invoer（vergunning artikel 23）**——你从非 EU 进口货物**并使用了进口反向征收（art.23 许可）**：「Bij een vergunning artikel 23 hoeft u bij de Douane geen btw te betalen bij de zogenoemde aangifte ten invoer. In plaats daarvan geeft u de btw aan in uw btw-aangifte en betaalt u per saldo niets.」——**有 art.23 时，进口 VAT 不在海关当场缴，而是挪到申报表里自核（4a），并在 5b 同额抵扣，净额为零。**
  2. **U hebt diensten afgenomen van een ondernemer van buiten de EU, die de btw u heeft verlegd**——从非 EU 企业接收服务、btw 被反向征收给你，在荷兰申报。
- **vergunning artikel 23 是什么**（第 14 页）：**递延进口 VAT 许可**。没有它，进口 VAT 由 Douane 在 aangifte ten invoer 时征收；**对某些「特定原材料（ruwe grondstoffen genoemd in de wet op de omzetbelasting）」，进口反向征收是强制的，必须有 art.23 许可**。
- **净影响**：「Per saldo betaalt u dan geen btw. Toch moet u vraag 4a en 5b volledig invullen.」（4a 与 5b 都必须完整填，净额零）。

> ⚠️ **关键边界**：第 14 页明确——**4a 只在「使用了 art.23 verlegging」或「从非 EU 接收被反向征收的服务」时填**。**普通进口（无 art.23）的进口 VAT 由海关代缴、不进 4a**，只可作 voorbelasting 在 5b 抵（须凭海关单据）。详见 §4 问题③。

#### 4b — Leveringen/diensten uit landen **binnen de EU**（EU 内采购，自核）
- **填**：**linkerkolom 净额 + rechterkolom btw**；**不需按税率拆分**（第 14、27 页）。
- **归入两种情形**（第 14 页）：
  1. **U hebt goederen gekocht van ondernemers uit andere EU-landen die naar Nederland vervoerd zijn**——**intracommunautaire verwerving（ICA / EU 内取得）**：供方未收外国 btw，你在荷兰自核（这就是 ICP 的镜像采购侧）。
  2. **U hebt diensten afgenomen van een ondernemer uit een ander EU-land, die de btw naar u heeft verlegd**——从 EU 企业接收被反向征收的服务，在荷兰申报。**例外**：与不动产相关的服务**不进 4b，进 2a**（第 14 页：「Dit geldt niet voor diensten aan onroerende zaken. Die vult u in bij vraag 2a.」）。
- **时点**：ICA 货物按 factuurdatum；EU 接收的服务按服务**发生**期（factuurdatum 不相干，第 14 页 Let op!）。
- **净影响**：同 4a——「Per saldo betaalt u dan geen btw. Toch moet u vraag 4b en 5b volledig invullen.」

---

### Rubriek 5 — Voorbelasting（进项税与结算）

#### 5b — Voorbelasting（进项税抵扣）
- **填**：**只填税额（een btw-bedrag，进项 VAT）**，无净额列（第 15、27、39 页）。
- **构成**（第 15 页）：
  1. **btw die andere ondernemers aan u in rekening hebben gebracht**——供应商向你开的荷兰 btw（采购/成本/投资），即使你**还没付款**也可抵（「Ook als u uw leveranciers nog niet hebt betaald」）。
  2. **btw die u moet aangeven omdat de btw naar u is verlegd**——你因反向征收（2a、4a、4b）自核出来的 btw，可在此**同额抵回**。
- **抵扣条件**（第 15、27、39 页）：
  - 凭**符合法定要求的发票**；
  - **U gebruikt de goederen en diensten zakelijk（业务用途）**——纯私用的 btw 不得抵；
  - **U gebruikt ze voor activiteiten die belast zijn met btw**——用于**应税**活动才可抵；用于 vrijgestelde（免税）活动的进项**不得抵**。
  - **Let op!**：被反向征收（verlegd）给接收方的供货、以及适用 0% 的供货，**算作「belaste bedrijfsactiviteiten」**（即不因 0%/verlegd 而失去抵扣权）。
- **不得抵的进项（Welke btw mag u niet als voorbelasting aftrekken，第 16、40 页）**：
  - privé-aankopen（私人采购）；
  - uitgaven voor **vrijgestelde** omzet；
  - uitgaven voor **niet-belastbare** omzet；
  - **eten en drinken in de horeca**（餐饮店堂食的吃喝）；
  - personeelsvoorzieningen 超过 **€227/人/年**阈值的部分（超阈值时连员工自付部分也不得抵）；
  - btw die ten **onrechte**（错误地）被供应商开出的；
  - **Let op!**：在**其它 EU 国**付的 btw**不得**进荷兰申报（要走 belastingdienst.nl 的 EU 退税程序）。

> **5b ≠ 4a/4b 的净额**：4a/4b 自核的 btw 进 4a/4b 的 rechterkolom（销项侧）**也**进 5b（进项侧）抵回；2a 同理。所以 5b 是「真实采购进项 + 各反向征收自核 btw 的可抵部分」之和。

#### 5a / 5c / 5d / 5e
- **PDF 未命名**（见本节开头「关于 5a/5c/5d/5e 的真相」）。Total payable/refundable 由程序自动算（第 16/28/40 页）。**5d/5e：PDF 未覆盖。**

---

## 3. 关键机制（按官方说法整理）

### 3.1 反向征收（verleggingsregeling / btw verlegd）

PDF 区分三个场景，**净效果都是「自核 + 同额抵扣 = 净零」，但格子不同**：

| 场景 | 角色 | 销项侧填哪 | 进项侧 | 净效果 |
|---|---|---|---|---|
| 境内 verlegging，**供方** | 你供货、把 btw 推给对方企业 | **1e**（净额） | — | 你不收 btw |
| 境内 verlegging，**接收方** | 对方把 btw 推给你 | **2a**（净额+btw 自核） | **5b** 同额抵 | 净零 |
| 跨境采购自核 — 非 EU（art.23 进口 / 非 EU 服务） | 接收方 | **4a**（净额+btw 自核） | **5b** 同额抵 | 净零 |
| 跨境采购自核 — EU（ICA / EU 服务） | 接收方 | **4b**（净额+btw 自核） | **5b** 同额抵 | 净零 |

要点：**反向征收里「净额（基数）」与「自核 btw」都要进对应格子**；可抵部分到 5b 抵回。被 verlegd 的供货对供方算「belaste bedrijfsactiviteit」（第 15、39 页），不损害供方的进项抵扣权。

### 3.2 EU 内供货（intracommunautaire prestaties）与 Opgaaf ICP

- **3b（销售侧 net）必须与 Opgaaf ICP 逐笔对账**：第 13/26/38 页明确「moet u specificeren in de opgaaf ICP」。
- **ICP 是单独清单**，在 Mijn Belastingdienst Zakelijk 提交，**不会收到单独邀请（geen uitnodiging）**——要自己判断该不该报（第 8、21、33 页）。
- **采购镜像**：你作为 EU 买方，ICA 落在 **4b**（自核），不在 ICP 里报（ICP 是供方义务）。
- **新/准新交通工具**供给无 btw-id 的私人/法人：算 ICA 供货但**无法进 ICP**（对方无 btw-id），要寄发票副本 + 说明信给 Belastingdienst/Central Liaison Office, Postbus 378, 7600 AJ Almelo（第 13、26 页）。

### 3.3 进口 / 非 EU（invoer）与 Artikel 23

- **有 vergunning artikel 23（递延进口 VAT 许可）**：进口 VAT 不在海关当场付，**挪进申报表 4a 自核 + 5b 抵回**，净零（第 14 页）。
- **对法律点名的「特定原材料（bepaalde ruwe grondstoffen）」，verlegging bij invoer 是强制的，必须申请 art.23 许可**（第 14 页）。
- **无 art.23**：进口 VAT 由 Douane 在 aangifte ten invoer 时征——**不进 4a**；该已付进口 VAT 凭海关单据作 voorbelasting 在 5b 抵（PDF 在第 14 页只反面提到「In plaats daarvan…」，未正面展开无许可情形，按税法常识补充，标「PDF 未正面展开无 art.23 情形」）。

### 3.4 voorbelasting（5b）与投资品 / herzieningsregeling（投资品调整规则）

- **进项原则上在「btw 被开给你那一期」一次性抵扣**（第 15 页：「U doet dat in het aangiftetijdvak waarin die btw aan u in rekening is gebracht」），**包括投资品（investeringen）**——**购入期全额抵，不是按折旧分摊抵**。
- **herziening（事后调整）**：第 7、21、33 页：「Bij de aanschaf van goederen of diensten hebt u bepaald welk gedeelte u deze gebruikt voor belaste omzet. Later bekijkt u of dit nog steeds klopt.」——购入时按「用于应税营业额的比例」抵；**事后若比例变化**：
  - 抵多了 → **herzien**（调减），把多抵的 btw 当作额外应缴 btw 加到 **rubriek 1**；
  - 抵少了 → 把额外可抵 btw 作为 voorbelasting 加到 **5b**。
- **5 年（动产）/ 10 年（不动产）/ 10% 阈值**：**本 toelichting 没有给出这些数字**，只指向 belastingdienst.nl/btw-gebruik「btw-aftrek bij (niet-)investeringsgoederen en de herziening ervan」。**标「PDF 未覆盖 herziening 的 5/10 年与 10% 阈值——属税法细节，须查税法/会计」。**

### 3.5 gemengd gebruik / privégebruik（混合使用 / 私用）

- **混合（belaste + vrijgestelde）omzet 三类进项**（第 16、28、40 页）：
  1. 只用于**应税**营业额 → btw **全额可抵**；
  2. 只用于**免税**营业额 → btw **全不可抵**；
  3. 两者**混用** → 按「**belaste : vrijgestelde omzet 比例**」拆成可抵/不可抵；**若能证明实际使用比例不同，可按实际使用拆分**。
- **公私混用（zakelijk + privé）的货物/服务三种处理**（第 16、28 页）：
  1. **U trekt helemaal geen btw af**（完全不抵）；
  2. **U trekt geen btw af voor het deel dat u privé gebruikt of gaat gebruiken**（按私用部分先行不抵）；
  3. **U trekt de btw volledig af, maar betaalt aan het einde van het jaar btw voor het privégebruik**（先全额抵，年末在 **1d** 补私用 btw）。
- **business% 怎么定、汽车用什么 forfait**：PDF 不给公式，指向 belastingdienst.nl 与汽车 hulpmiddel（标「PDF 未覆盖具体比例算法」）。

### 3.6 OSS / afstandsverkopen（远程销售，€10.000 阈值）—— 仅了解，v1 不做

- 阈值：**面向 particulieren 的（跨境）远程销售 + 数字服务，去年和/或今年合计 > €10.000**（第 14、26、38 页）。
- **未超阈值**：可按荷兰规则、荷兰税率，落在 1a/1b（境内口径）。
- **超阈值**：btw 落在**客户/运输终点所在 EU 国**；
  - **用 OSS（eenloketsysteem）**：那部分营业额**不进荷兰申报**（第 11、36 页：「Hierin vult u alleen Nederlandse btw in」「Do not enter One Stop Shop (OSS) turnover」）；
  - **不用 OSS**：远程销售净额进 **3c**，btw 在终点国直接申报缴纳。
- **我们 v1 不实现 OSS** → 见 §4 问题②/⑥。

### 3.7 KOR（kleineondernemersregeling）—— 仅了解，v1 不启用

- **NL-KOR**：荷兰境内年营业额 ≤ **€20.000** 可选 KOR，得到 btw-vrijstelling，**不再向客户收 btw**，且 **「u kunt dan geen btw meer aftrekken of terugvragen, en mogelijk moet u een deel van de eerder ontvangen btw terugbetalen」**（选 KOR 后不能再抵进项，可能要回补此前抵过的 btw）——第 8 页。
- **EU-KOR**：在其它 EU 国做生意、全 EU 年营业额低于 EU 阈值及该国阈值，可申请 EU 范围的小规模免税；需**每季度报 opgaaf kwartaalomzet**（第 8、22 页）。
- **我们 v1 不启用 KOR**（标「不实现」）。

---

## 4. ⭐ 对照校验：我们的 (treatment × rate) → 格子 提案

> 图例：✅ 与官方一致 ｜ ⚠️ 有出入或需补条件（给官方正确口径 + 页码）｜ ❓ PDF 未明确覆盖 / 官方说不进申报。

| # | side | treatment | rate | 提案 → 格子 | 提案报什么 | 判定 | 官方依据与正确口径 |
|---|---|---|---|---|---|---|---|
| 1 | 销售 | NL_DOMESTIC | 21% | **1a** | 净额 + 销项VAT | ✅ | 第 11 页：hoog tarief → 1a，Omzet 列净额、Btw 列税额。（PDF 不背书「21%」数字，按现行 hoog=21%。） |
| 2 | 销售 | NL_DOMESTIC | 9% | **1b** | 净额 + 销项VAT | ✅ | 第 11 页：laag tarief → 1b，净额+税额。（laag=9% 同上注。） |
| 3 | 销售 | NL_DOMESTIC | 0% | **1e** | 净额 | ✅ | 第 12 页：境内 0%-tarief（tabel II），**但排除 export(3a) 与 ICP(3b)** → 1e，仅净额。注意区分「真正境内 0%」与「出口/ICP」。 |
| 4 | 销售 | EU_B2C | 21/9% | **1a/1b** | 净额+销项VAT（按 NL 税率，不做 OSS） | ⚠️ | **仅在「未超 €10.000 远程销售阈值」时成立**：未超阈值时按荷兰税率落 1a/1b（第 14 页阈值定义 + 第 11 页 1a/1b）。**一旦累计 > €10.000**：欧盟规则要求落在客户国（用 OSS→不进荷兰申报；不用 OSS→净额进 **3c**，btw 终点国缴）。**我们既然 v1 不做 OSS，必须对 EU_B2C 做阈值监控**：未超→1a/1b；将超/已超→走 3c 或外国注册（业务红线）。详见问题②。 |
| 5 | 销售 | EXPORT_NON_EU | 0% | **3a** | 净额 | ✅ | 第 13 页：uitvoer 到 EU 外 → 3a，仅净额（含 douane-entrepot 货物）。 |
| 6 | 销售 | EU_B2B_REVERSE | 0% | **3b（+ICP）** | 净额（+ ICP 清单） | ✅ | 第 13 页：ICP 供货 → 3b 仅净额（0%），**且 3b 金额必须在 Opgaaf ICP 明细化**（3b 总额 ≡ ICP 合计）。注意 3b 同时含「ICP 货物」与「一般规则下 reverse-charge 的 B2B 服务」，但第 14 页那串特殊服务（不动产/客运/文体娱/餐饮/短租等）**不进 3b**。 |
| 7 | 开支 | NL_DOMESTIC_PURCH | 任意 | **5b** | 可抵进项VAT（×业务使用%） | ✅ | 第 15–16 页：境内供应商开的荷兰 btw → 5b，仅税额。需满足业务用途/应税活动；私用、horeca 吃喝、vrijgesteld 用途、超 €227 personeelsvoorzieningen、错开 btw 等**不得抵**。「×业务使用%」对应 gemengd/privé 拆分（第 16 页）。 |
| 8 | 开支 | EU_B2B_REVERSE_PURCH | 任意 | **4b + 5b** | 4b 自核净额+VAT；5b 同额抵 | ✅ | 第 14 页：EU 货物 ICA + EU 被反向征收服务 → 4b（净额+自核 btw，不拆税率），5b 同额抵，净零。**注意例外**：与**不动产**相关的 EU 服务**进 2a 不进 4b**（第 14 页 Let op!）。 |
| 9 | 开支 | IMPORT_NON_EU | 任意 | **4a + 5b** | 4a 净额+进口VAT 自核；5b 抵 | ⚠️ | **仅在持有 vergunning artikel 23 时成立**（第 14 页）：有 art.23 → 进口 VAT 不在海关缴、挪进 4a 自核 + 5b 抵、净零。**无 art.23**：进口 VAT 由 Douane 当场征收，**不进 4a**，只凭海关单据作 voorbelasting 进 5b。所以我们这条要按「是否持 art.23」分叉。另：从非 EU 接收的**服务**被反向征收也进 4a。详见问题③。 |
| 10 | 开支 | EU_B2C_PURCH | 任意 | **❓待定** | 作为消费者付的外国VAT通常不进NL申报 | ❓→✅(不报) | **PDF 明确支持「不报」**：第 16/40 页 Let op!「De btw die u betaalt in andere EU-landen mag u **niet** aftrekken in uw Nederlandse btw-aangifte」——在其它 EU 国付的 btw 不得进荷兰申报（要走 EU 退税程序）。所以「不进 NL 申报」是对的；这不是「未覆盖」，是「官方明确排除」。详见问题④。 |

### 对 6 个悬而未决问题的回答

**① 1a/1b/1e 按 21/9/0 税率分档对不对？**
**✅ 方向正确，但要松绑「数字」**。PDF 把 1a 定义为 **hoog tarief**、1b 为 **laag tarief**、1e 为 **0% / niet belast**（第 11–12 页），**并未把 21/9/0 这三个数字写死在格子定义里**。建议工程实现上**用 treatment+rate 映射到「hoog/laag/zero」三档**，而**税率数值仍按我们 VAT 数据驱动的字典表**（符合红线 12「税率不写死成枚举」）。另外 **1c（overige tarieven 如 13% 食堂 forfait）**不在 21/9/0 三档内——见问题⑥。

**② EU_B2C 是否真按 NL 税率进 1a/1b？**
**⚠️ 有条件**。PDF 的逻辑是「阈值制」：**面向 EU 私人客户的（跨境）远程销售 + 数字服务，去年/今年累计 ≤ €10.000** 时，按荷兰规则、荷兰税率处理（落 1a/1b）；**一旦 > €10.000**，税基落客户/终点国——用 OSS 则不进荷兰申报，不用 OSS 则净额进 **3c**，btw 在终点国缴（第 14、26、38 页）。**我们 v1 不做 OSS**，因此**必须对 EU_B2C 远程销售做 €10.000 累计阈值监控**：未超→1a/1b 成立；接近/超过→要么报 3c+外国注册、要么开 OSS（我们没做）。**注意**：阈值只针对「远程销售货物 + 数字服务给私人」；常规「客户来荷兰当场消费」的本地销售本来就是 1a/1b，不受此约束。

**③ 进口走 4a 自核还是海关代缴不进申报？**
**⚠️ 取决于 art.23**。**有 vergunning artikel 23** → 走 **4a 自核 + 5b 抵**，净零（第 14 页）；**无 art.23** → 进口 VAT 由 **Douane 在 aangifte ten invoer 时征收**，**不进 4a**，凭海关单据作 voorbelasting 进 5b。对法律点名的特定原材料，verlegging bij invoer 强制、必须有 art.23。**工程上 IMPORT_NON_EU 需要一个布尔位「持有 art.23?」来分叉**。

**④ EU_B2C_PURCH 进不进 NL 申报？**
**✅ 不进**。第 16/40 页 Let op! 明确：在其它 EU 国付的 btw **不得**在荷兰申报里抵（要走 EU 退税程序）。作为消费者付的外国 VAT 既不是进项（无业务发票/反向征收）也不入荷兰任何格子。**我们这条置「不映射任何格子」是正确的官方口径**。

**⑤ 5a 求和是否含 2a/4a/4b 的自核 VAT？**
**口径如下（PDF 不直接给 5a 公式，但逻辑闭合）**：
- 「应缴销项侧」= **1a + 1b + 1c + 1d 的 btw + 2a + 4a + 4b 的自核 btw**（2a/4a/4b 的 rechterkolom 都是「verschuldigde btw」，第 12/14/25/27 页明说要当应缴申报）。**所以「销项合计」确实含 2a/4a/4b 的自核 VAT**。
- 「进项侧」= **5b（含真实采购进项 + 2a/4a/4b 可抵部分）**。
- **净应缴/应退 = 销项合计 − 5b**，由程序自动算（第 16/28/40 页）。
- **但 toelichting 不命名 5a/5c**（见 §2 开头）。结论：**我们若要展示 5a，定义应为「1a/1b/1c/1d 销项 + 2a + 4a + 4b 自核 btw 之和」；5c = 5a − 5b**。这是工程展示口径，**请会计确认是否与申报表 UI 的 5a/5c 完全一致**。

**⑥ 1c / 1d / 3c 在 v1 是否可一律置 0？**
- **1c（overige tarieven, behalve 0%）**：PDF 唯一点名场景是「sportkantine 选 13% forfait」（第 11 页）。**普通 ZZP/自由职业基本不会用** → **v1 可置 0**（但保留格子，别删）。⚠️ 须作者确认自己业务无任何「其它税率」收入。
- **1d（privégebruik）**：**不能简单置 0**——只要有**公司资产私用（车、gas/water/elektra/telefoon、把货物转私产、给自己/家人/关系人免费服务等）**，年末最后一期就要在 1d 补税（第 11、35–36 页）。**若作者有公司车私用或上述任何情形，1d 必填**；完全无公私混用才可置 0。建议 v1 至少留「年末 1d 手工填」入口。
- **3c（installatie/afstandsverkopen）**：只有「在别的 EU 国安装/组装」或「不用 OSS 的超阈值远程销售」才用（第 14 页）。**纯本地 ZZP、无 EU 远程销售 → v1 可置 0**；但与问题②联动：一旦 EU_B2C 超 €10.000 且不开 OSS，3c 就被激活。**建议默认 0、但随阈值监控可触发**。

---

## 5. 速查：哪个格子填净额、哪个填税额

| 格子 | 净额（Omzet/基数） | 税额（Btw） | 备注 |
|---|:---:|:---:|---|
| 1a | ✅ | ✅ | hoog（21%） |
| 1b | ✅ | ✅ | laag（9%） |
| 1c | ✅ | ✅ | overige（如 13% 食堂） |
| 1d | ❌ | ✅ | 仅税额，仅年末最后一期 |
| 1e | ✅ | ❌ | 0% / 反向征收供方侧 |
| 2a | ✅ | ✅ | 接收方自核（→5b 抵） |
| 3a | ✅ | ❌ | 出口非 EU |
| 3b | ✅ | ❌ | ICP（≡Opgaaf ICP） |
| 3c | ✅ | ❌ | 安装/远程销售 |
| 4a | ✅ | ✅ | 非 EU 进口(art.23)/服务自核（→5b 抵） |
| 4b | ✅ | ✅ | EU 采购/服务自核（→5b 抵） |
| 5b | ❌ | ✅ | 进项税额（含 2a/4a/4b 可抵部分） |
| 5a/5c | （合计） | （合计） | 程序自动算；toelichting 不逐格命名 |
| 5d/5e | — | — | **PDF 未覆盖** |

---

## 6. ⚠️ 需作者 / 会计最终确认的点

1. **5a/5c/5d/5e 编号**：2026 toelichting 只命名 5b，合计由程序自算。请确认申报表 UI（Mijn Belastingdienst Zakelijk）是否仍显示 5a/5c 标签、以及 5d/5e 在 2026 是否还有用途（本文按「不存在 / 不实现」处理）。
2. **税率数值（21/9/13/0）**：PDF 只说 hoog/laag/overige/0%，不背书数字。我们按红线 12 用数据驱动字典表，**请确认 2026 现行 hoog=21%、laag=9% 无变动**。
3. **EU_B2C 的 €10.000 阈值监控（问题②）**：我们 v1 不做 OSS。**请确认作者是否有任何面向 EU 私人客户的远程销售/数字服务**；若有，需要阈值监控 + 超阈值的处理路径（3c 或外国注册），否则会错报。
4. **IMPORT_NON_EU 是否持 vergunning artikel 23（问题③）**：决定走 4a 自核还是海关代缴+5b。**请作者确认是否申请了 art.23**。
5. **1c / 1d 是否真能置 0（问题⑥）**：1c 看是否有「其它税率」收入（食堂等）；**1d 看是否有公司车私用或任何公私混用**——若有，1d 不能置 0，须年末补税。
6. **herzieningsregeling 的 5 年/10 年/10% 阈值（§3.4）**：本 toelichting 不含这些数字，属税法细节，**须查 Wet OB / 会计师确认**后再实现投资品调整逻辑。
7. **gemengd / privé 拆分的具体比例算法（§3.5）**：PDF 只给原则（按 belaste:vrijgestelde omzet 比例，或按实际使用）。**汽车 forfait、business% 的具体计算须查 belastingdienst.nl/btw-gebruik / 汽车 hulpmiddel**。
8. **3b 含哪些「服务」**：第 14 页那串「不进 3b/ICP」的特殊服务（不动产、客运、文体娱入场、餐饮、短租等）地点规则特殊。**若作者会卖这类跨境 B2B 服务，需逐类核对落点**（多数不进 3b）。
9. **margeregeling、KOR、OSS**：v1 均不实现/不启用；若将来作者业务触及（二手货差额征税、年营业额 ≤€20k 想用 KOR、EU 远程销售），需另立里程碑并复核本指南相关段落。

---

## 7. M16 补充：家用储能与外部销售原件

> 2026-10-30 根据下列官方网页补充，独立于原 PDF 分析。M16 已规划但尚未实施，既有里程碑验收记录仍为历史。

### 7.1 普通 VAT 制度下的家用储能

税务局[储能指南](https://www.belastingdienst.nl/wps/wcm/connect/nl/btw/content/thuisbatterij-btw) 要求符合条件的电力交易、EMS、动态合同、正确抬头凭证及普通 VAT 状态。纯私人使用不符合条件；符合全额归属条件的购置可抵扣采购 VAT，售电报酬产生销项 VAT。网页还提示适用时应处理光伏卖电。

全额抵扣且兼有私人使用时，每年根据 EMS 信息、必要时结合能源账单确定私人使用。官方示例把未税购置成本分摊到投入使用当年及随后四个日历年，用年度基础乘当年私人比例及购置 VAT 税率，在 Q4 的 1d 填基础和税额。第六年起不再产生这项购置调整，持续卖电仍单独纳税。

示例：未税 €10,000、购置 VAT €2,100、私人比例 45%，年度基础为 `10,000 ÷ 5 × 45% = 900`，VAT 为 `900 × 21% = 189`。这不表示最初购置 VAT 只能抵扣五分之一。

### 7.2 外部销售原件与编号

按[官方 self-billing 规则](https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/btw/administratie_bijhouden/facturen_maken/factuureisen/aangepaste_regels_facturen/factuur_uitgereikt_door_afnemer)，须注意事先约定、正确供需双方身份、self-billing 标注和有效发票内容；供应方仍对 VAT 负责。供应方将其记为销售，可保留买方原编号或赋予内部连续编号。通用[发票要求](https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/btw/administratie_bijhouden/facturen_maken/factuureisen/factuureisen) 要求一个或多个连续编号系列及唯一发票号，唯一性不是完整规则。

### 7.3 应用边界与未覆盖情形

[M16](../plan/milestones/M16_zh.md) 冻结受限的普通 VAT 实现：最初全额抵扣、年度单独输入、已知小计及明确缺项提示、可靠的 1d 基础。不将 M10 的当年百分比算法推广到储能，也不在无法可靠确定时推断旧贡献的计税基础。KOR、部分初始抵扣、处置／特殊用途变化和通用资本品调整仍不属于 M16。

外部销售确认须依据真实销售证据及税额拆分，不根据平台净打款或混合家庭能源账单的净应付额。排除公司 P/L 只是记账选择，不是所得税免责结论；[所得税会分别判断活动及其关联](https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/winst/inkomstenbelasting/wanneer_bent_u_ondernemer_voor_de_inkomstenbelasting/)。
