import * as echarts from 'echarts'
import { useEffect, useRef } from 'react'

/**
 * Thin React wrapper around ECharts.
 *
 * Owns a single chart instance: renders into a div, re-applies ``option`` on
 * every change, follows the container via a ResizeObserver, and disposes on
 * unmount. The full ``echarts`` bundle is imported — this is a desktop app, so
 * bundle size is not a concern and it keeps tree-shaking complexity out of the
 * call sites.
 */
export default function EChart({
  option,
  height = 280,
}: {
  option: echarts.EChartsOption
  height?: number
}): JSX.Element {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<echarts.ECharts | null>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const chart = echarts.init(el)
    chartRef.current = chart

    const observer = new ResizeObserver(() => chart.resize())
    observer.observe(el)

    return () => {
      observer.disconnect()
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setOption(option, true)
  }, [option])

  return <div ref={ref} style={{ width: '100%', height }} />
}
