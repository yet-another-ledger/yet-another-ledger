import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createI18n } from 'vue-i18n'
import en from '../../src/locales/en.json'

const mocks = vi.hoisted(() => ({ post: vi.fn() }))
vi.mock('../../src/api/http', () => ({ post: mocks.post }))
vi.mock('naive-ui', async () => {
  const { h } = await import('vue')
  const field = { props: ['label'], template: '<label>{{ label }}<slot /></label>' }
  const input = {
    props: ['value'], emits: ['update:value'],
    template: '<input :value="value" @input="$emit(\'update:value\', $event.target.value)" />',
  }
  return {
    NAlert: { template: '<div><slot /></div>' }, NButton: {
      props: ['disabled', 'loading'], emits: ['click'],
      template: '<button :disabled="disabled || loading" @click="$emit(\'click\')"><slot /></button>',
    },
    NFormItem: field, NInput: input, NSpace: { template: '<div><slot /></div>' },
    NText: { template: '<span><slot /></span>' },
  }
})
import FixedAssetResidualEstimate from '../../src/components/fixed-assets/ResidualEstimate.vue'

const costInputs = {
  net_amount: 100, vat_amount: 21, vat_treatment_id: 'treatment', category_id: 'category',
  deductible: true, pl_cost_basis: 'NET' as const, include_in_pl: true,
  business_percentage: 100, depreciation_years: 1,
}

beforeEach(() => vi.clearAllMocks())
afterEach(() => vi.restoreAllMocks())

describe('fixed asset residual estimate', () => {
  it('blocks implicit form submission from estimate text fields but preserves IME Enter', async () => {
    const wrapper = mount(FixedAssetResidualEstimate, {
      props: { value: 0, costInputs, usefulLifeYears: 5 },
      global: { plugins: [createI18n({ legacy: false, locale: 'en', messages: { en } })] },
    })
    const inputs = wrapper.findAll('input')
    expect(inputs).toHaveLength(5)
    for (const input of inputs) {
      const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
      input.element.dispatchEvent(enter)
      expect(enter.defaultPrevented).toBe(true)
    }
    const composingEnter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true, isComposing: true })
    inputs[4]!.element.dispatchEvent(composingEnter)
    expect(composingEnter.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  it('keeps the estimate explicit and applies only after review', async () => {
    mocks.post.mockResolvedValue({
      status: 'suggested', suggested_residual_value: '45.00', reason: 'Common used value.',
      assumptions: ['Good condition'], acquisition_cost: '100.00', currency_code: 'EUR',
    })
    const wrapper = mount(FixedAssetResidualEstimate, {
      props: { value: 0, costInputs, usefulLifeYears: 5, suggestedName: 'Battery' },
      global: { plugins: [createI18n({ legacy: false, locale: 'en', messages: { en } })] },
    })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(mocks.post).toHaveBeenCalledWith('/api/v1/fixed-assets/residual-estimate', expect.objectContaining({
      cost_inputs: costInputs, product_name: 'Battery', useful_life_years: 5,
    }))
    expect(wrapper.emitted('update:value')).toBeUndefined()
    await wrapper.findAll('button').find(button => button.text() === en.fixedAssets.estimateApply)!.trigger('click')
    expect(wrapper.emitted('update:value')?.[0]).toEqual([45])
    wrapper.unmount()
  })

  it('discards stale replies after input changes and preserves manual residual edits', async () => {
    let resolve!: (value: unknown) => void
    mocks.post.mockReturnValue(new Promise(yes => { resolve = yes }))
    const wrapper = mount(FixedAssetResidualEstimate, {
      props: { value: 12, costInputs, usefulLifeYears: 5 },
      global: { plugins: [createI18n({ legacy: false, locale: 'en', messages: { en } })] },
    })
    const fields = wrapper.findAll('input')
    await fields[0]!.setValue('Tool')
    await wrapper.find('button').trigger('click')
    await wrapper.setProps({ usefulLifeYears: 8 })
    resolve({ status: 'suggested', suggested_residual_value: '90.00', reason: 'Old', assumptions: [], acquisition_cost: '100' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Old')
    expect(wrapper.emitted('update:value')).toBeUndefined()
    wrapper.unmount()
  })

  it('replaces or withdraws AI-seeded identity, but preserves manually edited and manually cleared fields', async () => {
    let resolve!: (value: unknown) => void
    mocks.post.mockReturnValue(new Promise(yes => { resolve = yes }))
    const wrapper = mount(FixedAssetResidualEstimate, {
      props: { value: 0, costInputs, usefulLifeYears: 5, suggestionKey: 'rev-1', suggestedName: 'Product A', suggestedBrand: 'Brand A', suggestedModel: 'Model A' },
      global: { plugins: [createI18n({ legacy: false, locale: 'en', messages: { en } })] },
    })
    const fields = wrapper.findAll('input')
    expect((fields[0]!.element as HTMLInputElement).value).toBe('Product A')
    expect((fields[1]!.element as HTMLInputElement).value).toBe('Brand A')
    await wrapper.find('button').trigger('click')
    await wrapper.setProps({ suggestionKey: 'rev-1-reextracted', suggestedName: 'Product A', suggestedBrand: 'Brand A', suggestedModel: 'Model A' })
    resolve({ status: 'suggested', suggested_residual_value: '9.00', reason: 'Previous extraction', assumptions: [], acquisition_cost: '100.00', currency_code: 'EUR' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Previous extraction')
    await fields[0]!.setValue('')
    await fields[1]!.setValue('Owner brand')
    await wrapper.setProps({ suggestionKey: 'rev-2', suggestedName: 'Product B', suggestedBrand: 'Brand B', suggestedModel: null })
    await flushPromises()
    expect((fields[0]!.element as HTMLInputElement).value).toBe('')
    expect((fields[1]!.element as HTMLInputElement).value).toBe('Owner brand')
    expect((fields[2]!.element as HTMLInputElement).value).toBe('')
    await wrapper.setProps({ suggestionKey: 'rev-3', suggestedName: null, suggestedBrand: null, suggestedModel: null })
    expect((fields[0]!.element as HTMLInputElement).value).toBe('')
    expect((fields[1]!.element as HTMLInputElement).value).toBe('Owner brand')
    wrapper.unmount()
  })

  it('invalidates late saved-purchase estimates when authoritative cost inputs change', async () => {
    let resolve!: (value: unknown) => void
    mocks.post.mockReturnValue(new Promise(yes => { resolve = yes }))
    const wrapper = mount(FixedAssetResidualEstimate, {
      props: { value: 12, expenseId: 'purchase-1', costInputs, usefulLifeYears: 5 },
      global: { plugins: [createI18n({ legacy: false, locale: 'en', messages: { en } })] },
    })
    await wrapper.find('input').setValue('Equipment')
    await wrapper.find('button').trigger('click')
    const updatedCost = { ...costInputs, net_amount: 1000 }
    await wrapper.setProps({ costInputs: updatedCost })
    resolve({ status: 'suggested', suggested_residual_value: '5.00', reason: 'Old cost estimate', assumptions: [], acquisition_cost: '100', currency_code: 'EUR' })
    await flushPromises()
    expect(wrapper.text()).not.toContain('Old cost estimate')
    expect(wrapper.emitted('update:value')).toBeUndefined()
    expect((wrapper.props('value'))).toBe(12)
    expect(mocks.post).toHaveBeenCalledWith('/api/v1/fixed-assets/residual-estimate', expect.objectContaining({ expense_id: 'purchase-1', cost_inputs: null }))
    wrapper.unmount()
  })
})
