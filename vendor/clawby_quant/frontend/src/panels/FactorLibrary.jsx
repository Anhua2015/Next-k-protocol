import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Card, Table, Tag, Switch, InputNumber, message, Typography, Badge, Tabs,
  Input, Select, Space,
} from 'antd'
import FactorsPanel from './FactorsPanel'
import { apiPath } from '../api'
import { useI18n } from '../i18n'

function IntervalEditor({ row, onSave }) {
  const [val, setVal] = React.useState(row.interval)
  React.useEffect(() => { setVal(row.interval) }, [row.interval])
  const commit = () => {
    const v = Math.max(1, Math.round(val || 1))
    if (v !== row.interval) onSave(row.name, v)
  }
  return (
    <InputNumber size="small" min={1} max={86400} value={val} style={{ width: 92 }}
                 addonAfter="s" onChange={setVal} onBlur={commit} onPressEnter={commit} />
  )
}

export default function FactorLibrary({ factorsData }) {
  const { t, lang } = useI18n()
  const [rows, setRows] = useState([])
  const [search, setSearch] = useState('')
  const [srcFilter, setSrcFilter] = useState('')
  const [stateFilter, setStateFilter] = useState('')

  const fmtAge = (s) => {
    if (s == null) return t('factor.ago.now')
    if (s < 60) return `${s}s`
    if (s < 3600) return `${Math.floor(s / 60)}m`
    return `${Math.floor(s / 3600)}h`
  }

  const load = useCallback(() => {
    fetch(apiPath('/api/factor-config')).then((r) => r.json())
      .then((d) => setRows(d.factors || [])).catch(() => {})
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, 15000)
    return () => clearInterval(timer)
  }, [load])

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return rows.filter((r) => {
      if (q && !(`${r.name} ${r.label} ${r.label_en || ''}`.toLowerCase().includes(q))) return false
      if (srcFilter && r.source !== srcFilter) return false
      if (stateFilter === 'on' && !r.enabled) return false
      if (stateFilter === 'off' && r.enabled) return false
      return true
    })
  }, [rows, search, srcFilter, stateFilter])

  const update = async (name, payload) => {
    try {
      await fetch(apiPath(`/api/factor-config/${name}`), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      message.success(`${name} ${t('common.updated')}`)
      load()
    } catch {
      message.error(t('common.updateFailed'))
    }
  }

  const sources = useMemo(
    () => [...new Set(rows.map((r) => r.source).filter(Boolean))],
    [rows],
  )

  const columns = [
    { title: t('factor.col.factor'), key: 'label', width: 230,
      render: (_, r) => (
        <span><b>{lang === 'en' ? (r.label_en || r.label) : r.label}</b>{' '}
          <Typography.Text type="secondary" style={{ fontSize: 11 }}>{r.name}</Typography.Text>
          <Tag color="green" style={{ fontSize: 10, marginLeft: 4 }}>{t('factor.depsTag')}</Tag>
        </span>
      ) },
    { title: t('factor.col.source'), dataIndex: 'source', width: 110,
      render: (v) => <Tag color={String(v || '').includes('Clawby') ? 'purple' : 'gold'} style={{ fontSize: 11 }}>{v}</Tag> },
    { title: t('factor.col.value'), key: 'value', width: 170,
      render: (_, r) => (
        r.value == null
          ? <Typography.Text type="secondary" style={{ fontSize: 11 }}>-</Typography.Text>
          : (
            <span style={{ fontSize: 11.5, fontVariantNumeric: 'tabular-nums' }}>
              {r.value}
              {r.per_symbol && r.value_symbol && (
                <Typography.Text type="secondary" style={{ fontSize: 10, marginLeft: 4 }}>
                  ({r.value_symbol.replace('USDT', '')})
                </Typography.Text>
              )}
            </span>
          )
      ) },
    { title: t('factor.col.interval'), key: 'interval', width: 148,
      render: (_, r) => <IntervalEditor row={r} onSave={(n, v) => update(n, { interval_sec: v })} /> },
    { title: t('factor.col.default'), dataIndex: 'default_interval', width: 66,
      render: (v) => <Typography.Text type="secondary" style={{ fontSize: 11 }}>{v}s</Typography.Text> },
    { title: t('factor.col.coverage'), key: 'cov', width: 66,
      render: (_, r) => `${r.symbols_collected}/${r.symbols_total}` },
    { title: t('factor.col.updated'), key: 'age', width: 92,
      render: (_, r) => (
        <Badge status={r.last_age_sec == null ? 'default' : r.stale ? 'error' : 'success'}
               text={<span style={{ fontSize: 12 }}>{fmtAge(r.last_age_sec)}</span>} />
      ) },
    { title: t('factor.col.enabled'), key: 'enabled', width: 58, align: 'right',
      render: (_, r) => (
        <Switch size="small" checked={r.enabled} onChange={(v) => update(r.name, { enabled: v })} />
      ) },
  ]

  const items = [
    { key: 'manage',
      label: `${t('factor.tab.manage')} (${rows.filter((r) => r.enabled).length}/${rows.length} ${t('factor.enabledCount')})`,
      children: (
        <>
          <Typography.Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 8 }}>
            {t('factor.hintEssentials')}
          </Typography.Text>
          <Space style={{ marginBottom: 10 }} wrap>
            <Input.Search allowClear size="small" style={{ width: 240 }}
                          placeholder={t('factor.search')} value={search}
                          onChange={(e) => setSearch(e.target.value)} />
            <Select size="small" allowClear style={{ width: 140 }}
                    placeholder={t('factor.filter.allSource')}
                    value={srcFilter || undefined}
                    onChange={(v) => setSrcFilter(v || '')}
                    options={sources.map((s) => ({ value: s, label: s }))} />
            <Select size="small" allowClear style={{ width: 120 }}
                    placeholder={t('factor.filter.allStatus')}
                    value={stateFilter || undefined}
                    onChange={(v) => setStateFilter(v || '')}
                    options={[{ value: 'on', label: t('factor.filter.on') },
                              { value: 'off', label: t('factor.filter.off') }]} />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              {t('factor.showing')} {filtered.length}/{rows.length}
            </Typography.Text>
          </Space>
          <Table rowKey="name" dataSource={filtered} columns={columns} size="small"
                 pagination={false} scroll={{ y: 500 }} />
        </>
      ) },
    { key: 'snapshot', label: t('factor.tab.snapshot'),
      children: <FactorsPanel data={factorsData} bare /> },
  ]

  return (
    <Card size="small" title={t('factor.title')}>
      <Tabs items={items} size="small" />
    </Card>
  )
}
