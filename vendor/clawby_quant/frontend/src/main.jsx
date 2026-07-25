import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider, theme } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import enUS from 'antd/locale/en_US'
import App from './App'
import { I18nProvider, useI18n } from './i18n'
import { nk } from './theme'
import './index.css'

function LocaleShell() {
  const { lang } = useI18n()
  return (
    <ConfigProvider
      locale={lang === 'en' ? enUS : zhCN}
      theme={{
        algorithm: theme.defaultAlgorithm,
        token: {
          colorPrimary: nk.gold,
          colorSuccess: nk.green,
          colorError: nk.red,
          colorWarning: nk.gold,
          colorInfo: nk.blue,
          colorBgBase: nk.bg,
          colorBgContainer: nk.surface,
          colorBorder: nk.border,
          colorText: nk.text,
          colorTextSecondary: '#555B66',
          borderRadius: 3,
          fontFamily: '"Source Sans 3", system-ui, "PingFang SC", "Microsoft YaHei", sans-serif',
          fontSize: 13,
        },
        components: {
          Layout: {
            headerBg: nk.surface,
            bodyBg: 'transparent',
            siderBg: nk.surface,
            triggerBg: nk.surfaceLight,
            triggerColor: nk.text,
          },
          Menu: {
            itemBg: 'transparent',
            itemSelectedBg: 'rgba(166, 124, 61, 0.12)',
            itemSelectedColor: nk.gold,
            itemHoverBg: 'rgba(27, 29, 34, 0.045)',
          },
          Table: {
            headerBg: nk.surfaceLight,
          },
        },
      }}
    >
      <App />
    </ConfigProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <I18nProvider>
    <LocaleShell />
  </I18nProvider>
)
