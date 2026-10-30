# SI3 · P/L Mileage Breakdown

> 🌐 **English** · [中文](SI3_zh.md)

> **Status: ↪ Incorporated into M16 on 2026-10-30; implementation has not started.** Retain this file as the original requirement record. The authoritative implementation plan is now [M16](../milestones/M16.md), especially D10 and Step 8; SI3 has no independent implementation schedule. M15 remains paused.

## Background

- Under [M11](../milestones/M11.md), each private-transport business trip creates one Expense projection. Mileage is already included in P/L expenses and deducted from the displayed Profit.
- The author recalls an annual income-tax filing screen with a separate “Business Mileage” input that may calculate a deduction. The exact field, tax year and calculation behaviour remain unverified; this recollection is not a confirmed tax-filing rule.
- The report should make the relevant amounts easy to find separately when preparing a tax return, without requiring the author to reconstruct them manually or risk counting the same deduction twice.

## Requested Improvement

- In the **Profit & Loss** report, show both profit figures for the same selected reporting period:
  - **Profit after mileage expenses**: the current Profit, including the mileage deduction.
  - **Profit before mileage expenses**: profit without deducting mileage expenses.
- Make the **mileage expense amount** separately identifiable so the two profit figures can be reconciled. The intended relationship is: profit before mileage expenses − mileage expenses = profit after mileage expenses.
- Use explicit before/after labels rather than an ambiguous “with/without mileage”. M16 fixes the reporting fields, scope and reconciliation rules.
- **Expense by Category** needs no change for this mileage request alone. M16 separately adds a P/L-inclusion filter for its broader Expense/VAT scope.

## Resolution in M16

- M16 D10 / Step 8 adds mileage, other costs and profit before/after mileage to both P/L totals and monthly/quarterly series. Existing `profit` remains profit after mileage; mileage stays in P/L costs under M11.
- All figures use the same selected period, base currency and backend-rounded cost slices. Identify mileage by `Expense.kind`, not category name. Preserve the before-minus-mileage-equals-after identity and series/total reconciliation.
- The remembered income-tax field remains unverified. M16 deliberately provides no field-specific filing guidance, so verifying that screen is not an implementation dependency and neither profit is labeled automatically suitable for it.
- Implementation, gates and acceptance belong to M16. No current report or accounting behavior changes merely because these plans were recorded; do not implement or accept SI3 a second time.

## References

- [M16 · External Sales Bookkeeping and VAT / P&L Separation](../milestones/M16.md), D10 and Step 8.
- [Roadmap · Small improvements](../roadmap.md#4si-small-improvements-separate-from-milestone-numbering)
- [M11 · Mileage Expenses](../milestones/M11.md), especially D3 and D14.
