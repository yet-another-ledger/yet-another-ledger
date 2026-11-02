/**
 * Pinia store for M10 reporting (P/L, BTW VAT return, ICP, Expenses by category).
 *
 * Follows the same pattern as other stores in this project:
 * - Reactive state (loading, error, result)
 * - Async fetch function that populates state
 * - Exports types re-exported from schema.d.ts
 */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { components } from '../api/schema'
import { ApiError, get } from '../api/http'

type ProfitLossReport = components['schemas']['ProfitLossReport']
type ProfitLossSeriesItem = components['schemas']['ProfitLossSeriesItem']
type VatReturnReport = components['schemas']['VatReturnReport']
type IcpReport = components['schemas']['IcpReport']
type IcpLine = components['schemas']['IcpLine']
type ExpenseReport = components['schemas']['ExpenseReport']
type ExpenseCategoryRow = components['schemas']['ExpenseCategoryRow']
type DashboardSummary = components['schemas']['DashboardSummary']
type DashboardKpi = components['schemas']['DashboardKpi']
type DashboardMonthly = components['schemas']['DashboardMonthly']
type DashboardTopCategory = components['schemas']['DashboardTopCategory']

export type {
  ProfitLossReport,
  ProfitLossSeriesItem,
  VatReturnReport,
  IcpReport,
  IcpLine,
  ExpenseReport,
  ExpenseCategoryRow,
  DashboardSummary,
  DashboardKpi,
  DashboardMonthly,
  DashboardTopCategory,
}

export const useReportsStore = defineStore('reports', () => {
  // P/L report state
  const plReport = ref<ProfitLossReport | null>(null)
  const plLoading = ref(false)
  const plError = ref<string | null>(null)

  // VAT return report state
  const vatReport = ref<VatReturnReport | null>(null)
  const vatLoading = ref(false)
  const vatError = ref<string | null>(null)

  // ICP report state
  const icpReport = ref<IcpReport | null>(null)
  const icpLoading = ref(false)
  const icpError = ref<string | null>(null)

  // Expense report state (step 4)
  const expenseReport = ref<ExpenseReport | null>(null)
  const expenseLoading = ref(false)
  const expenseError = ref<string | null>(null)

  // Dashboard state (step 5)
  const dashboardReport = ref<DashboardSummary | null>(null)
  const dashboardLoading = ref(false)
  const dashboardError = ref<string | null>(null)

  /**
   * Fetch the P/L report for the given date range and granularity.
   *
   * @param from  ISO date string (YYYY-MM-DD), inclusive start.
   * @param to    ISO date string (YYYY-MM-DD), inclusive end.
   * @param granularity  'month' or 'quarter'.
   */
  async function fetchProfitLoss(
    from: string,
    to: string,
    granularity: 'month' | 'quarter' = 'month',
  ): Promise<void> {
    plLoading.value = true
    plError.value = null
    try {
      const params = new URLSearchParams({ from, to, granularity })
      plReport.value = await get<ProfitLossReport>(
        `/api/v1/reports/profit-loss?${params.toString()}`,
      )
    } catch (e: unknown) {
      plError.value = e instanceof ApiError ? e.message : String(e)
    } finally {
      plLoading.value = false
    }
  }

  /**
   * Fetch the BTW VAT return report for the given year and quarter.
   *
   * @param year     Calendar year (e.g. 2026).
   * @param quarter  Quarter number 1–4.
   */
  async function fetchVatReturn(year: number, quarter: number): Promise<void> {
    vatLoading.value = true
    vatError.value = null
    try {
      const params = new URLSearchParams({ year: String(year), quarter: String(quarter) })
      vatReport.value = await get<VatReturnReport>(
        `/api/v1/reports/vat-return?${params.toString()}`,
      )
    } catch (e: unknown) {
      vatError.value = e instanceof ApiError ? e.message : String(e)
    } finally {
      vatLoading.value = false
    }
  }

  /**
   * Fetch the ICP (Opgaaf ICP) report for the given year and quarter.
   *
   * @param year     Calendar year (e.g. 2026).
   * @param quarter  Quarter number 1–4.
   */
  async function fetchIcp(year: number, quarter: number): Promise<void> {
    icpLoading.value = true
    icpError.value = null
    try {
      const params = new URLSearchParams({ year: String(year), quarter: String(quarter) })
      icpReport.value = await get<IcpReport>(`/api/v1/reports/icp?${params.toString()}`)
    } catch (e: unknown) {
      icpError.value = e instanceof ApiError ? e.message : String(e)
    } finally {
      icpLoading.value = false
    }
  }

  /**
   * Fetch the expense report aggregated by category for the given date range.
   *
   * @param from  ISO date string (YYYY-MM-DD), inclusive start.
   * @param to    ISO date string (YYYY-MM-DD), inclusive end.
   */
  async function fetchExpenseReport(from: string, to: string, includeInPl: boolean | null = null): Promise<void> {
    expenseLoading.value = true
    expenseError.value = null
    try {
      const params = new URLSearchParams({ from, to })
      if (includeInPl !== null) params.set('include_in_pl', String(includeInPl))
      expenseReport.value = await get<ExpenseReport>(
        `/api/v1/reports/expenses?${params.toString()}`,
      )
    } catch (e: unknown) {
      expenseError.value = e instanceof ApiError ? e.message : String(e)
    } finally {
      expenseLoading.value = false
    }
  }

  /**
   * Fetch the dashboard summary for the given year.
   *
   * @param year  Calendar year (e.g. 2026).
   */
  async function fetchDashboard(year: number): Promise<void> {
    dashboardLoading.value = true
    dashboardError.value = null
    try {
      const params = new URLSearchParams({ year: String(year) })
      dashboardReport.value = await get<DashboardSummary>(
        `/api/v1/reports/dashboard?${params.toString()}`,
      )
    } catch (e: unknown) {
      dashboardError.value = e instanceof ApiError ? e.message : String(e)
    } finally {
      dashboardLoading.value = false
    }
  }

  return {
    plReport,
    plLoading,
    plError,
    fetchProfitLoss,
    vatReport,
    vatLoading,
    vatError,
    fetchVatReturn,
    icpReport,
    icpLoading,
    icpError,
    fetchIcp,
    expenseReport,
    expenseLoading,
    expenseError,
    fetchExpenseReport,
    dashboardReport,
    dashboardLoading,
    dashboardError,
    fetchDashboard,
  }
})
