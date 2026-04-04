/**
 * SessionDetailModal
 * Full-screen overlay showing a multi-trace line chart of meter values
 * for a single charging transaction: Power, Amps/Power Offered, SoC, Energy, HVB Voltage.
 */

import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'
import { X, Zap, Clock, Battery, TrendingUp } from 'lucide-react'
import { ChargingSession, fetchSessionDetail, MeterValuePoint } from '../lib/api'

interface Props {
  session: ChargingSession
  onClose: () => void
}

// ── Colour palette (matches v2) ───────────────────────────────────────────────
const C = {
  power:    '#3b82f6',   // blue
  offered:  '#a855f7',   // purple  (amps offered / power offered)
  soc:      '#ef4444',   // red
  energy:   '#22c55e',   // green
  voltage:  '#f59e0b',   // amber
}

// ── Custom tooltip ────────────────────────────────────────────────────────────
interface TooltipProps {
  active?: boolean
  payload?: Array<{ name: string; value: number | null; color: string }>
  label?: string
  hasCurrent: boolean
  hasPowerOffered: boolean
}

function ChartTooltip({ active, payload, label, hasCurrent, hasPowerOffered }: TooltipProps) {
  if (!active || !payload?.length) return null

  const fmt = (name: string, val: number | null) => {
    if (val == null) return null
    if (name === 'Power (kW)')           return `${val.toFixed(2)} kW`
    if (name === 'Amps Offered (A)')     return `${val.toFixed(0)} A`
    if (name === 'Power Offered (kW)')   return `${val.toFixed(2)} kW`
    if (name === 'SoC (%)')              return `${val.toFixed(0)} %`
    if (name === 'Energy (kWh)')         return `${val.toFixed(3)} kWh`
    if (name === 'HVB (V)')              return `${val.toFixed(0)} V`
    return String(val)
  }

  return (
    <div className="bg-gray-900/95 border border-gray-700 rounded-lg px-3 py-2 text-xs shadow-xl">
      <p className="text-gray-400 font-mono mb-1.5">{label?.slice(-5)}</p>
      {payload.map(p => {
        const text = fmt(p.name, p.value)
        if (text == null) return null
        return (
          <p key={p.name} className="flex justify-between gap-4 font-medium" style={{ color: p.color }}>
            <span>{p.name}</span>
            <span className="font-mono">{text}</span>
          </p>
        )
      })}
    </div>
  )
}

// ── Stat pill ─────────────────────────────────────────────────────────────────
function Stat({ icon, label, value }: { icon: React.ReactNode; label: string; value: string }) {
  return (
    <div className="flex items-center gap-2 bg-gray-50 rounded-lg px-3 py-2">
      <span className="text-gray-400">{icon}</span>
      <div>
        <p className="text-xs text-gray-400 leading-none">{label}</p>
        <p className="text-sm font-semibold text-gray-800 mt-0.5">{value}</p>
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export function SessionDetailModal({ session: s, onClose }: Props) {
  // Close on Escape key
  useEffect(() => {
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const { data, isLoading, isError } = useQuery({
    queryKey: ['session-detail', s.station_id, s.transaction_id, s.connector_id],
    queryFn: () => fetchSessionDetail({
      station_id:     s.station_id,
      transaction_id: s.transaction_id,
      connector_id:   s.connector_id,
    }),
    staleTime: 5 * 60_000,
  })

  const points: MeterValuePoint[] = data?.points ?? []

  // Detect which optional series have data
  const hasCurrent      = points.some(p => p.current_offered_a  != null)
  const hasPowerOffered = points.some(p => p.power_offered_kw   != null)
  const hasSoc          = points.some(p => p.soc                != null)
  const hasVoltage      = points.some(p => p.voltage_v          != null)
  const hasEnergy       = points.some(p => p.energy_kwh_delta   != null)

  // Summary stats
  const maxPower   = points.reduce((m, p) => Math.max(m, p.power_kw      ?? 0), 0)
  const totalEnergy = points.length
    ? (points[points.length - 1].energy_kwh_delta ?? 0)
    : (s.energy_kwh ?? 0)
  const socStart = points.find(p => p.soc != null)?.soc
  const socEnd   = [...points].reverse().find(p => p.soc != null)?.soc

  // X-axis label — just HH:MM
  const xTick = (v: string) => v.slice(-5)

  return (
    /* Backdrop */
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      {/* Card */}
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-5xl max-h-[90vh] flex flex-col overflow-hidden">

        {/* Header */}
        <div className="flex items-start justify-between px-6 pt-5 pb-3 border-b border-gray-100">
          <div>
            <h2 className="text-base font-semibold text-gray-900">
              Charge Session Details — {s.evse_name}
            </h2>
            <p className="text-xs text-gray-500 mt-0.5 font-mono">
              {s.start_dt} → {s.end_dt ?? '(active)'}
              {s.id_tag && <span className="ml-3 text-gray-400">VID: {s.id_tag}</span>}
            </p>
          </div>
          <button
            onClick={onClose}
            className="ml-4 p-1.5 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        {/* Stats row */}
        <div className="flex flex-wrap gap-2 px-6 py-3 border-b border-gray-100 bg-gray-50/50">
          <Stat icon={<Zap size={14} />}      label="Peak Power"  value={`${maxPower.toFixed(1)} kW`} />
          <Stat icon={<TrendingUp size={14} />} label="Energy"    value={`${(totalEnergy ?? 0).toFixed(3)} kWh`} />
          {s.duration_min != null && (
            <Stat icon={<Clock size={14} />}  label="Duration"    value={`${s.duration_min} min`} />
          )}
          {socStart != null && socEnd != null && (
            <Stat icon={<Battery size={14} />} label="SoC"        value={`${socStart.toFixed(0)} → ${socEnd.toFixed(0)} %`} />
          )}
          {s.est_revenue_usd != null && (
            <Stat icon={<span className="text-xs font-bold">$</span>} label="Est. Revenue" value={`$${s.est_revenue_usd.toFixed(2)}`} />
          )}
        </div>

        {/* Chart area */}
        <div className="flex-1 overflow-auto px-6 py-4 min-h-0">
          {isLoading && (
            <div className="flex items-center justify-center h-64 text-gray-400 text-sm">
              Loading meter values…
            </div>
          )}
          {isError && (
            <div className="flex items-center justify-center h-64 text-red-400 text-sm">
              ⚠ Failed to load meter values
            </div>
          )}
          {!isLoading && !isError && points.length === 0 && (
            <div className="flex items-center justify-center h-64 text-gray-400 text-sm">
              No meter values recorded for this session
            </div>
          )}
          {!isLoading && !isError && points.length > 0 && (
            <ResponsiveContainer width="100%" height={360}>
              <LineChart data={points} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#f0f0f0" />

                {/* Time axis */}
                <XAxis
                  dataKey="ts_ak"
                  tickFormatter={xTick}
                  tick={{ fontSize: 11, fill: '#9ca3af' }}
                  tickLine={false}
                  axisLine={false}
                  minTickGap={40}
                />

                {/* Left axis: Power kW (also used by energy kWh — similar scale) */}
                <YAxis
                  yAxisId="power"
                  orientation="left"
                  tick={{ fontSize: 11, fill: '#9ca3af' }}
                  tickLine={false}
                  axisLine={false}
                  width={40}
                  tickFormatter={v => `${v}`}
                  label={{ value: 'kW / kWh', angle: -90, position: 'insideLeft', offset: 10, style: { fontSize: 10, fill: '#9ca3af' } }}
                />

                {/* Right axis: SoC % fixed 0–100 */}
                {hasSoc && (
                  <YAxis
                    yAxisId="soc"
                    orientation="right"
                    domain={[0, 100]}
                    tick={{ fontSize: 11, fill: '#9ca3af' }}
                    tickLine={false}
                    axisLine={false}
                    width={36}
                    tickFormatter={v => `${v}%`}
                  />
                )}

                {/* Hidden axes for correct visual scaling */}
                {hasCurrent && (
                  <YAxis yAxisId="amps" hide />
                )}
                {hasVoltage && (
                  <YAxis yAxisId="voltage" hide domain={['auto', 'auto']} />
                )}

                <Tooltip
                  content={
                    <ChartTooltip
                      hasCurrent={hasCurrent}
                      hasPowerOffered={hasPowerOffered}
                    />
                  }
                  isAnimationActive={false}
                />
                <Legend
                  wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
                  iconType="line"
                />

                {/* Power (kW) */}
                <Line
                  yAxisId="power"
                  dataKey="power_kw"
                  name="Power (kW)"
                  stroke={C.power}
                  strokeWidth={2}
                  dot={false}
                  activeDot={{ r: 3 }}
                  connectNulls
                  isAnimationActive={false}
                />

                {/* Amps Offered (Tritium) */}
                {hasCurrent && (
                  <Line
                    yAxisId="amps"
                    dataKey="current_offered_a"
                    name="Amps Offered (A)"
                    stroke={C.offered}
                    strokeWidth={1.5}
                    strokeDasharray="6 3"
                    dot={false}
                    activeDot={{ r: 3 }}
                    connectNulls
                    isAnimationActive={false}
                  />
                )}

                {/* Power Offered (Autel) */}
                {hasPowerOffered && (
                  <Line
                    yAxisId="power"
                    dataKey="power_offered_kw"
                    name="Power Offered (kW)"
                    stroke={C.offered}
                    strokeWidth={1.5}
                    strokeDasharray="6 3"
                    dot={false}
                    activeDot={{ r: 3 }}
                    connectNulls
                    isAnimationActive={false}
                  />
                )}

                {/* SoC % */}
                {hasSoc && (
                  <Line
                    yAxisId="soc"
                    dataKey="soc"
                    name="SoC (%)"
                    stroke={C.soc}
                    strokeWidth={2}
                    dot={false}
                    activeDot={{ r: 3 }}
                    connectNulls
                    isAnimationActive={false}
                  />
                )}

                {/* Energy (kWh delta) */}
                {hasEnergy && (
                  <Line
                    yAxisId="power"
                    dataKey="energy_kwh_delta"
                    name="Energy (kWh)"
                    stroke={C.energy}
                    strokeWidth={2}
                    dot={false}
                    activeDot={{ r: 3 }}
                    connectNulls
                    isAnimationActive={false}
                  />
                )}

                {/* HVB Voltage */}
                {hasVoltage && (
                  <Line
                    yAxisId="voltage"
                    dataKey="voltage_v"
                    name="HVB (V)"
                    stroke={C.voltage}
                    strokeWidth={1.5}
                    dot={false}
                    activeDot={{ r: 3 }}
                    connectNulls
                    isAnimationActive={false}
                  />
                )}
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  )
}
