/**
 * Trading domain chart data model.
 *
 * Kept independent from any rendering library — this is the structured
 * OHLC/overlay shape the backend produces (chart.data on a ChartData row
 * with library: 'klinecharts'), and KLineChartsRenderer.tsx is the only
 * place that translates it into klinecharts' own API shapes.
 */

export interface OHLCBar {
  /** ISO-8601 timestamp */
  time: string
  open: number
  high: number
  low: number
  close: number
  volume?: number | null
}

export interface TrendlinePoint {
  time: string
  price: number
}

export interface Trendline {
  start: TrendlinePoint
  end: TrendlinePoint
  label?: string
  color?: string
}

export interface TradingChartOverlays {
  entry?: number
  stopLoss?: number
  takeProfit?: number[]
  supportResistance?: number[]
  trendlines?: Trendline[]
}

export interface TradingChartData {
  symbol: string
  /** Canonical timeframe: M1 | M5 | M15 | M30 | H1 | H4 | D1 | W1 */
  timeframe: string
  source: 'oanda' | 'metaapi'
  bars: OHLCBar[]
  overlays?: TradingChartOverlays
}
