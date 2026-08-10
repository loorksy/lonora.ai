'use client'

import { useEffect, useRef } from 'react'
import type { Chart as KLineChart } from 'klinecharts'
import type { ChartData } from '../ChartRenderer'
import type { TradingChartData } from '@/lib/types/trading'

const TIMEFRAME_TO_PERIOD: Record<string, { type: 'minute' | 'hour' | 'day' | 'week'; span: number }> = {
  M1: { type: 'minute', span: 1 },
  M5: { type: 'minute', span: 5 },
  M15: { type: 'minute', span: 15 },
  M30: { type: 'minute', span: 30 },
  H1: { type: 'hour', span: 1 },
  H4: { type: 'hour', span: 4 },
  D1: { type: 'day', span: 1 },
  W1: { type: 'week', span: 1 },
}

const ENTRY_COLOR = '#2563eb'
const STOP_LOSS_COLOR = '#dc2626'
const TAKE_PROFIT_COLOR = '#16a34a'
const SUPPORT_RESISTANCE_COLOR = '#9333ea'
const TRENDLINE_COLOR = '#f59e0b'

function priceLineOverlay(value: number, label: string, color: string) {
  return {
    name: 'priceLine',
    points: [{ value }],
    extendData: label,
    styles: {
      line: { color, style: 'dashed' as const },
      text: { color: '#ffffff', backgroundColor: color },
    },
  }
}

export function KLineChartsRenderer({ chart }: { chart: ChartData }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartInstanceRef = useRef<KLineChart | null>(null)

  useEffect(() => {
    let disposed = false
    let klc: typeof import('klinecharts') | null = null

    async function setup() {
      klc = await import('klinecharts')
      if (disposed || !containerRef.current) return

      const data = chart.data as unknown as TradingChartData
      const bars = data?.bars ?? []

      const kChart = klc.init(containerRef.current, {
        styles: {
          candle: {
            bar: {
              upColor: '#16a34a',
              downColor: '#dc2626',
              noChangeColor: '#9ca3af',
              upBorderColor: '#16a34a',
              downBorderColor: '#dc2626',
              noChangeBorderColor: '#9ca3af',
              upWickColor: '#16a34a',
              downWickColor: '#dc2626',
              noChangeWickColor: '#9ca3af',
            },
          },
        },
      })
      if (!kChart || disposed) return
      chartInstanceRef.current = kChart

      const klineData = bars.map((b) => ({
        timestamp: new Date(b.time).getTime(),
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
        volume: b.volume ?? undefined,
      }))

      kChart.setDataLoader({
        getBars: ({ callback }) => {
          callback(klineData, false)
        },
      })
      kChart.setSymbol({ ticker: data?.symbol ?? chart.title ?? 'Symbol' })
      kChart.setPeriod(TIMEFRAME_TO_PERIOD[data?.timeframe ?? 'H1'] ?? { type: 'hour', span: 1 })
      kChart.createIndicator('VOL')

      const overlays = data?.overlays
      if (overlays?.entry != null) {
        kChart.createOverlay(priceLineOverlay(overlays.entry, `Entry ${overlays.entry}`, ENTRY_COLOR))
      }
      if (overlays?.stopLoss != null) {
        kChart.createOverlay(priceLineOverlay(overlays.stopLoss, `SL ${overlays.stopLoss}`, STOP_LOSS_COLOR))
      }
      for (const [i, tp] of (overlays?.takeProfit ?? []).entries()) {
        kChart.createOverlay(priceLineOverlay(tp, `TP${i + 1} ${tp}`, TAKE_PROFIT_COLOR))
      }
      for (const level of overlays?.supportResistance ?? []) {
        kChart.createOverlay({
          name: 'horizontalStraightLine',
          points: [{ value: level }],
          styles: { line: { color: SUPPORT_RESISTANCE_COLOR, style: 'dashed' as const, size: 1 } },
        })
      }
      for (const tl of overlays?.trendlines ?? []) {
        kChart.createOverlay({
          name: 'segment',
          points: [
            { timestamp: new Date(tl.start.time).getTime(), value: tl.start.price },
            { timestamp: new Date(tl.end.time).getTime(), value: tl.end.price },
          ],
          styles: { line: { color: tl.color ?? TRENDLINE_COLOR } },
        })
      }
    }

    setup()

    return () => {
      disposed = true
      if (containerRef.current) {
        klc?.dispose(containerRef.current)
      }
      chartInstanceRef.current = null
    }
  }, [chart])

  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const observer = new ResizeObserver(() => {
      chartInstanceRef.current?.resize()
    })
    observer.observe(container)
    return () => observer.disconnect()
  }, [])

  return <div ref={containerRef} style={{ width: '100%', height: '100%' }} />
}
