import React, { useMemo } from 'react'
import { Card, Col, Row, Statistic, Tooltip } from 'antd'
import { useI18n } from '../i18n'

function highEventCount(cal) {
  const ev = (cal && cal.events) || []
  return ev.filter((e) => /high|高|3/i.test(String(e.importance || ''))).length
}

/** At-a-glance cards for essential quant factors. */
export default function FactorsPanel({ data, bare = false }) {
  const { t } = useI18n()
  const cards = useMemo(() => {
    if (!data) return []
    const idx = {}
    for (const f of data.factors || []) idx[`${f.factor}|${f.symbol}`] = f.value

    const v = (k) => idx[k]
    const btcTaker = v('taker_flow|BTCUSDT')?.latest
    const liq = v('liq_agg|BTCUSDT')
    const orders = v('liq_orders|BTCUSDT')
    const wall = v('ob_wall|BTCUSDT')
    const cvd = v('cvd|BTCUSDT')?.latest
    const basis = (v('mark_all|') || {})['BTCUSDT']?.basis_pct
    const fg = v('fear_greed|')?.latest
    const cal = v('econ_cal|')

    const items = [
      { t: t('fp.taker'), val: btcTaker, fmt: (x) => x.toFixed(2),
        color: btcTaker > 1.1 ? '#2F8A52' : btcTaker < 0.9 ? '#C24B45' : undefined,
        tip: t('fp.takerTip') },
      { t: t('fp.basis'), val: basis, fmt: (x) => `${x.toFixed(3)}%`, tip: t('fp.basisTip') },
      { t: t('fp.liq'), val: liq,
        fmt: (x) => `${(x.long_mult || 0).toFixed(1)}x / ${(x.short_mult || 0).toFixed(1)}x`,
        tip: t('fp.liqTip') },
      { t: t('fp.liqOrders'), val: orders,
        fmt: (x) => `${x.n_10m || 0} / ${x.n_prev_20m || 0}`,
        tip: t('fp.liqOrdersTip') },
      { t: t('fp.obWall'), val: wall,
        fmt: (x) => `${(x.orders || []).length}`,
        tip: t('fp.obWallTip') },
      { t: t('fp.cvd'), val: cvd, fmt: (x) => Number(x).toFixed(1), tip: t('fp.cvdTip') },
      { t: t('fp.fg'), val: fg, fmt: (x) => x.toFixed(0),
        color: fg >= 85 || fg <= 15 ? '#C24B45' : undefined, tip: t('fp.fgTip') },
      { t: t('fp.econ'), val: cal,
        fmt: (x) => `${highEventCount(x)} high`,
        tip: t('fp.econTip') },
    ]
    return items.filter((i) => i.val !== undefined && i.val !== null)
  }, [data, t])

  const grid = (
    <Row gutter={[10, 10]}>
      {cards.map((c) => (
        <Col xs={12} md={8} lg={bare ? 4 : 12} key={c.t}>
          <Tooltip title={c.tip}>
            <Card size="small" style={{ background: '#FFFFFF' }}>
              <Statistic title={<span style={{ fontSize: 12 }}>{c.t}</span>}
                         value={c.fmt(c.val)}
                         valueStyle={{ fontSize: 16, color: c.color }} />
            </Card>
          </Tooltip>
        </Col>
      ))}
    </Row>
  )
  if (bare) return grid
  return <Card size="small" title={t('fp.title')}>{grid}</Card>
}
