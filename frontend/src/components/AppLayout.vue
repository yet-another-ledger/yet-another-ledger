<script setup lang="ts">
/**
 * Shared application layout.
 *
 * Desktop (≥ 992 px):
 *   - Persistent left sidebar (n-layout-sider, 220 px) with brand + vertical
 *     n-menu navigation.
 *   - Right section: a slim top bar (header-right actions only) + n-layout-content
 *     with the single shared <router-view />.
 *
 * Mobile (< 992 px):
 *   - No sidebar; top bar shows a hamburger button on the left.
 *   - Clicking the hamburger opens an n-drawer (placement="left") that contains
 *     the same vertical n-menu.  Selecting a nav item closes the drawer.
 */
import { computed, h, onMounted, ref, watch, type Component } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useCompanyStore } from '../stores/company'
import { useI18n } from 'vue-i18n'
import { useSettingsPanel } from '../composables/useSettingsPanel'
import { useLocale } from '../composables/useLocale'
import { useBreakpoint } from '../composables/useBreakpoint'
import {
  SettingsOutline,
  BusinessOutline,
  LibraryOutline,
  GlobeOutline,
  LogOutOutline,
  PeopleOutline,
  GridOutline,
  CubeOutline,
  DocumentTextOutline,
  ReceiptOutline,
  AlbumsOutline,
  CalculatorOutline,
  WalletOutline,
  CardOutline,
  RepeatOutline,
  BarChartOutline,
  MenuOutline,
} from '@vicons/ionicons5'
import { NIcon, type MenuOption } from 'naive-ui'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const companyStore = useCompanyStore()
const { t } = useI18n()
const { open } = useSettingsPanel()
const { currentLocale, setLocale } = useLocale()
const { isDesktop } = useBreakpoint()

onMounted(() => {
  if (!companyStore.company && !companyStore.loading) {
    void companyStore.fetchCompany()
  }
})

/** Controls the mobile drawer open/close state. */
const drawerOpen = ref(false)

function renderIcon(icon: Component) {
  return () => h(NIcon, null, { default: () => h(icon) })
}

const navOptions = computed<MenuOption[]>(() => [
  { label: t('nav.dashboard'), key: 'dashboard', icon: renderIcon(GridOutline) },
  { label: t('nav.customers'), key: 'customer-list', icon: renderIcon(PeopleOutline) },
  { label: t('nav.products'), key: 'product-list', icon: renderIcon(CubeOutline) },
  { label: t('nav.invoices'), key: 'invoice-list', icon: renderIcon(DocumentTextOutline) },
  { label: t('nav.quotes'), key: 'quote-list', icon: renderIcon(ReceiptOutline) },
  { label: t('nav.estimates'), key: 'estimate-list', icon: renderIcon(CalculatorOutline) },
  { label: t('nav.payments'), key: 'payment-list', icon: renderIcon(WalletOutline) },
  { label: t('nav.expenses'), key: 'expense-list', icon: renderIcon(CardOutline) },
  { label: t('nav.receiptInbox'), key: 'receipt-inbox', icon: renderIcon(AlbumsOutline) },
  { label: t('nav.recurring'), key: 'recurring-expense-list', icon: renderIcon(RepeatOutline) },
  {
    label: t('nav.reports'),
    key: 'reports-group',
    icon: renderIcon(BarChartOutline),
    children: [
      { label: t('reports.pl.title'), key: 'report-profit-loss' },
      { label: t('reports.vat.navLink'), key: 'report-vat-return' },
      { label: t('reports.icp.navLink'), key: 'report-icp' },
      { label: t('reports.expenses.navLink'), key: 'report-expenses' },
    ],
  },
])

/** Highlight the active menu item based on the current route name. */
const activeKey = computed<string | null>(() => {
  const name = route.name
  if (typeof name === 'string' && name.startsWith('customer')) return 'customer-list'
  if (typeof name === 'string' && name.startsWith('product')) return 'product-list'
  if (typeof name === 'string' && name.startsWith('invoice')) return 'invoice-list'
  if (typeof name === 'string' && name.startsWith('quote')) return 'quote-list'
  if (typeof name === 'string' && name.startsWith('estimate')) return 'estimate-list'
  if (typeof name === 'string' && name.startsWith('payment')) return 'payment-list'
  if (typeof name === 'string' && name.startsWith('recurring-expense')) return 'recurring-expense-list'
  if (typeof name === 'string' && name.startsWith('expense')) return 'expense-list'
  if (typeof name === 'string' && name.startsWith('receipt-inbox')) return 'receipt-inbox'
  if (name === 'report-profit-loss') return 'report-profit-loss'
  if (name === 'report-vat-return') return 'report-vat-return'
  if (name === 'report-icp') return 'report-icp'
  if (name === 'report-expenses') return 'report-expenses'
  if (name === 'dashboard') return 'dashboard'
  return null
})

/**
 * Writable ref that controls which menu groups are expanded.
 * Initialised with reports-group open when landing on a report route.
 * Users can freely expand/collapse groups; the watch below ensures the
 * reports group is always open while any report route is active.
 */
const expandedKeys = ref<string[]>(
  typeof route.name === 'string' && route.name.startsWith('report-')
    ? ['reports-group']
    : [],
)

function handleUpdateExpandedKeys(keys: string[]) {
  expandedKeys.value = keys
}

/** Auto-expand the reports group when navigating into a report route. */
watch(
  () => route.name,
  (name) => {
    if (typeof name === 'string' && name.startsWith('report-')) {
      if (!expandedKeys.value.includes('reports-group')) {
        expandedKeys.value = [...expandedKeys.value, 'reports-group']
      }
    }
  },
)

function handleNav(key: string) {
  if (key === 'dashboard') void router.push('/dashboard')
  else if (key === 'customer-list') void router.push('/customers')
  else if (key === 'product-list') void router.push('/products')
  else if (key === 'invoice-list') void router.push('/invoices')
  else if (key === 'quote-list') void router.push('/quotes')
  else if (key === 'estimate-list') void router.push('/estimates')
  else if (key === 'payment-list') void router.push('/payments')
  else if (key === 'expense-list') void router.push({ path: '/expenses', query: { tab: 'purchase' } })
  else if (key === 'receipt-inbox') void router.push('/receipt-inbox')
  else if (key === 'recurring-expense-list') void router.push('/recurring-expenses')
  else if (key === 'report-profit-loss') void router.push('/reports/profit-loss')
  else if (key === 'report-vat-return') void router.push('/reports/vat-return')
  else if (key === 'report-icp') void router.push('/reports/icp')
  else if (key === 'report-expenses') void router.push('/reports/expenses')

  // Close the mobile drawer after navigation.
  drawerOpen.value = false
}

function toggleLocale() {
  void setLocale(currentLocale.value === 'en' ? 'zh' : 'en')
}

async function handleLogout() {
  await auth.logout()
  void router.push('/login')
}
</script>

<template>
  <!-- Full-viewport wrapper so the layout always fills the screen. -->
  <div class="app-layout-root">
    <n-layout has-sider style="height: 100vh">
      <!-- ── Desktop sidebar ─────────────────────────────────────── -->
      <n-layout-sider
        v-if="isDesktop"
        bordered
        :width="220"
        class="app-sider"
      >
        <div class="sider-brand" @click="router.push('/dashboard')">
          {{ t('app.title') }}
        </div>
        <n-menu
          :options="navOptions"
          :value="activeKey"
          :expanded-keys="expandedKeys"
          :indent="18"
          @update:value="handleNav"
          @update:expanded-keys="handleUpdateExpandedKeys"
        />
      </n-layout-sider>

      <!-- ── Right section (header + content) ───────────────────── -->
      <n-layout>
        <!-- Top bar (always visible) -->
        <n-layout-header bordered class="app-topbar">
          <!-- Hamburger (mobile only) -->
          <n-button
            v-if="!isDesktop"
            quaternary
            size="small"
            :aria-label="t('nav.openMenu')"
            class="hamburger-btn"
            @click="drawerOpen = true"
          >
            <template #icon>
              <n-icon><MenuOutline /></n-icon>
            </template>
          </n-button>

          <!-- Brand title (mobile only, desktop has it in the sider) -->
          <h2
            v-if="!isDesktop"
            class="topbar-brand"
            @click="router.push('/dashboard')"
          >
            {{ t('app.title') }}
          </h2>

          <!-- Spacer -->
          <div class="topbar-spacer" />

          <!-- Right-side action buttons (always visible) -->
          <div class="topbar-right">
            <n-button
              quaternary
              size="small"
              :title="t('settings.panel.open')"
              @click="open()"
            >
              <template #icon>
                <n-icon><SettingsOutline /></n-icon>
              </template>
            </n-button>
            <n-button
              quaternary
              size="small"
              :title="t('settings.panel.company')"
              @click="router.push('/settings/company')"
            >
              <template #icon>
                <n-icon><BusinessOutline /></n-icon>
              </template>
            </n-button>
            <n-button
              quaternary
              size="small"
              :title="t('nav.contentLibrary')"
              @click="router.push('/content-library')"
            >
              <template #icon>
                <n-icon><LibraryOutline /></n-icon>
              </template>
            </n-button>
            <n-button quaternary size="small" @click="toggleLocale">
              <template #icon>
                <n-icon><GlobeOutline /></n-icon>
              </template>
              {{ currentLocale === 'en' ? '中文' : 'EN' }}
            </n-button>
            <span
              v-if="companyStore.company?.name"
              class="topbar-company-name"
              :title="companyStore.company.name"
            >{{ companyStore.company.name }}</span>
            <n-tag v-if="auth.user" size="small" type="info">
              {{ auth.user.email }}
            </n-tag>
            <n-button quaternary size="small" type="error" @click="handleLogout">
              <template #icon>
                <n-icon><LogOutOutline /></n-icon>
              </template>
            </n-button>
          </div>
        </n-layout-header>

        <!-- Param-only edit navigation must not reuse stale document/customer state.
             route.path includes the id but deliberately excludes query changes. -->
        <n-layout-content class="app-content">
          <router-view v-slot="{ Component, route }">
            <component :is="Component" :key="route.path" />
          </router-view>
        </n-layout-content>
      </n-layout>
    </n-layout>

    <!-- ── Mobile drawer ──────────────────────────────────────────── -->
    <n-drawer
      v-model:show="drawerOpen"
      :width="240"
      placement="left"
    >
      <n-drawer-content :title="t('app.title')" closable>
        <n-menu
          :options="navOptions"
          :value="activeKey"
          :expanded-keys="expandedKeys"
          :indent="18"
          @update:value="handleNav"
          @update:expanded-keys="handleUpdateExpandedKeys"
        />
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.app-layout-root {
  height: 100vh;
  overflow: hidden;
}

/* ── Sidebar ── */
.app-sider {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.sider-brand {
  padding: 16px 20px;
  font-size: 16px;
  font-weight: 700;
  cursor: pointer;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  border-bottom: 1px solid var(--n-border-color, #efeff5);
  user-select: none;
}

/* ── Top bar ── */
.app-topbar {
  display: flex;
  align-items: center;
  padding: 0 16px;
  height: 56px;
  flex-shrink: 0;
}

.hamburger-btn {
  flex-shrink: 0;
  margin-right: 8px;
}

.topbar-brand {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  cursor: pointer;
  white-space: nowrap;
  user-select: none;
}

.topbar-spacer {
  flex: 1;
}

.topbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

/* ── Company name chip in top bar ── */
.topbar-company-name {
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 500;
  flex-shrink: 1;
  min-width: 0;
}

/* ── Content ── */
.app-content {
  overflow-y: auto;
  height: calc(100vh - 56px);
}
</style>
