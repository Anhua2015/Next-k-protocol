import React, { useState } from 'react'
import { Layout, Menu, Tag, Space, Typography, Segmented } from 'antd'
import {
  DashboardOutlined, ThunderboltOutlined, FunctionOutlined,
  SafetyOutlined, SettingOutlined, BookOutlined,
} from '@ant-design/icons'
import { api } from './api'
import { usePoll } from './hooks'
import { useI18n } from './i18n'
import { nk } from './theme'
import { ModeSwitch } from './panels/StatusBar'
import Dashboard from './pages/Dashboard'
import StrategiesPage from './pages/StrategiesPage'
import FactorsPage from './pages/FactorsPage'
import RiskPage from './pages/RiskPage'
import ConfigPage from './pages/ConfigPage'
import JournalPage from './pages/JournalPage'

const { Header, Sider, Content } = Layout

const PAGES = {
  dashboard: { key: 'menu.dashboard', icon: <DashboardOutlined />, comp: Dashboard },
  strategies: { key: 'menu.strategies', icon: <ThunderboltOutlined />, comp: StrategiesPage },
  factors: { key: 'menu.factors', icon: <FunctionOutlined />, comp: FactorsPage },
  journal: { key: 'menu.journal', icon: <BookOutlined />, comp: JournalPage },
  risk: { key: 'menu.risk', icon: <SafetyOutlined />, comp: RiskPage },
  config: { key: 'menu.config', icon: <SettingOutlined />, comp: ConfigPage },
}

export default function App() {
  const [page, setPage] = useState(() => {
    const q = new URLSearchParams(window.location.search).get('page')
    return PAGES[q] ? q : 'dashboard'
  })
  const [collapsed, setCollapsed] = useState(false)
  const [status] = usePoll(api.status, 5000)
  const { t, lang, setLang } = useI18n()

  const manageAgeMs = status?.last_manage_ms ? status.now_ms - status.last_manage_ms : null
  const engineLabel = manageAgeMs == null ? '-'
    : manageAgeMs < 2000 ? `${(manageAgeMs / 1000).toFixed(1)}s`
    : `${Math.round(manageAgeMs / 1000)}s`

  const PageComp = PAGES[page].comp

  return (
    <Layout style={{ minHeight: '100vh', background: 'transparent' }}>
      <Sider
        collapsible
        collapsed={collapsed}
        onCollapse={setCollapsed}
        theme="light"
        width={200}
        style={{ background: nk.surface, borderRight: `1px solid ${nk.border}` }}
      >
        <div
          className="nk-brand"
          style={{
            height: 52, margin: '8px 12px', display: 'flex', alignItems: 'center',
            gap: 8, fontWeight: 700, fontSize: 15, paddingLeft: 4, color: nk.text,
          }}
        >
          <ThunderboltOutlined style={{ fontSize: 18, color: nk.gold }} />
          {!collapsed && (
            <span>
              Next <span className="accent">K</span>
              <span style={{ display: 'block', fontSize: 10, fontWeight: 500, color: nk.muted, fontFamily: 'Source Sans 3, sans-serif' }}>
                Quant
              </span>
            </span>
          )}
        </div>
        <Menu
          theme="light"
          mode="inline"
          selectedKeys={[page]}
          style={{ background: 'transparent', borderInlineEnd: 0 }}
          onClick={(e) => setPage(e.key)}
          items={Object.entries(PAGES).map(([k, v]) => ({ key: k, icon: v.icon, label: t(v.key) }))}
        />
      </Sider>

      <Layout style={{ background: 'transparent' }}>
        <Header
          style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            background: 'rgba(255,255,255,0.88)', backdropFilter: 'blur(12px)',
            borderBottom: `1px solid ${nk.border}`, paddingInline: 20,
          }}
        >
          <Space size="middle" wrap>
            <Typography.Title
              level={4}
              style={{ margin: 0, fontFamily: '"Noto Serif SC", Georgia, serif', fontSize: 18 }}
            >
              {t(PAGES[page].key)}
            </Typography.Title>
            {status && (
              <Space size="small" wrap>
                <Tag color={status.mode === 'live' ? 'error' : 'processing'}>
                  {status.mode === 'live' ? t('hdr.live') : t('hdr.paper')}
                </Tag>
                {status.halted && <Tag color="error">{t('hdr.halted')}</Tag>}
                {status.event_quiet && <Tag color="warning">{t('hdr.quiet')}</Tag>}
                <Tag color={manageAgeMs != null && manageAgeMs < 5000 ? 'success' : 'warning'}>
                  {t('hdr.engine')} 0.5s×2 · {engineLabel} {t('hdr.ago')}
                </Tag>
                <Tag color={status.ws_symbols > 0 ? 'success' : 'default'}>
                  {t('hdr.px')} {status.ws_symbols} {t('hdr.coins')}
                </Tag>
              </Space>
            )}
          </Space>
          <Space size="middle">
            <Segmented
              size="small"
              value={lang}
              options={[{ label: '中文', value: 'zh' }, { label: 'EN', value: 'en' }]}
              onChange={setLang}
            />
            <ModeSwitch status={status} />
          </Space>
        </Header>

        <Content style={{ padding: 16 }}>
          <PageComp status={status} />
        </Content>
      </Layout>
    </Layout>
  )
}
