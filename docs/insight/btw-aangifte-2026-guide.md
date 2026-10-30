# Dutch BTW (VAT) Quarterly Return Filing Guide — 2026

> 🌐 **English** · [中文](btw-aangifte-2026-guide_zh.md)

> **Source**: Official Belastingdienst document *Toelichting bij de aangifte omzetbelasting (btw) 2026* / *Explanatory notes to the 2026 VAT return (turnover tax)* (order no. OB061-1T62FD, version Mei 2026).
> This document is based on a **full read of all 41 pages**: pp. 3–16 cover "**Voor ondernemers in Nederland**" (entrepreneurs established in the Netherlands — authoritative; applicable to our eenmanszaak/ZZP); pp. 17–28 cover "Voor *niet* in Nederland gevestigde ondernemers" (Dutch); pp. 29–40 cover the same section in English ("For entrepreneurs *not* established in the Netherlands"); p. 41 is the back cover.
> **The NL-established version (pp. 3–16) is the authoritative reference for implementation**; the English section (pp. 29–40) is used only to verify term translations.
>
> **Important scope disclaimer**: This toelichting is a "filing guide", **not** the tax law itself. It clearly explains what to enter in each field, but **for certain mechanisms (the 5-year/10-year/10% thresholds of the herzieningsregeling, the exact apportionment algorithm for gemengd gebruik, and the clawback details of the KOR) it only states the principle and refers to other pages on belastingdienst.nl — this document does not elaborate**. Anything not specified in the PDF is marked "**PDF not covered**" in this guide; no guesswork is made.

---

> **M16 supplement (2026-10-30)**: [§7](#7-m16-supplement--home-batteries-and-external-sales-originals) separately records official web guidance for home batteries and self-billing. It supplements the original PDF review; it does not imply that the PDF established the detailed five-year battery calculation. Implementation boundaries are in [M16](../plan/milestones/M16.md).

## 0. One-line conclusion (for the time-pressed)

- Of our 10-row mapping proposal: **7 rows ✅ align with the official guidance**, **2 rows ⚠️ diverge or require additional conditions**, and **1 row ❓ the PDF explicitly states should not be included in the Dutch return (i.e. "leave blank / do not report" is correct)**.
- The 3 most critical points:
  1. **⚠️ Proposal row 4 (EU_B2C always enters 1a/1b at NL rates) only holds when "OSS is not used" and "the €10,000 distance-sales threshold has not been exceeded"**. Once the threshold is exceeded, EU rules require the tax base to fall in the customer's country (via OSS / filing in that country), not in 1a/1b. PDF pp. 11, 14 (3c) are explicit.
  2. **⚠️ Proposal row 9 (IMPORT_NON_EU self-assessed via 4a) only holds when the taxpayer holds a vergunning artikel 23 (deferred import VAT licence)**. Without an art.23 licence, import VAT is collected by Douane at the border and **does not go into 4a** — it can only be claimed as voorbelasting in 5b. PDF p. 14 is explicit.
  3. **❌ The 5a/5c/5d/5e box numbers assumed in our proposal do not exist in the 2026 toelichting**: Section 5 of the explanatory notes only describes **5b (voorbelasting, input VAT)**. The total payable/refundable is calculated "automatisch" by the filing system (corresponding to 5a total output / 5c net on the return form, but the toelichting **does not name 5a/5c individually**, and **5d/5e are completely absent**). See §2 "The truth about 5a/5c/5d/5e".

---

## 1. Overall structure of the return

### 1.1 Five rubriek (sections)

The PDF divides the return questions into 5 rubrieken (pp. 10–15 / pp. 35–40 in English):

| Rubriek | Dutch heading | English translation | Meaning |
|---|---|---|---|
| **1** | Prestaties binnenland | Domestic supplies | Supplies/services you make in the Netherlands (output side), split by rate + private use |
| **2** | Verleggingsregelingen binnenland | Domestic reverse-charge schemes | Supplies/services you **receive** for which btw has been shifted (verlegd) to you domestically |
| **3** | Prestaties naar of in het buitenland | Supplies of goods/services to or in other countries | Exports (non-EU), intra-EU supplies (ICP), installation/distance sales |
| **4** | Prestaties vanuit het buitenland aan u geleverd | Supplies provided to you from abroad | Imports/EU purchases you **receive** for which you must self-assess btw in the Netherlands |
| **5** | Voorbelasting | Input VAT | Input tax deduction + settlement (amount payable/refundable) |

### 1.2 Filing period (aangiftetijdvak)

- The PDF repeatedly refers to three types of tijdvak: **maand / kwartaal / jaar** (e.g. "Het tijdvak waarover u aangifte moet doen" varies according to the annual notification letter). **The specific period is specified by the tax authority in the aangiftebrief**; for ZZP the default is usually **quarterly (kwartaal)**.
- **A return must be filed even if there is no turnover in the period or a refund is expected** ("Doe altijd btw-aangifte als deze voor u klaarstaat", p. 4).
- **Nil return (nihilaangifte)**: Answer "Nee" and file a nil return only if **none** of the 7 trigger events listed on pp. 4/18 occurred in the period:
  - btw in rekening gebracht (output btw charged)
  - btw over zakelijke uitgaven in aftrek kunt brengen als voorbelasting (input tax claimable)
  - te maken gehad met verleggingsregeling (reverse-charge involved)
  - goederen geleverd tegen het 0%-tarief (0% supplies made)
  - goederen verworven uit andere EU-landen (goods acquired from other EU countries)
  - diensten afgenomen uit andere landen waarbij de btw verlegd is naar u (services received from abroad with btw shifted to you)
  - goederen ingevoerd en gebruik gemaakt van een vergunning artikel 23 (imports under art.23 licence)
  - btw over privégebruik moet aangeven (private-use btw to report)
  - installatie-/afstandsverkopen binnen de EU verricht (installation/distance sales within the EU)

### 1.3 Reporting basis: invoice date vs. receipt of payment

The PDF consistently provides the **factuurdatum (invoice date)** basis and gives no "cash basis" option:

- **ICP goods supplies (3b)**: "gaat u uit van de factuurdatum, ook als de feitelijke levering in een volgend tijdvak plaatsvindt" (use the invoice date, even if physical delivery falls in a later period) — pp. 13, 25, 38.
- **ICP services (3b) / services received from the EU (4b)**: "gaat u uit van het tijdvak waarin deze diensten worden geleverd. De factuurdatum is hierbij niet van belang" (use the period in which the services are **performed**; the invoice date is irrelevant) — pp. 13, 14, 38.
- **Conclusion**: These explanatory notes use an invoice/accruals basis. There is no explanation of the cash-accounting scheme (kasstelsel) — kasstelsel belongs to a separate regime and is **PDF not covered**.

> Additional note: **Bad-debt relief (oninbare vorderingen, pp. 7, 21, 33)**: When a customer's debt is deemed irrecoverable (at the latest one year after the agreed due date, or after the statutory 30-day payment term), the output btw that was previously reported in 1a/1b is **reversed** there, and the corresponding turnover is deducted from the 1a/1b turnover amounts.

### 1.4 Rounding rules (Bedragen afronden)

Pp. 10 / 23 / 35:

> **"Rond alle bedragen af op hele euro's. Dit mag u in uw voordeel doen. Zet bij negatieve bedragen een minteken (-) voor het bedrag."**
> Translation: Round off all amounts to **whole euros**. You may round **in your favour**. For negative amounts, place a minus sign (-) before the amount.

Implementation note: **All box amounts are rounded to whole euros (drop the cents)**, and **rounding in the taxpayer's favour is permitted**. Negative amounts carry a leading "−" sign. Note that this is **return-level** rounding; it does not affect invoice/line-level calculations (line level continues to follow our existing quantize-to-minor-unit rules).

---

## 2. Precise definition of each box (rubriek) — key points

> Convention: **"Net amount / Omzet" = the tax base excluding VAT (called kolom 'Omzet' or linkerkolom in the PDF)**; **"Tax amount / Btw" = the VAT amount (called kolom 'Btw' or rechterkolom)**.

### The truth about 5a / 5c / 5d / 5e (stated upfront to avoid confusion)

**The 2026 toelichting mentions and describes only `5b Voorbelasting` throughout the entire document.** The heading of Section 5 is "5 Voorbelasting" and the only named box beneath it is **5b**.

- **Nowhere in the text are `5a`, `5c`, `5d`, or `5e` named.**
- The "total output tax payable (traditional 5a)" and "net payable/refundable (traditional 5c)" are **calculated automatically** by the filing system: pp. 16 / 28 / 40 state "**Totaal te betalen of terug te vragen — Het aangifteprogramma berekent automatisch het bedrag dat u moet betalen of terugvraagt**".
- Therefore: **When aggregating, our system only needs to produce the individual detail boxes (net amounts and tax amounts for 1a/1b/1c/1d/1e/2a/3a/3b/3c/4a/4b, plus the input tax amount for 5b)**. "5a = total output" and "5c = 5a − 5b" are **display-level totals** that we can compute ourselves (see §4, question ⑤). **5d/5e do not exist in this explanatory document**; per the PDF, **there is no need to implement boxes for them** (5d/5e appeared in historical/other contexts for KOR small-business relief, but are not mentioned in the 2026 toelichting — marked "**PDF not covered**").

> ⚠️ **Requires confirmation from the author/accountant**: Does the actual return UI (Mijn Belastingdienst Zakelijk) still display 5a/5c labels for the totals? This toelichting takes the position that "the system calculates the totals automatically" without naming individual boxes.

---

### Rubriek 1 — Prestaties binnenland (domestic supplies, output side)

#### 1a — Leveringen/diensten belast met **hoog tarief** (21%)
- **Enter**: The `Omzet` column gets the **net amount (tax base)**; the `Btw` column gets the **output tax amount**. (Pp. 11, 24, 36: "Vul in de kolom 'Omzet' het bedrag waarover btw wordt berekend in. Vul in de kolom 'Btw' het btw-bedrag in.")
- **Includes**: Supplies of goods and services made in the Netherlands taxed at the **high rate (hoog tarief = 21%)**.
- Both net amount and tax amount are entered.

#### 1b — Leveringen/diensten belast met **laag tarief** (9%)
- **Enter**: Net amount + output tax amount (same kolom rules as 1a).
- **Includes**: Domestic supplies/services taxed at the **low rate (laag tarief = 9%)**.

> ⚠️ Note: The PDF **does not embed the figures "21%" or "9%" in the definitions of 1a/1b** — it only says "hoog/laag tarief". The specific rate values must be looked up on the btw-tarieven page of belastingdienst.nl. **The current 2026 rates hoog=21%, laag=9% are widely known, but the explanatory notes themselves do not endorse specific figures** (marked "rate figures not stated in PDF; using current 21/9").
>
> **Margeregeling (margin scheme)** within the same band: For second-hand goods taxed on the profit margin, the `Btw` column holds the "btw on the margin"; a negative margin cannot offset positive turnover (pp. 11, 24) — we do not implement marge in v1, marked "not implemented".

#### 1c — Leveringen/diensten belast met **overige tarieven, behalve 0%** (other rates, excluding 0%)
- **Enter**: Net amount + tax amount (same kolom rules implied).
- **Includes**: The only scenario specifically named is "sportkantine opting for a **13% forfaitair** rate on total canteen receipts (inclusive of btw)" (p. 11: "Vul deze vraag in als u een sportkantine hebt en u ervoor kiest om een forfaitair btw-tarief van 13% te betalen over uw totale kantineontvangsten inclusief btw.").
- **Essentially never applicable to a ZZP freelancer** (see §4, question ⑥).

#### 1d — **Privégebruik** (private-use adjustment)
- **Enter**: **Only the tax amount (btw die u moet betalen over het privégebruik)**; entered **only in the last return of the year** (pp. 11, 24, 35–36: "Vul deze vraag alleen in de laatste aangifte van het jaar in.").
- **Purpose**: Corrects excess input tax deducted during the year on goods/services that were partly used privately. Typical scenarios:
  - Private use of a business vehicle (including woon-werkverkeer commuting, which the PDF also treats as private use);
  - Private use of gas, water, elektra, telefoon;
  - Fictieve levering/dienst (withdrawing business assets for private use → btw based on the asset value; when ceasing business and retaining goods privately → report immediately in the period of transfer, **not** at year-end, see p. 11 Let op!).
- **Algorithm details (how to determine business%, which forfait applies) are not elaborated in the PDF** — it refers to belastingdienst.nl/btw-gebruik and the hulpmiddel "Btw of btw-aftrek over uw auto berekenen". Marked "**PDF not covered — algorithm**".

#### 1e — Leveringen/diensten belast met **0% of niet bij u belast** (0% or not taxed at your level)
- **Enter**: **Only the net amount (omzet)**; **no tax amount** (pp. 12, 24, 36).
- **Includes** (p. 12 specifies two categories):
  1. "leveringen van goederen en diensten in Nederland die vallen onder het **0%-tarief** (zie tabel II bij de Wet op de omzetbelasting 1968), **behalve export (vraag 3a) of intracommunautaire leveringen (vraag 3b)**" — **domestic 0% supplies, but excluding exports (→ 3a) and ICP supplies (→ 3b)**.
  2. "leveringen van goederen en diensten **waarbij de btw verlegd is naar een andere ondernemer**" — **domestic supplies where you, as the supplier, have shifted (verlegd) the btw to another entrepreneur** (i.e. the supplier side of the domestic verleggingsregeling goes into 1e as a net amount).
- P. 12 "Wanneer btw verleggen" lists the domestic reverse-charge scenarios (supplier enters 1e; recipient enters 2a):
  - zakendoen met het buitenland
  - onderaanneming en personeel uitlenen in sectoren **bouw, scheepsbouw, schoonmaakbedrijven, hoveniers** (subcontracting and staff lending in construction/shipbuilding/cleaning/horticulture)
  - levering van telecommunicatiediensten aan een andere ondernemer
  - handel in **mobiele telefoons, (computer)chips, spelcomputers, laptops en tablets**
  - onroerende zaken (immovable property where a taxable supply has been opted for)
  - **afval en oude materialen** (waste and scrap materials, including related processing services)
  - verplichte verlegging bij gas- en elektriciteitscertificaten
  - **executieverkopen** (forced sales), **verkoop van goud** (gold sales), **overdracht van emissierechten** (transfer of emission allowances)

---

### Rubriek 2 — Verleggingsregelingen binnenland (domestic reverse-charge, recipient side)

#### 2a — Leveringen/diensten waarbij de btw naar u is verlegd (btw reverse-charged **to you**)
- **Enter**: **Both net amount and tax amount**. As the recipient you **calculate** (uitrekenen) the reverse-charged btw yourself and report it as "verschuldigde btw (output tax due)" in 2a (pp. 12, 25, 37).
- **Includes**: Domestic supplies of goods/services made to you by another entrepreneur who has shifted the btw to you (invoice states "**btw verlegd**" plus your btw-id). Applicable scenarios are the same as the "Wanneer btw verleggen" list under 1e above (recipient perspective).
- **Net effect**: "Het btw-bedrag dat u hebt aangegeven, kunt u onder voorwaarden weer als voorbelasting aftrekken bij vraag 5b … U betaalt per saldo dan geen btw. Toch moet u vraag 2a en 5b invullen." — **The output btw reported in 2a can be reclaimed as input tax in 5b (subject to deduction conditions), resulting in a net zero; but both 2a and 5b must be completed** (pp. 12, 25, 37).

---

### Rubriek 3 — Prestaties naar of in het buitenland (exports and cross-border supplies, output side)

#### 3a — Leveringen naar landen buiten de EU (**uitvoer / non-EU exports**)
- **Enter**: **Only the net amount (omzet)**; no tax amount (pp. 13, 25, 37).
- **Includes**: Turnover from goods exported from the Netherlands to **countries outside the EU**; **also includes** goods placed under a douane-entrepot (customs bonded warehouse) regime.

#### 3b — Leveringen naar of diensten in landen binnen de EU (**ICP / intra-EU supplies**)
- **Enter**: **Only the net amount (het bedrag van de goederen en diensten die u … hebt geleverd naar of in andere landen binnen de EU)**; no tax amount (0% rate applies, pp. 13, 26, 38).
- **Key constraint (relationship with the ICP listing)**: "**Het bedrag dat u bij deze vraag invult, moet u specificeren in de opgaaf ICP.**" — **The amount entered in 3b must be itemised in the Opgaaf ICP (ICP listing)**. That is, **3b total ≡ Opgaaf ICP total** (pp. 13, 26, 38).
- **Timing**:
  - ICP **goods** supplies: use the **factuurdatum** (even if physical delivery falls in a later period).
  - ICP **services**: use the period in which the services are **performed**; the factuurdatum is irrelevant.
- **Conditions for the 0% rate** (pp. 13, 26, 38):
  1. The administration can prove that the goods were transported to another EU country;
  2. It can be proved that the supply was made to an entrepreneur holding a valid btw-id (non-Dutch);
  3. **The Dutch Opgaaf ICP was filed on time, correctly, and in full**.
- **Services excluded from 3b/ICP** (p. 14, "Welke diensten mag u niet opnemen…" list): Services that are vrijgesteld / 0% in the customer's own country; services filed via OSS; services related to **immovable property** (e.g. rental/maintenance); **personenvervoer (passenger transport)**; admission to and on-site services for cultural/artistic/sporting/scientific/entertainment/educational events; restaurant/catering services; short-term hire of means of transport (≤30 days) / vessel hire (≤90 days) — these B2B services **do not follow the general reverse-charge rule into 3b** (special place-of-supply rules apply).

#### 3c — Installatie/afstandsverkopen binnen de EU (**EU installation + distance sales**)
- **Enter**: **Only the net amount (omzet)** (pp. 14, 26).
- **Includes** (p. 14):
  - **Installation/assembly** of goods in another EU country (installatie/montage) — btw is payable in the EU country where installation/assembly takes place;
  - **Afstandsverkopen (distance sales) when you do not use OSS (eenloketsysteem)**, provided all conditions are met:
    - Customer is: a particulier / an entrepreneur making only vrijgesteld supplies / a non-entrepreneur legal entity;
    - You (directly or indirectly) arrange transport of the goods from the Netherlands to the customer;
    - **Last year and/or this year** your distance sales + digital services to particulieren **have exceeded the €10,000 threshold**.
- **Place of taxation**: Distance-sales btw is due **in the EU country where the transport of the goods ends** (p. 14: "moet u btw betalen in het EU-land waar het vervoer van uw goederen eindigt").
- **Relationship to OSS**: **Distance-sales turnover processed through OSS is not entered in the Dutch return** (OSS is filed on the EU-country side; the Dutch return "Hierin vult u alleen Nederlandse btw in", p. 11).

---

### Rubriek 4 — Prestaties vanuit het buitenland aan u geleverd (supplies received from abroad, self-assessment side)

#### 4a — Leveringen/diensten uit landen **buiten de EU** (non-EU imports, self-assessed)
- **Enter**: **linkerkolom (left column) = net amount (waarde van de goederen/diensten); rechterkolom (right column) = btw**; **no split by different tax rates required** (pp. 14, 27: "U hoeft deze bedragen niet te splitsen naar de verschillende btw-tarieven.").
- **Two scenarios included** (p. 14, NL-established version):
  1. **U hebt goederen ingevoerd van buiten de EU én daarbij gebruikgemaakt van de verleggingsregeling bij invoer (vergunning artikel 23)** — you imported goods from outside the EU **and used the import reverse-charge (art.23 licence)**: "Bij een vergunning artikel 23 hoeft u bij de Douane geen btw te betalen bij de zogenoemde aangifte ten invoer. In plaats daarvan geeft u de btw aan in uw btw-aangifte en betaalt u per saldo niets." — **With art.23, import VAT is not paid to Douane at the border; instead it is self-assessed in 4a and offset in 5b, resulting in net zero.**
  2. **U hebt diensten afgenomen van een ondernemer van buiten de EU, die de btw u heeft verlegd** — services received from a non-EU entrepreneur with btw reverse-charged to you, reported in the Dutch return.
- **What is vergunning artikel 23** (p. 14): **A deferred import VAT licence**. Without it, import VAT is collected by Douane at the aangifte ten invoer. **For certain "specific raw materials (ruwe grondstoffen genoemd in de wet op de omzetbelasting)", the import reverse-charge is mandatory and an art.23 licence must be applied for** (p. 14).
- **Net effect**: "Per saldo betaalt u dan geen btw. Toch moet u vraag 4a en 5b volledig invullen." (Both 4a and 5b must be completed in full; net zero.)

> ⚠️ **Key boundary**: P. 14 makes clear — **4a is only completed when the art.23 verlegging was used, or when services from outside the EU were received with btw reverse-charged to you**. **Import VAT on ordinary imports (without art.23) is collected by Douane and does not go into 4a**; it may only be claimed as voorbelasting in 5b (supported by customs documents). See §4, question ③.

#### 4b — Leveringen/diensten uit landen **binnen de EU** (EU purchases, self-assessed)
- **Enter**: **linkerkolom net amount + rechterkolom btw**; **no split by rate required** (pp. 14, 27).
- **Two scenarios included** (p. 14):
  1. **U hebt goederen gekocht van ondernemers uit andere EU-landen die naar Nederland vervoerd zijn** — **intracommunautaire verwerving (ICA / intra-community acquisition)**: the supplier charged no foreign btw; you self-assess in the Netherlands (the mirror image of ICP from the buyer's side).
  2. **U hebt diensten afgenomen van een ondernemer uit een ander EU-land, die de btw naar u heeft verlegd** — services received from an EU entrepreneur with btw reverse-charged to you, reported in the Netherlands. **Exception**: Services related to immovable property **go into 2a, not 4b** (p. 14: "Dit geldt niet voor diensten aan onroerende zaken. Die vult u in bij vraag 2a.").
- **Timing**: ICA goods use the factuurdatum; services received from the EU use the period in which the services are **performed** (factuurdatum is irrelevant, p. 14 Let op!).
- **Net effect**: Same as 4a — "Per saldo betaalt u dan geen btw. Toch moet u vraag 4b en 5b volledig invullen."

---

### Rubriek 5 — Voorbelasting (input tax and settlement)

#### 5b — Voorbelasting (input tax deduction)
- **Enter**: **Only a tax amount (een btw-bedrag, input VAT)**; no net-amount column (pp. 15, 27, 39).
- **Composition** (p. 15):
  1. **btw die andere ondernemers aan u in rekening hebben gebracht** — Dutch btw charged to you by suppliers (purchases/costs/investments), deductible **even if you have not yet paid your suppliers** ("Ook als u uw leveranciers nog niet hebt betaald").
  2. **btw die u moet aangeven omdat de btw naar u is verlegd** — btw you self-assessed due to reverse-charges (2a, 4a, 4b), which can be **offset here in equal amounts**.
- **Deduction conditions** (pp. 15, 27, 39):
  - Supported by a **legally compliant invoice**;
  - **U gebruikt de goederen en diensten zakelijk (business use)** — btw on purely private use is not deductible;
  - **U gebruikt ze voor activiteiten die belast zijn met btw** — deductible only for **taxable** activities; input tax on vrijgestelde (exempt) activities **is not deductible**.
  - **Let op!**: Supplies with btw reverse-charged to the recipient, and supplies at 0%, **count as "belaste bedrijfsactiviteiten"** (i.e. deduction rights are not lost merely because the rate is 0% or the transaction is verlegd).
- **Input tax that may not be deducted (Welke btw mag u niet als voorbelasting aftrekken, pp. 16, 40)**:
  - privé-aankopen (private purchases);
  - uitgaven voor **vrijgestelde** omzet (expenditure for exempt turnover);
  - uitgaven voor **niet-belastbare** omzet (expenditure for non-taxable turnover);
  - **eten en drinken in de horeca** (food and drink consumed at restaurants/cafes);
  - personeelsvoorzieningen exceeding **€227 per person per year** (once the threshold is exceeded, even the employee's own contribution is non-deductible);
  - btw charged **ten onrechte** (erroneously) by a supplier;
  - **Let op!**: btw paid **in other EU countries** **may not** be reclaimed in the Dutch return (the EU refund procedure on belastingdienst.nl must be used instead).

> **5b ≠ net amounts in 4a/4b**: The btw self-assessed in 4a/4b goes into the rechterkolom (output side) of 4a/4b **and also** into 5b (input side) as an offsetting credit; 2a works the same way. Therefore 5b is the sum of "genuine purchase input tax + the deductible portion of all reverse-charge self-assessments".

#### 5a / 5c / 5d / 5e
- **Not named in the PDF** (see the opening of this section, "The truth about 5a/5c/5d/5e"). Total payable/refundable is calculated automatically by the filing system (pp. 16/28/40). **5d/5e: PDF not covered.**

---

## 3. Key mechanisms (as set out in the official guidance)

### 3.1 Reverse-charge (verleggingsregeling / btw verlegd)

The PDF distinguishes three scenarios; **the net effect in all cases is "self-assess + equal offset = net zero", but the boxes differ**:

| Scenario | Role | Output-side box | Input side | Net effect |
|---|---|---|---|---|
| Domestic verlegging, **supplier** | You supply and shift btw to the other entrepreneur | **1e** (net amount) | — | You collect no btw |
| Domestic verlegging, **recipient** | The other party shifts btw to you | **2a** (net + self-assessed btw) | **5b** equal offset | Net zero |
| Cross-border self-assessment — non-EU (art.23 import / non-EU services) | Recipient | **4a** (net + self-assessed btw) | **5b** equal offset | Net zero |
| Cross-border self-assessment — EU (ICA / EU services) | Recipient | **4b** (net + self-assessed btw) | **5b** equal offset | Net zero |

Key point: **In reverse-charge situations, both the "net amount (base)" and the "self-assessed btw" must be entered in the corresponding box**; the deductible portion is offset in 5b. For the supplier, a verlegd supply counts as a "belaste bedrijfsactiviteit" (pp. 15, 39), preserving the supplier's input tax deduction rights.

### 3.2 Intra-EU supplies (intracommunautaire prestaties) and the Opgaaf ICP

- **3b (output-side net) must be reconciled line-by-line with the Opgaaf ICP**: pp. 13/26/38 explicitly state "moet u specificeren in de opgaaf ICP".
- **The ICP is a separate listing**, submitted via Mijn Belastingdienst Zakelijk; **no separate invitation is sent (geen uitnodiging)** — the entrepreneur must independently determine whether it needs to be filed (pp. 8, 21, 33).
- **Purchase mirror**: As an EU buyer, your ICA goes into **4b** (self-assessed) and is not reported in the ICP (ICP is a supplier obligation).
- **New/almost-new means of transport** supplied to a private individual/legal entity without a btw-id: This is treated as an ICA supply but **cannot be included in the ICP** (the other party has no btw-id); a copy of the invoice plus an explanatory letter must be sent to Belastingdienst/Central Liaison Office, Postbus 378, 7600 AJ Almelo (pp. 13, 26).

### 3.3 Imports / non-EU (invoer) and Artikel 23

- **With vergunning artikel 23 (deferred import VAT licence)**: Import VAT is not paid at the border; it is **shifted to 4a for self-assessment + offset in 5b**, net zero (p. 14).
- **For the legally designated "specific raw materials (bepaalde ruwe grondstoffen)"**, the import reverse-charge is mandatory and an art.23 licence must be applied for (p. 14).
- **Without art.23**: Import VAT is collected by Douane at the aangifte ten invoer — **does not go into 4a**; that import VAT already paid can be claimed as voorbelasting in 5b using customs documents (the PDF on p. 14 only references this indirectly via "In plaats daarvan…" without elaborating the no-licence case; supplemented here on the basis of tax law general knowledge, marked "**PDF does not positively address the no-art.23 scenario**").

### 3.4 Voorbelasting (5b) and capital goods / herzieningsregeling (capital goods adjustment scheme)

- **Input tax is in principle deducted in a single period** — "the period in which that btw was charged to you" (p. 15: "U doet dat in het aangiftetijdvak waarin die btw aan u in rekening is gebracht") — **including for capital goods (investeringen)**: **deducted in full in the period of acquisition, not spread over the depreciation period**.
- **Herziening (subsequent adjustment)**: Pp. 7, 21, 33: "Bij de aanschaf van goederen of diensten hebt u bepaald welk gedeelte u deze gebruikt voor belaste omzet. Later bekijkt u of dit nog steeds klopt." — At acquisition, deduction is taken according to the proportion of taxable turnover. **If that proportion changes subsequently**:
  - Too much deducted → **herzien** (adjust downward): the over-deducted btw is added to **rubriek 1** as additional output tax due;
  - Too little deducted → the additionally deductible btw is added to **5b** as voorbelasting.
- **5-year (movable goods) / 10-year (immovable property) / 10% threshold**: **These figures are not given in this toelichting**; it only refers to belastingdienst.nl/btw-gebruik "btw-aftrek bij (niet-)investeringsgoederen en de herziening ervan". Marked "**PDF not covered — the 5/10-year and 10% herziening thresholds are tax-law detail; consult tax law/accountant**".

### 3.5 Gemengd gebruik / privégebruik (mixed use / private use)

- **Three categories of input tax for mixed (belaste + vrijgestelde) omzet** (pp. 16, 28, 40):
  1. Used exclusively for **taxable** turnover → btw **fully deductible**;
  2. Used exclusively for **exempt** turnover → btw **not deductible at all**;
  3. Used for **both** → split into deductible/non-deductible according to the "**belaste : vrijgestelde omzet ratio**"; **if actual use can be demonstrated to differ, actual use may be used instead**.
- **Three options for goods/services used for both business and private purposes (zakelijk + privé)** (pp. 16, 28):
  1. **U trekt helemaal geen btw af** (deduct no btw at all);
  2. **U trekt geen btw af voor het deel dat u privé gebruikt of gaat gebruiken** (do not deduct the portion attributable to private use upfront);
  3. **U trekt de btw volledig af, maar betaalt aan het einde van het jaar btw voor het privégebruik** (deduct in full, then pay btw on private use in **1d** at year-end).
- **How to determine business%, which vehicle forfait applies**: The PDF gives no formula; it refers to belastingdienst.nl and the vehicle hulpmiddel (marked "**PDF not covered — specific apportionment algorithm**").

### 3.6 OSS / afstandsverkopen (distance sales, €10,000 threshold) — for reference only; not implemented in v1

- Threshold: **Cross-border distance sales + digital services to particulieren, cumulative last year and/or this year > €10,000** (pp. 14, 26, 38).
- **Below threshold**: May follow Dutch rules and Dutch rates, entered in 1a/1b (domestic treatment).
- **Above threshold**: btw falls in the **EU country of the customer/transport destination**:
  - **Using OSS (eenloketsysteem)**: That portion of turnover **is not entered in the Dutch return** (pp. 11, 36: "Hierin vult u alleen Nederlandse btw in" / "Do not enter One Stop Shop (OSS) turnover");
  - **Not using OSS**: Distance-sales net amount goes into **3c**; btw is filed and paid directly in the destination country.
- **We do not implement OSS in v1** → see §4, questions ②/⑥.

### 3.7 KOR (kleineondernemersregeling) — for reference only; not enabled in v1

- **NL-KOR**: Dutch annual turnover ≤ **€20,000** makes the entrepreneur eligible for KOR and btw-vrijstelling (VAT exemption); they **no longer charge btw to customers**, and "u kunt dan geen btw meer aftrekken of terugvragen, en mogelijk moet u een deel van de eerder ontvangen btw terugbetalen" (input tax can no longer be deducted or refunded, and previously deducted btw may have to be repaid) — p. 8.
- **EU-KOR**: For business done in other EU countries with total EU annual turnover below the EU threshold and the threshold of that country, a EU-wide small-business exemption can be applied for; a **quarterly opgaaf kwartaalomzet** must be filed (pp. 8, 22).
- **We do not enable KOR in v1** (marked "not implemented").

---

## 4. ⭐ Cross-check: our (treatment × rate) → box mapping proposal

> Legend: ✅ aligns with official guidance | ⚠️ diverges or requires additional conditions (official correct position + page reference given) | ❓ not clearly covered by PDF / official guidance says not to include in the return.

| # | Side | Treatment | Rate | Proposal → Box | What the proposal reports | Verdict | Official basis and correct position |
|---|---|---|---|---|---|---|---|
| 1 | Sales | NL_DOMESTIC | 21% | **1a** | Net amount + output VAT | ✅ | P. 11: hoog tarief → 1a; Omzet column = net amount, Btw column = tax amount. (PDF does not endorse the "21%" figure; using current hoog=21%.) |
| 2 | Sales | NL_DOMESTIC | 9% | **1b** | Net amount + output VAT | ✅ | P. 11: laag tarief → 1b; net amount + tax amount. (laag=9%, same note as above.) |
| 3 | Sales | NL_DOMESTIC | 0% | **1e** | Net amount | ✅ | P. 12: domestic 0%-tarief (tabel II), **but excluding export (3a) and ICP (3b)** → 1e, net amount only. Distinguish "genuine domestic 0%" from "export/ICP". |
| 4 | Sales | EU_B2C | 21/9% | **1a/1b** | Net amount + output VAT (at NL rates; OSS not used) | ⚠️ | **Holds only when the €10,000 distance-sales threshold has not been exceeded**: below threshold, Dutch rates apply → 1a/1b (p. 14 threshold definition + p. 11 1a/1b). **Once cumulative total > €10,000**: EU rules require the tax base to fall in the customer's country (OSS → not in Dutch return; no OSS → net amount → **3c**, btw payable in destination country). **Since we do not implement OSS in v1, we must monitor the €10,000 cumulative threshold for EU_B2C**: below → 1a/1b; approaching/exceeded → 3c or foreign registration (business red line). See question ②. |
| 5 | Sales | EXPORT_NON_EU | 0% | **3a** | Net amount | ✅ | P. 13: uitvoer to outside EU → 3a, net amount only (including goods in douane-entrepot). |
| 6 | Sales | EU_B2B_REVERSE | 0% | **3b (+ICP)** | Net amount (+ ICP listing) | ✅ | P. 13: ICP supply → 3b, net amount only (0%); **3b amount must be itemised in the Opgaaf ICP** (3b total ≡ ICP total). Note 3b covers both "ICP goods" and "B2B services under the general reverse-charge rule", but the special services listed on p. 14 (immovable property/passenger transport/cultural-sporting-entertainment events/catering/short-term hire etc.) **do not go into 3b**. |
| 7 | Expenses | NL_DOMESTIC_PURCH | any | **5b** | Deductible input VAT (× business use %) | ✅ | Pp. 15–16: Dutch btw charged by domestic suppliers → 5b, tax amount only. Subject to business-use/taxable-activity conditions; private use, horeca food/drink, vrijgesteld-use, personeelsvoorzieningen exceeding €227, erroneously charged btw etc. **are not deductible**. "× business use %" corresponds to the gemengd/privé apportionment (p. 16). |
| 8 | Expenses | EU_B2B_REVERSE_PURCH | any | **4b + 5b** | 4b self-assessed net + VAT; 5b equal offset | ✅ | P. 14: EU goods ICA + EU reverse-charged services → 4b (net + self-assessed btw, no rate split), 5b equal offset, net zero. **Note exception**: EU services relating to **immovable property** go into **2a not 4b** (p. 14 Let op!). |
| 9 | Expenses | IMPORT_NON_EU | any | **4a + 5b** | 4a net + import VAT self-assessed; 5b offset | ⚠️ | **Holds only when vergunning artikel 23 is held** (p. 14): with art.23 → import VAT not paid at Douane; shifted to 4a for self-assessment + 5b offset, net zero. **Without art.23**: import VAT collected by Douane, **does not go into 4a**; claim as voorbelasting in 5b using customs documents. Therefore this row requires a boolean flag "holds art.23?" to branch. Also: **services** from outside the EU with btw reverse-charged also go into 4a. See question ③. |
| 10 | Expenses | EU_B2C_PURCH | any | **❓ TBD** | Foreign VAT paid as consumer usually not in NL return | ❓→✅ (not reported) | **PDF explicitly supports "do not report"**: pp. 16/40 Let op! "De btw die u betaalt in andere EU-landen mag u **niet** aftrekken in uw Nederlandse btw-aangifte" — btw paid in other EU countries may not be deducted in the Dutch return (EU refund procedure must be used). Therefore "not included in the NL return" is the correct official position; this is not an uncovered case but an **officially excluded** one. See question ④. |

### Answers to the 6 open questions

**① Is splitting 1a/1b/1e by 21/9/0 rate correct?**
**✅ Directionally correct, but decouple the specific figures.** The PDF defines 1a as **hoog tarief**, 1b as **laag tarief**, 1e as **0% / niet belast** (pp. 11–12); **the figures 21/9/0 are not hard-coded into the box definitions**. The recommended engineering approach is to **map treatment+rate to the "hoog/laag/zero" bands**, while **the actual rate values remain in our VAT data-driven dictionary table** (consistent with red line 12 "do not hard-code tax rates as enums"). Also note that **1c (overige tarieven, e.g. 13% canteen forfait)** falls outside the 21/9/0 three bands — see question ⑥.

**② Does EU_B2C really go into 1a/1b at NL rates?**
**⚠️ Conditional.** The PDF operates on a "threshold" basis: **cross-border distance sales + digital services to EU private consumers, cumulative last year/this year ≤ €10,000** → Dutch rules and Dutch rates apply (goes into 1a/1b). **Once > €10,000** → tax base falls in the customer's/destination country; with OSS → not in the Dutch return; without OSS → net amount goes into **3c**, btw payable in destination country (pp. 14, 26, 38). **We do not implement OSS in v1**, so **a cumulative €10,000 threshold monitor for EU_B2C distance sales is mandatory**: below → 1a/1b; approaching/exceeded → either file 3c + foreign registration, or enable OSS (which we haven't done). **Note**: The threshold only applies to "distance sales of goods + digital services to private individuals"; ordinary "customer purchases on-site in the Netherlands" are domestic 1a/1b regardless.

**③ Does import go into 4a for self-assessment or is Douane collecting at the border (not in the return)?**
**⚠️ Depends on art.23.** **With vergunning artikel 23** → **4a self-assessment + 5b offset**, net zero (p. 14). **Without art.23** → import VAT collected by **Douane at the aangifte ten invoer**, **does not go into 4a**; claim as voorbelasting in 5b using customs documents. For the specific raw materials designated by law, the import reverse-charge is mandatory and art.23 must be held. **Engineering-wise, IMPORT_NON_EU needs a boolean flag "holds art.23?" to branch**.

**④ Does EU_B2C_PURCH go into the NL return?**
**✅ No.** Pp. 16/40 Let op! are explicit: btw paid in other EU countries **may not** be deducted in the Dutch return (the EU refund procedure must be used). Foreign VAT paid as a consumer is neither input tax (no business invoice/reverse-charge) nor entered in any Dutch box. **Our decision to map this row to "no box" is the correct official position**.

**⑤ Does the 5a total include the self-assessed VAT from 2a/4a/4b?**
**The position is as follows (PDF does not give the 5a formula directly, but the logic is self-consistent)**:
- "Total output side" = **btw amounts from 1a + 1b + 1c + 1d + self-assessed btw from 2a + 4a + 4b** (the rechterkolom of 2a/4a/4b is "verschuldigde btw"; pp. 12/14/25/27 explicitly state it must be reported as output tax due). **Therefore the "total output" does include the self-assessed VAT from 2a/4a/4b.**
- "Input side" = **5b (comprising genuine purchase input tax + deductible portions of 2a/4a/4b self-assessments)**.
- **Net payable/refundable = total output − 5b**, calculated automatically by the system (pp. 16/28/40).
- **However, the toelichting does not name 5a/5c** (see §2 opening). Conclusion: **If we wish to display 5a, it should be defined as "sum of output btw from 1a/1b/1c/1d + self-assessed btw from 2a + 4a + 4b"; 5c = 5a − 5b**. This is an engineering display definition — **please ask the accountant to confirm it matches the 5a/5c labels in the return UI**.

**⑥ Can 1c / 1d / 3c safely be set to zero in v1?**
- **1c (overige tarieven, behalve 0%)**: The only scenario named in the PDF is "sportkantine opting for the 13% forfait" (p. 11). **Essentially never applicable to an ordinary ZZP/freelancer** → **can be set to 0 in v1** (but keep the box; do not remove it). ⚠️ The author should confirm there is no "other rate" turnover in their business.
- **1d (privégebruik)**: **Cannot simply be set to 0** — whenever there is **private use of business assets (vehicle, gas/water/elektra/telefoon, withdrawal of goods for private use, free services to oneself/family/associates, etc.)**, 1d must be completed in the last return of the year (pp. 11, 35–36). **If the author has private use of a business vehicle or any such mixed situation, 1d is mandatory**; it can only be zero if there is absolutely no private use. Recommendation: at least provide a "year-end 1d manual entry" field in v1.
- **3c (installatie/afstandsverkopen)**: Only needed for "installation/assembly in another EU country" or "above-threshold distance sales without OSS" (p. 14). **Pure local ZZP with no EU distance sales → can be 0 in v1**; however, it is linked to question ②: once EU_B2C exceeds €10,000 and OSS is not used, 3c becomes active. **Recommended: default to 0, but triggerable by the threshold monitor**.

---

## 5. Quick reference: which boxes take net amount, which take tax amount

| Box | Net amount (Omzet/base) | Tax amount (Btw) | Notes |
|---|:---:|:---:|---|
| 1a | ✅ | ✅ | hoog (21%) |
| 1b | ✅ | ✅ | laag (9%) |
| 1c | ✅ | ✅ | overige (e.g. 13% canteen) |
| 1d | ❌ | ✅ | Tax amount only; last return of the year only |
| 1e | ✅ | ❌ | 0% / supplier side of reverse-charge |
| 2a | ✅ | ✅ | Recipient self-assessment (→ 5b offset) |
| 3a | ✅ | ❌ | Export non-EU |
| 3b | ✅ | ❌ | ICP (≡ Opgaaf ICP) |
| 3c | ✅ | ❌ | Installation / distance sales |
| 4a | ✅ | ✅ | Non-EU import (art.23) / services self-assessment (→ 5b offset) |
| 4b | ✅ | ✅ | EU purchases / services self-assessment (→ 5b offset) |
| 5b | ❌ | ✅ | Input tax amount (including deductible portions of 2a/4a/4b) |
| 5a/5c | (total) | (total) | Calculated automatically by the system; not individually named in the toelichting |
| 5d/5e | — | — | **PDF not covered** |

---

## 6. ⚠️ Points requiring final confirmation from the author / accountant

1. **5a/5c/5d/5e box numbers**: The 2026 toelichting names only 5b; totals are calculated automatically. Please confirm whether the return UI (Mijn Belastingdienst Zakelijk) still displays 5a/5c labels, and whether 5d/5e have any purpose in 2026 (this document treats them as "not existing / not implemented").
2. **Rate figures (21/9/13/0)**: The PDF only says hoog/laag/overige/0%; it does not endorse specific numbers. We use a data-driven dictionary table per red line 12 — **please confirm that the 2026 current rates hoog=21%, laag=9% are unchanged**.
3. **EU_B2C €10,000 threshold monitoring (question ②)**: We do not implement OSS in v1. **Please confirm whether the author makes any distance sales / provides digital services to EU private consumers**; if so, threshold monitoring + an above-threshold handling path (3c or foreign registration) are required — otherwise the return will be incorrect.
4. **Whether IMPORT_NON_EU holds vergunning artikel 23 (question ③)**: This determines whether the route is 4a self-assessment or Douane collection + 5b. **Please confirm whether the author has applied for art.23**.
5. **Whether 1c / 1d can genuinely be set to zero (question ⑥)**: 1c depends on whether there is any "other rate" turnover (canteen etc.); **1d depends on whether there is any private use of business vehicles or any mixed business/private use** — if so, 1d cannot be zero and year-end supplementary tax is required.
6. **Herzieningsregeling 5-year/10-year/10% thresholds (§3.4)**: These figures are not in this toelichting — they are tax-law detail. **Consult Wet OB / an accountant** before implementing capital-goods adjustment logic.
7. **Specific apportionment algorithm for gemengd / privé (§3.5)**: The PDF only states the principle (belaste:vrijgestelde omzet ratio, or actual use). **The vehicle forfait and specific business% calculation must be looked up on belastingdienst.nl/btw-gebruik / the vehicle hulpmiddel**.
8. **Which services fall within 3b**: The special services listed on p. 14 that are excluded from 3b/ICP (immovable property, passenger transport, admission to cultural/sporting/entertainment events, catering, short-term hire, etc.) have special place-of-supply rules. **If the author will sell such cross-border B2B services, each category must be checked individually** (most do not go into 3b).
9. **Margeregeling, KOR, OSS**: None implemented/enabled in v1. If the author's business reaches any of these situations in the future (second-hand goods margin scheme, annual turnover ≤ €20k and wish to use KOR, EU distance sales), a separate milestone must be established and the relevant sections of this guide revisited.

---

## 7. M16 supplement · Home batteries and external sales originals

> Added on 2026-10-30 from the official web sources below, separately from the original PDF analysis. M16 is planned, not implemented. Earlier milestone acceptance records remain historical.

### 7.1 Home battery under the ordinary VAT regime

The Tax Administration's [home-battery guidance](https://www.belastingdienst.nl/wps/wcm/connect/nl/btw/content/thuisbatterij-btw) makes deduction conditional on qualifying electricity trading, EMS, a dynamic contract, correctly named documents and ordinary VAT status. Purely private use does not qualify. Eligible fully attributed acquisitions can have purchase VAT deducted; electricity-sale remuneration attracts output VAT. The page also notes solar-electricity sales where applicable.

For a fully deducted battery also used privately, determine annual private use using EMS information, optionally with energy statements. The official example allocates acquisition net cost over the in-service calendar year and four following years. Multiply that annual cost by that year's private-use percentage and the acquisition VAT rate. Report private-use base and VAT in Q4 box 1d. No such acquisition adjustment remains from year six; continuing sales remain separately taxable.

Example: €10,000 net cost, €2,100 purchase VAT and 45% private use give annual base `10,000 / 5 × 45% = 900` and VAT `900 × 21% = 189`. This is not an instruction to deduct only one fifth of purchase VAT initially.

### 7.2 Externally issued sales and numbering

Under the [official self-billing rules](https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/btw/administratie_bijhouden/facturen_maken/factuureisen/aangepaste_regels_facturen/factuur_uitgereikt_door_afnemer), prior agreement, correct supplier/customer identities, the self-billing statement and valid invoice content matter; the supplier remains responsible for VAT. The supplier records the document as a sale and can retain the customer's number or assign an internal consecutive number. The general [invoice requirements](https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/btw/administratie_bijhouden/facturen_maken/factuureisen/factuureisen) require consecutive numbering in one or more series and unique invoice numbers; uniqueness alone is not the complete rule.

### 7.3 Application boundary and unresolved-case handling

[M16](../plan/milestones/M16.md) freezes a bounded ordinary-VAT implementation: full initial deduction, separate annual inputs, known subtotals plus explicit missing-data warnings, and a reliable 1d base. It does not generalize M10's current-year percentage adjustment to a battery or infer a base for legacy contributions when it cannot determine one reliably. KOR, partial initial deduction, disposal/special use changes and general capital-goods adjustments remain outside M16.

External sales recognition requires the actual sales evidence and tax breakdown, not a platform's net payout or the net payable on a mixed household energy statement. Excluding an activity from company P/L is a bookkeeping selection, not a ruling on income-tax liability; [income-tax activities are assessed separately, including their relationship](https://www.belastingdienst.nl/wps/wcm/connect/bldcontentnl/belastingdienst/zakelijk/winst/inkomstenbelasting/wanneer_bent_u_ondernemer_voor_de_inkomstenbelasting/).
