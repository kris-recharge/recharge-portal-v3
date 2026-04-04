/** Connectivity tab — live dashboard (unchanged) + date-filtered reconnect history. */

import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase } from '../lib/supabase'
import { fetchConnectivity, fetchConnectivityHistory } from '../lib/api'
import { StatusBadge } from '../components/StatusBadge'
import { SkeletonTable } from '../components/Skeleton'
import { RefreshCw } from 'lucide-react'

// ── Date helpers (AK) ─────────────────────────────────────────────────────────
function todayAK() {
  return new Date().toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}
function daysAgoAK(n: number) {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d.toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}

// ── ConnectivityTab ───────────────────────────────────────────────────────────
export function ConnectivityTab() {
  const queryClient = useQueryClient()

  // ── Live dashboard state ──────────────────────────────────────────────────
  const { data, isLoading } = useQuery({
    queryKey:        ['connectivity'],
    queryFn:         fetchConnectivity,
    refetchInterval: 30_000,
  })

  // Live: trigger a refetch when any new ocpp_events row arrives
  useEffect(() => {
    const channel = supabase
      .channel('ocpp_events_connectivity')
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'ocpp_events' },
        () => { queryClient.invalidateQueries({ queryKey: ['connectivity'] }) },
      )
      .subscribe()
    return () => { supabase.removeChannel(channel) }
  }, [queryClient])

  const chargers     = data?.chargers ?? []
  const onlineCount  = chargers.filter(c => c.is_online).length
  const offlineCount = chargers.length - onlineCount

  // ── History filter state ──────────────────────────────────────────────────
  const allEvseIds = chargers.map(c => c.station_id)
  const [startDate,      setStartDate]      = useState(daysAgoAK(7))
  const [endDate,        setEndDate]        = useState(todayAK())
  const [selectedEvses,  setSelectedEvses]  = useState<string[]>([])   // [] = all

  const historyParams = {
    start_date: startDate,
    end_date:   endDate,
    station_id: selectedEvses.length > 0 ? selectedEvses : undefined,
  }

  const { data: histData, isLoading: histLoading, refetch: refetchHistory } = useQuery({
    queryKey:  ['connectivity-history', historyParams],
    queryFn:   () => fetchConnectivityHistory(historyParams),
    enabled:   !!startDate && !!endDate,
  })

  const events = histData?.events ?? []
  const total  = histData?.total  ?? 0

  // Per-EVSE reconnect counts for the period
  const reconnectCounts: Record<string, number> = {}
  for (const e of events) {
    reconnectCounts[e.evse_name] = (reconnectCounts[e.evse_name] ?? 0) + 1
  }

  function toggleEvse(sid: string) {
    setSelectedEvses(prev =>
      prev.includes(sid) ? prev.filter(x => x !== sid) : [...prev, sid]
    )
  }

  const inputCls = 'px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'

  return (
    <div className="space-y-4">

      {/* ── Live dashboard (unchanged) ─────────────────────────────────────── */}
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-white rounded-xl border border-gray-200 p-4 text-center">
          <div className="text-2xl font-bold text-gray-900">{chargers.length}</div>
          <div className="text-xs text-gray-500 mt-1">Total Chargers</div>
        </div>
        <div className="bg-white rounded-xl border border-emerald-200 p-4 text-center">
          <div className="text-2xl font-bold text-emerald-600">{onlineCount}</div>
          <div className="text-xs text-gray-500 mt-1">Online</div>
        </div>
        <div className="bg-white rounded-xl border border-red-200 p-4 text-center">
          <div className="text-2xl font-bold text-red-600">{offlineCount}</div>
          <div className="text-xs text-gray-500 mt-1">Offline / Unknown</div>
        </div>
      </div>

      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-700">Charger Connectivity</h2>
          {data && (
            <span className="text-xs text-gray-400">
              As of {new Date(data.as_of_utc).toLocaleTimeString()}
            </span>
          )}
        </div>

        <div className="overflow-x-auto">
          <table className="w-full data-table">
            <thead>
              <tr>
                <th>EVSE</th>
                <th>Location</th>
                <th>Status</th>
                <th>Last Seen (AK)</th>
                <th>Minutes Ago</th>
                <th>Last Action</th>
                <th>Connection ID</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <SkeletonTable rows={5} cols={7} />
              ) : chargers.map(c => (
                <tr key={c.station_id}>
                  <td className="font-medium">{c.evse_name}</td>
                  <td>{c.location}</td>
                  <td>
                    <StatusBadge status={c.is_online ? 'online' : 'offline'} />
                  </td>
                  <td className="font-mono text-xs">{c.last_seen_ak ?? '—'}</td>
                  <td className={`tabular-nums font-medium ${
                    c.minutes_since_last_message == null
                      ? 'text-gray-400'
                      : c.minutes_since_last_message > 20
                        ? 'text-red-600'
                        : 'text-emerald-600'
                  }`}>
                    {c.minutes_since_last_message != null
                      ? `${c.minutes_since_last_message.toFixed(0)} min`
                      : '—'}
                  </td>
                  <td className="text-gray-500">{c.last_action ?? '—'}</td>
                  <td className="font-mono text-xs text-gray-400 truncate max-w-xs">
                    {c.connection_id ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── Connection History ─────────────────────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100">
          <h2 className="text-sm font-semibold text-gray-700 mb-3">Connection History</h2>

          {/* Filters */}
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">Start Date (AK)</label>
              <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} className={inputCls} />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-500 mb-1">End Date (AK)</label>
              <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} className={inputCls} />
            </div>

            {/* EVSE multi-select */}
            {allEvseIds.length > 0 && (
              <div>
                <label className="block text-xs font-medium text-gray-500 mb-1">EVSE</label>
                <div className="flex flex-wrap gap-1.5">
                  {chargers.map(c => (
                    <button
                      key={c.station_id}
                      onClick={() => toggleEvse(c.station_id)}
                      className={`px-2.5 py-1.5 rounded-lg border text-xs font-medium transition-colors ${
                        selectedEvses.includes(c.station_id) || selectedEvses.length === 0
                          ? 'bg-blue-600 text-white border-blue-600'
                          : 'bg-white text-gray-600 border-gray-200 hover:border-blue-300'
                      }`}
                    >
                      {c.evse_name}
                    </button>
                  ))}
                  {selectedEvses.length > 0 && (
                    <button
                      onClick={() => setSelectedEvses([])}
                      className="px-2.5 py-1.5 rounded-lg border border-gray-200 text-xs text-gray-500 hover:border-gray-400"
                    >
                      All
                    </button>
                  )}
                </div>
              </div>
            )}

            <button
              onClick={() => refetchHistory()}
              className="flex items-center gap-1.5 px-3 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 text-sm rounded-lg transition-colors"
            >
              <RefreshCw size={13} /> Refresh
            </button>
          </div>
        </div>

        {/* Summary count cards */}
        {!histLoading && (
          <div className="px-5 py-3 border-b border-gray-100 flex flex-wrap gap-4">
            <div className="text-sm">
              <span className="font-semibold text-gray-800">{total}</span>
              <span className="text-gray-500 ml-1.5">
                reconnect{total !== 1 ? 's' : ''} in period
              </span>
            </div>
            {Object.entries(reconnectCounts).sort((a, b) => b[1] - a[1]).map(([name, count]) => (
              <div key={name} className="text-sm">
                <span className="font-semibold text-gray-800">{count}</span>
                <span className="text-gray-500 ml-1">× {name}</span>
              </div>
            ))}
          </div>
        )}

        {/* History table */}
        <div className="overflow-x-auto">
          <table className="w-full data-table">
            <thead>
              <tr>
                <th>Timestamp (AK)</th>
                <th>EVSE</th>
                <th>Location</th>
                <th>Connector #</th>
                <th>Event</th>
                <th>Connection ID</th>
              </tr>
            </thead>
            <tbody>
              {histLoading ? (
                <SkeletonTable rows={8} cols={6} />
              ) : events.length === 0 ? (
                <tr>
                  <td colSpan={6} className="text-center text-gray-400 py-8 text-sm">
                    No reconnect events in the selected period
                  </td>
                </tr>
              ) : events.map((e, i) => (
                <tr key={i}>
                  <td className="font-mono text-xs">{e.received_at_ak}</td>
                  <td className="font-medium">{e.evse_name}</td>
                  <td>{e.location}</td>
                  <td className="tabular-nums">{e.connector_id ?? '—'}</td>
                  <td>
                    <span className="px-2 py-0.5 bg-blue-50 text-blue-700 text-xs font-medium rounded">
                      {e.event}
                    </span>
                  </td>
                  <td className="font-mono text-xs text-gray-400 truncate max-w-xs">
                    {e.connection_id ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

    </div>
  )
}
