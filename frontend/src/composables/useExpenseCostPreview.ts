import { onScopeDispose, ref, watch } from 'vue'
import { get, post } from '../api/http'
import type { components } from '../api/schema'

type Request = components['schemas']['ExpenseCostPreviewRequest']
type Result = components['schemas']['ExpenseCostPreviewResult']

/** Raw proposals and stored-current costs are always calculated by the backend. */
export function useExpenseCostPreview(
  inputs: () => Request | null,
  storedPath: () => string | null,
) {
  const costPreview = ref<Result | null>(null)
  const originalCostPreview = ref<Result | null>(null)
  const costPreviewLoading = ref(false)
  const costPreviewError = ref(false)
  let revision = 0

  function resetCostPreview() {
    revision += 1
    costPreview.value = null
    originalCostPreview.value = null
    costPreviewLoading.value = false
    costPreviewError.value = false
  }

  // Synchronous invalidation also rejects a response arriving before Vue's next render.
  watch(() => JSON.stringify([inputs(), storedPath()]), resetCostPreview, { flush: 'sync' })
  onScopeDispose(resetCostPreview)

  async function previewPlCost() {
    resetCostPreview()
    const request = inputs()
    if (!request) return
    const version = revision
    const path = storedPath()
    costPreviewLoading.value = true
    try {
      const [proposed, current] = await Promise.all([
        post<Result>('/api/v1/expenses/cost-preview', request),
        path ? get<Result>(path) : Promise.resolve(null),
      ])
      if (version !== revision) return
      costPreview.value = proposed
      originalCostPreview.value = current
    } catch {
      if (version === revision) costPreviewError.value = true
    } finally {
      if (version === revision) costPreviewLoading.value = false
    }
  }

  return {
    costPreview, originalCostPreview, costPreviewLoading, costPreviewError,
    previewPlCost, resetCostPreview,
  }
}
