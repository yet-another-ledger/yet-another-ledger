import { describe, expect, it } from 'vitest'
import { resolvePurchaseTreatmentSuggestion } from '../../src/composables/usePurchaseExpenseDictionaries'

const suggestion = { id: 'treatment-1', code: 'EU_B2B_REVERSE_PURCH', reason: 'Reverse charge is stated.' }
const eligible = { id: 'treatment-1', code: 'EU_B2B_REVERSE_PURCH', label: 'Reverse', side: 'PURCHASE', effect: 'ZERO_REVERSE', active: true } as const

describe('resolvePurchaseTreatmentSuggestion', () => {
  it('accepts only the current active purchase record with the expected effect and code', () => {
    expect(resolvePurchaseTreatmentSuggestion(suggestion, [eligible as never])).toEqual(eligible)
    expect(resolvePurchaseTreatmentSuggestion({ ...suggestion, id: 'stale-id' }, [eligible as never])).toBeNull()
    expect(resolvePurchaseTreatmentSuggestion({ ...suggestion, code: 'NL_DOMESTIC_PURCH' }, [eligible as never])).toBeNull()
    expect(resolvePurchaseTreatmentSuggestion(suggestion, [{ ...eligible, active: false } as never])).toBeNull()
    expect(resolvePurchaseTreatmentSuggestion(suggestion, [{ ...eligible, side: 'SALES' } as never])).toBeNull()
    expect(resolvePurchaseTreatmentSuggestion(suggestion, [{ ...eligible, effect: 'APPLY_RATE' } as never])).toBeNull()
    expect(resolvePurchaseTreatmentSuggestion({ ...suggestion, code: 'UNSUPPORTED' }, [eligible as never])).toBeNull()
    expect(resolvePurchaseTreatmentSuggestion(null, [eligible as never])).toBeNull()
  })
})
