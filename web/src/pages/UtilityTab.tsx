/**
 * Utility Reads tab — v3.2
 *
 * Daily site efficiency per meter: efficiency % = dispensed kWh ÷ metered kWh.
 * One bar chart per meter (site). Scoped server-side to the units the signed-in
 * user can access — a user who can see a unit at a site sees that site's meter.
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { RefreshCw, AlertCircle, Gauge } from 'lucide-react'
import { fetchUtilityEfficiency, type UtilityEfficiencyMeter } from '../lib/api'

// v3.2: single bar colour for the Utility Reads charts.
const BAR_COLOR = '#2563eb'         // blue
const KWH_BAR_COLOR = '#10b981'     // green — metered-only meters (kWh, no efficiency)

function todayAK() {
  return new Date().toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}
function daysAgoAK(n: number) {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d.toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}

function fmtDay(iso: string): string {
  // iso is a plain YYYY-MM-DD — render without timezone shifting.
  const [, m, d] = iso.split('-')
  return `${parseInt(m, 10)}/${parseInt(d, 10)}`
}

function MeterChart({ meter }: { meter: UtilityEfficiencyMeter }) {
  const meteredOnly = meter.metered_only
  const data = meter.days.map(d => ({
    ...d,
    label: fmtDay(d.date),
    // recharts needs a numeric value to plot; null renders as a gap.
    eff: d.efficiency_pct,
  }))

  const withData = meter.days.filter(d => d.efficiency_pct !== null)
  const avg = withData.length
    ? Math.round(withData.reduce((s, d) => s + (d.efficiency_pct ?? 0), 0) / withData.length)
    : null
  const totalKwh = meter.days.reduce((s, d) => s + d.metered_kwh, 0)

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900">
            {meter.site_name ?? meter.display_name}
            {meter.unit_name && <span className="text-gray-400 font-normal"> · {meter.unit_name}</span>}
            {meteredOnly && <span className="text-gray-400 font-normal"> · metered only</span>}
          </h3>
          <p className="text-xs text-gray-400 mt-0.5">
            {meter.display_name || meter.utility} · acct {meter.account_number}
          </p>
        </div>
        <div className="text-right">
          <div className="text-2xl font-bold text-gray-800 leading-none">
            {meteredOnly
              ? `${Math.round(totalKwh).toLocaleString()} kWh`
              : avg !== null ? `${avg}%` : '—'}
          </div>
          <div className="text-[11px] text-gray-400 mt-0.5">
            {meteredOnly ? 'total metered' : 'avg efficiency'}
          </div>
        </div>
      </div>

      <ResponsiveContainer width="100%" height={200}>
        <BarChart data={data} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" vertical={false} />
          <XAxis dataKey="label" tick={{ fontSize: 10, fill: '#9ca3af' }} interval="preserveStartEnd" />
          <YAxis tick={{ fontSize: 10, fill: '#9ca3af' }} unit={meteredOnly ? '' : '%'} />
          <Tooltip
            formatter={(_v, _n, p) => {
              const d = p.payload as typeof data[number]
              return meteredOnly
                ? [`${d.metered_kwh} kWh metered`, 'Usage']
                : [
                    `${d.eff ?? '—'}%  (${d.dispensed_kwh} / ${d.metered_kwh} kWh)`,
                    'Efficiency',
                  ]
            }}
            labelFormatter={(l) => `Day ${l}`}
          />
          <Bar
            dataKey={meteredOnly ? 'metered_kwh' : 'eff'}
            radius={[2, 2, 0, 0]}
            fill={meteredOnly ? KWH_BAR_COLOR : BAR_COLOR}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

export function UtilityTab() {
  const [startDate, setStartDate] = useState(daysAgoAK(13))   // 14-day default
  const [endDate,   setEndDate]   = useState(todayAK())

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['utility-efficiency', startDate, endDate],
    queryFn: () => fetchUtilityEfficiency(startDate, endDate),
    refetchInterval: 300_000,
  })

  const meters = data?.meters ?? []

  return (
    <div className="space-y-4">
      {/* Header + date range */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
            <Gauge size={16} className="text-gray-400" /> Utility Reads — Site Efficiency
          </h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Daily efficiency = energy dispensed to vehicles ÷ utility-metered energy.
          </p>
        </div>
        <div className="flex items-end gap-2">
          <div>
            <label className="block text-[11px] font-medium text-gray-500 mb-1">Start</label>
            <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)}
              className="px-2 py-1.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
          </div>
          <div>
            <label className="block text-[11px] font-medium text-gray-500 mb-1">End</label>
            <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)}
              className="px-2 py-1.5 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
          </div>
          <button onClick={() => refetch()}
            className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-700 transition-colors pb-2">
            <RefreshCw size={13} className={isFetching ? 'animate-spin' : ''} /> Refresh
          </button>
        </div>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-20 text-sm text-gray-400">
          <RefreshCw size={16} className="animate-spin mr-2" /> Loading meter data…
        </div>
      ) : error ? (
        <div className="flex items-center gap-2 text-red-500 text-sm py-8">
          <AlertCircle size={16} />
          Failed to load utility data.{' '}
          <button onClick={() => refetch()} className="underline">Retry</button>
        </div>
      ) : meters.length === 0 ? (
        <div className="text-center py-16 text-gray-400 text-sm">
          No meter data available for your units in this date range.
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {meters.map(m => <MeterChart key={m.account_number} meter={m} />)}
        </div>
      )}
    </div>
  )
}
