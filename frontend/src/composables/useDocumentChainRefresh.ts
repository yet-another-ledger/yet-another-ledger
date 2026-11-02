import { getCurrentScope, onScopeDispose, ref } from 'vue'
import type { Ref } from 'vue'

export type DocumentChainRefreshSource = 'initial' | 'payment' | 'revision'

/**
 * A small, injectable page behaviour: payment panels emit only after their
 * mutation succeeded, so apply their authoritative aggregate first and then
 * reload the independent document-chain projection.
 */
export function createDocumentChainPaymentChangeHandler<T>(
  refreshAfterPayment: () => Promise<boolean>,
  applyPaymentAggregate?: (aggregate: T) => void,
): (aggregate: T) => Promise<boolean> {
  return async (aggregate: T) => {
    applyPaymentAggregate?.(aggregate)
    return refreshAfterPayment()
  }
}

/**
 * Maintain a backend-authoritative document-chain projection independently
 * from mutations which cause it to change.  A refresh failure deliberately
 * retains the last successful projection: the payment mutation has already
 * committed and must not be presented as failed because this follow-up read
 * was unavailable.
 */
export function useDocumentChainRefresh<T>(loadDocumentChain: () => Promise<T>) {
  const documentChain = ref<T | null>(null) as Ref<T | null>
  const chainRefreshing = ref(false)
  const initialChainError = ref<unknown | null>(null)
  const paymentRefreshError = ref<unknown | null>(null)
  const revisionRefreshError = ref<unknown | null>(null)
  let requestVersion = 0
  let disposed = false

  if (getCurrentScope()) {
    onScopeDispose(() => {
      // Every in-flight result is now stale. Do not update refs after its
      // owning component has unmounted, regardless of success or failure.
      disposed = true
      requestVersion += 1
    })
  }

  async function refreshDocumentChain(source: DocumentChainRefreshSource): Promise<boolean> {
    if (disposed) return false
    const currentRequest = ++requestVersion
    chainRefreshing.value = true
    if (source === 'initial') initialChainError.value = null
    else if (source === 'payment') paymentRefreshError.value = null
    else revisionRefreshError.value = null
    try {
      const chain = await loadDocumentChain()
      if (disposed || currentRequest !== requestVersion) return false
      documentChain.value = chain
      initialChainError.value = null
      paymentRefreshError.value = null
      revisionRefreshError.value = null
      return true
    } catch (error: unknown) {
      if (!disposed && currentRequest === requestVersion) {
        if (source === 'initial') initialChainError.value = error
        else if (source === 'payment') paymentRefreshError.value = error
        else revisionRefreshError.value = error
      }
      return false
    } finally {
      if (!disposed && currentRequest === requestVersion) chainRefreshing.value = false
    }
  }

  function loadInitialDocumentChain(): Promise<boolean> {
    return refreshDocumentChain('initial')
  }

  function refreshAfterPayment(): Promise<boolean> {
    return refreshDocumentChain('payment')
  }

  function refreshAfterRevision(): Promise<boolean> {
    return refreshDocumentChain('revision')
  }

  /** Invalidate a reused page's old owner before its replacement loads. */
  function resetDocumentChain(): void {
    requestVersion += 1
    documentChain.value = null
    chainRefreshing.value = false
    initialChainError.value = null
    paymentRefreshError.value = null
    revisionRefreshError.value = null
  }

  return {
    documentChain,
    chainRefreshing,
    initialChainError,
    paymentRefreshError,
    revisionRefreshError,
    loadInitialDocumentChain,
    refreshAfterPayment,
    refreshAfterRevision,
    resetDocumentChain,
  }
}
