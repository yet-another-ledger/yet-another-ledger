import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent } from 'vue'
import ReportSourcePanel from '../../src/components/reports/ReportSourcePanel.vue'

const routerPush = vi.hoisted(() => vi.fn())
vi.mock('vue-router', () => ({ useRouter: () => ({ push: routerPush }) }))

const props = {
  title: 'Source records', actionLabel: 'Open record', emptyLabel: 'No sources',
  items: [{ id: 'first/id', label: 'A'.repeat(900) }, { id: 'second', label: 'Second source' }],
}

afterEach(() => routerPush.mockClear())

describe('Report source panel with real Naive UI collapse and buttons', () => {
  it('exposes a native focusable toggle and a stable, labelled content relation', async () => {
    const wrapper = mount(ReportSourcePanel, { props, attachTo: document.body })
    try {
      const toggle = wrapper.get('.report-source-panel__toggle')
      expect(toggle.element.tagName).toBe('BUTTON')
      expect((toggle.element as HTMLButtonElement).tabIndex).toBe(0)
      expect(toggle.attributes('aria-expanded')).toBe('false')
      expect(wrapper.find('[role="region"]').exists()).toBe(false)
      ;(toggle.element as HTMLButtonElement).focus()
      expect(document.activeElement).toBe(toggle.element)

      // Native button click is also the browser's Enter/Space activation path.
      await toggle.trigger('click')
      expect(toggle.attributes('aria-expanded')).toBe('true')
      const content = wrapper.get('[role="region"]')
      expect(content.attributes('id')).toBe(toggle.attributes('aria-controls'))
      expect(content.attributes('aria-labelledby')).toBe(toggle.attributes('id'))
      expect(content.isVisible()).toBe(true)
      for (const [index, action] of wrapper.findAll('.report-source-panel__action').entries()) {
        expect(action.isVisible()).toBe(true)
        await action.trigger('click')
        expect(routerPush).toHaveBeenLastCalledWith(index ? '/external-sales/second' : '/external-sales/first%2Fid')
      }
      // Stop propagation prevents the collapse header from toggling a second time.
      await toggle.trigger('click')
      expect(toggle.attributes('aria-expanded')).toBe('false')
      await wrapper.get('.n-collapse-item-arrow').trigger('click')
      expect(toggle.attributes('aria-expanded')).toBe('true')
    } finally {
      wrapper.unmount()
    }
  })

  it('keeps instances independent and replaces current source targets while expanded', async () => {
    const host = mount(defineComponent({
      components: { ReportSourcePanel },
      props: ['items'],
      setup: () => ({ panelProps: props }),
      template: '<ReportSourcePanel v-bind="panelProps" :items="items"/><ReportSourcePanel v-bind="panelProps"/>',
    }), { props: { items: props.items } })
    const panels = host.findAllComponents(ReportSourcePanel)
    try {
      const toggles = panels.map(panel => panel.get('.report-source-panel__toggle'))
      expect(toggles[0]!.attributes('aria-controls')).not.toBe(toggles[1]!.attributes('aria-controls'))
      await toggles[0]!.trigger('click')
      expect(toggles[0]!.attributes('aria-expanded')).toBe('true')
      expect(toggles[1]!.attributes('aria-expanded')).toBe('false')
      await host.setProps({ items: [{ id: 'replacement', label: 'Current period' }] })
      expect(panels[0]!.findAll('.report-source-panel__action')).toHaveLength(1)
      await panels[0]!.get('.report-source-panel__action').trigger('click')
      expect(routerPush).toHaveBeenLastCalledWith('/external-sales/replacement')
      expect(panels[0]!.text()).not.toContain('Second source')
      await host.setProps({ items: [] })
      expect(panels[0]!.get('[role="region"]').isVisible()).toBe(true)
      expect(panels[0]!.text()).toContain('No sources')
      expect(panels[0]!.findAll('.report-source-panel__action')).toHaveLength(0)
    } finally {
      host.unmount()
    }
  })
})
