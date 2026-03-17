/**
 * DailyTotalsCharts
 * Side-by-side bar charts: Sessions per Day and Energy Delivered per Day.
 * Both respond to the applied date/EVSE filter.
 */

import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'
import { DailyTotal } from '../lib/api'

interface Props {
  data: DailyTotal[]
  isLoading?: boolean
}

// "2026-03-07" → "Mar 7"
function fmtDate(iso: string): string {
  const [, m, d] = iso.split('-')
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
  return `${months[parseInt(m, 10) - 1]} ${parseInt(d, 10)}`
}

function ChartTooltipCount({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-gray-900/95 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl">
      <p className="text-gray-400 mb-1">{label}</p>
      <p className="text-blue-400 font-semibold">{payload[0].value} sessions</p>
    </div>
  )
}

function ChartTooltipEnergy({ active, payload, label }: any) {
  if (!active || !payload?.length) return null
  return (
    <div className="bg-gray-900/95 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl">
      <p className="text-gray-400 mb-1">{label}</p>
      <p className="text-emerald-400 font-semibold">{Number(payload[0].value).toFixed(1)} kWh</p>
    </div>
  )
}

function SkeletonBar() {
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
      <div className="skeleton h-4 w-36 rounded mb-4" />
      <div className="skeleton h-40 w-full rounded" />
    </div>
  )
}

export function DailyTotalsCharts({ data, isLoading }: Props) {
  if (isLoading) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <SkeletonBar />
        <SkeletonBar />
      </div>
    )
  }

  if (!data.length) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {['Sessions per Day', 'Energy Delivered per Day (kWh)'].map(title => (
          <div key={title} className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
            <h3 className="text-sm font-semibold text-gray-700 mb-4">{title}</h3>
            <div className="flex items-center justify-center h-32 text-gray-400 text-sm">
              No data in range
            </div>
          </div>
        ))}
      </div>
    )
  }

  // Recharts won't auto-format the XAxis date string, so we pre-format
  const chartData = data.map(d => ({ ...d, label: fmtDate(d.date) }))

  const tickStyle = { fontSize: 11, fill: '#9ca3af' }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">

      {/* Sessions per Day */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
        <h3 className="text-sm font-semibold text-gray-700 mb-1">Sessions per Day</h3>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={chartData} margin={{ top: 4, right: 8, bottom: 4, left: -8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" vertical={false} />
            <XAxis
              dataKey="label"
              tick={tickStyle}
              tickLine={false}
              axisLine={false}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={tickStyle}
              tickLine={false}
              axisLine={false}
              allowDecimals={false}
              width={28}
            />
            <Tooltip content={<ChartTooltipCount />} cursor={{ fill: '#f0f9ff' }} />
            <Bar dataKey="count" fill="#3b82f6" radius={[3, 3, 0, 0]} maxBarSize={40} />
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* Energy per Day */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
        <h3 className="text-sm font-semibold text-gray-700 mb-1">Energy Delivered per Day (kWh)</h3>
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={chartData} margin={{ top: 4, right: 8, bottom: 4, left: -8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#f3f4f6" vertical={false} />
            <XAxis
              dataKey="label"
              tick={tickStyle}
              tickLine={false}
              axisLine={false}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={tickStyle}
              tickLine={false}
              axisLine={false}
              width={40}
              tickFormatter={v => v >= 1000 ? `${(v/1000).toFixed(1)}k` : String(v)}
            />
            <Tooltip content={<ChartTooltipEnergy />} cursor={{ fill: '#f0fdf4' }} />
            <Bar dataKey="energy_kwh" fill="#22c55e" radius={[3, 3, 0, 0]} maxBarSize={40} />
          </BarChart>
        </ResponsiveContainer>
      </div>

    </div>
  )
}
