/**
 * Charging Sessions tab — date-range + EVSE filter, Live mode (rolling 168-hour
 * window, auto-refreshes every 60 s), server-side KPI totals, paginated table
 * with click-to-detail chart, daily bar charts, and session-start density heatmap.
 */

import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase } from '../lib/supabase'
import { fetchSessions, fetchAnalytics, ChargingSession } from '../lib/api'
import { KPICard } from '../components/KPICard'
import { SkeletonKPIRow, SkeletonTable } from '../components/Skeleton'
import { SessionDetailModal } from '../components/SessionDetailModal'
import { DailyTotalsCharts } from '../components/DailyTotalsCharts'
import { SessionDensityHeatmap } from '../components/SessionDensityHeatmap'
import { Zap, Clock, DollarSign, Activity, Filter, X, Radio } from 'lucide-react'

const PAGE_SIZE = 100
const LIVE_HOURS = 168          // rolling window length
const LIVE_REFRESH_MS = 60_000  // auto-refresh interval (60 s)

// ── EVSE options (matches constants.py) ───────────────────────────────────────
const EVSE_OPTIONS = [
  { id: 'as_c8rCuPHDd7sV1ynHBVBiq', name: 'ARG - Right',   location: 'ARG' },
  { id: 'as_cnIGqQ0DoWdFCo7zSrN01', name: 'ARG - Left',    location: 'ARG' },
  { id: 'as_oXoa7HXphUu5riXsSW253', name: 'Delta - Right', location: 'Delta Jct' },
  { id: 'as_xTUHfTKoOvKSfYZhhdlhT', name: 'Delta - Left',  location: 'Delta Jct' },
  { id: 'as_LYHe6mZTRKiFfziSNJFvJ', name: 'Glennallen',    location: 'Glennallen' },
]

// ── AK-timezone date helpers ───────────────────────────────────────────────────
const AK_TZ = 'America/Anchorage'

/** Returns YYYY-MM-DD in AK local time — safe after 3 PM (avoids UTC "tomorrow" bug). */
function toAKDateStr(d: Date): string {
  return d.toLocaleDateString('en-CA', { timeZone: AK_TZ })
}
function defaultEndDate()   { return toAKDateStr(new Date()) }
function defaultStartDate() {
  const d = new Date()
  d.setDate(d.getDate() - 7)
  return toAKDateStr(d)
}

// ── Live-window helpers ────────────────────────────────────────────────────────
/** ISO-8601 UTC string for "now − 168 h", computed fresh at call time. */
function liveStartISO(): string {
  return new Date(Date.now() - LIVE_HOURS * 60 * 60 * 1000).toISOString()
}
/** ISO-8601 UTC string for "now", computed fresh at call time. */
function liveEndISO(): string {
  return new Date().toISOString()
}

// ── Filter state type ─────────────────────────────────────────────────────────
interface Filters {
  startDate:  string    // YYYY-MM-DD for static mode; "" when live
  endDate:    string    // YYYY-MM-DD for static mode; "" when live
  stationIds: string[]  // [] = all allowed
  isLive:     boolean
}

const DEFAULT_FILTERS: Filters = {
  startDate:  defaultStartDate(),
  endDate:    defaultEndDate(),
  stationIds: [],
  isLive:     false,
}

// ─────────────────────────────────────────────────────────────────────────────
export function SessionsTab() {
  const queryClient = useQueryClient()
  const [page, setPage] = useState(1)

  // Draft = UI controls; applied = what actually drives queries
  const [draft,   setDraft]   = useState<Filters>(DEFAULT_FILTERS)
  const [applied, setApplied] = useState<Filters>(DEFAULT_FILTERS)

  const [selectedSession, setSelectedSession] = useState<ChargingSession | null>(null)

  // ── Apply / reset ──────────────────────────────────────────────────────────
  function applyFilters() {
    setPage(1)
    setApplied({ ...draft })
  }

  function clearFilters() {
    const reset: Filters = {
      ...DEFAULT_FILTERS,
      startDate:  defaultStartDate(),
      endDate:    defaultEndDate(),
    }
    setDraft(reset)
    setApplied(reset)
    setPage(1)
  }

  // ── Live toggle ────────────────────────────────────────────────────────────
  function toggleLive() {
    if (draft.isLive) {
      // Turn off → reset to default 7-day static window, preserve EVSE selection
      const off: Filters = {
        startDate:  defaultStartDate(),
        endDate:    defaultEndDate(),
        stationIds: draft.stationIds,
        isLive:     false,
      }
      setDraft(off)
      setApplied(off)
      setPage(1)
    } else {
      // Turn on → apply immediately, preserve EVSE selection
      const on: Filters = {
        startDate:  '',
        endDate:    '',
        stationIds: draft.stationIds,
        isLive:     true,
      }
      setDraft(on)
      setApplied(on)
      setPage(1)
    }
  }

  // ── EVSE pill toggle ───────────────────────────────────────────────────────
  function toggleEvse(id: string) {
    const newIds = draft.stationIds.includes(id)
      ? draft.stationIds.filter(s => s !== id)
      : [...draft.stationIds, id]
    setDraft(prev => ({ ...prev, stationIds: newIds }))
    // In live mode EVSE changes apply immediately (no Apply button needed)
    if (draft.isLive) {
      setApplied(prev => ({ ...prev, stationIds: newIds }))
      setPage(1)
    }
  }

  function selectAllEvse() {
    setDraft(prev => ({ ...prev, stationIds: [] }))
    if (draft.isLive) {
      setApplied(prev => ({ ...prev, stationIds: [] }))
      setPage(1)
    }
  }

  // ── isDirty: only matters in static mode ──────────────────────────────────
  const isDirty =
    !draft.isLive && (
      draft.startDate !== applied.startDate ||
      draft.endDate   !== applied.endDate   ||
      JSON.stringify(draft.stationIds.slice().sort()) !==
        JSON.stringify(applied.stationIds.slice().sort())
    )

  // ── Sessions query ─────────────────────────────────────────────────────────
  const { data, isLoading, isError } = useQuery({
    queryKey: ['sessions', page, applied],
    queryFn: () =>
      fetchSessions({
        page,
        page_size: PAGE_SIZE,
        // Live mode: compute exact rolling window at fetch time
        start_date: applied.isLive ? liveStartISO() : (applied.startDate || undefined),
        end_date:   applied.isLive ? liveEndISO()   : (applied.endDate   || undefined),
        station_id: applied.stationIds.length ? applied.stationIds : undefined,
      }),
    refetchInterval: applied.isLive ? LIVE_REFRESH_MS : false,
    retry: 2,
  })

  // ── Analytics query (daily totals + heatmap) ───────────────────────────────
  const { data: analytics, isLoading: analyticsLoading } = useQuery({
    queryKey: ['analytics', applied],
    queryFn: () =>
      fetchAnalytics({
        start_date: applied.isLive ? liveStartISO() : (applied.startDate || undefined),
        end_date:   applied.isLive ? liveEndISO()   : (applied.endDate   || undefined),
        station_id: applied.stationIds.length ? applied.stationIds : undefined,
      }),
    refetchInterval: applied.isLive ? LIVE_REFRESH_MS : false,
    retry: 2,
  })

  // ── Supabase Realtime — invalidate on new meter value rows ─────────────────
  useEffect(() => {
    const channel = supabase
      .channel('meter_values_realtime')
      .on(
        'postgres_changes',
        { event: 'INSERT', schema: 'public', table: 'meter_values_parsed' },
        () => {
          queryClient.invalidateQueries({ queryKey: ['sessions', 1, applied] })
          queryClient.invalidateQueries({ queryKey: ['analytics', applied] })
        },
      )
      .subscribe()
    return () => { supabase.removeChannel(channel) }
  }, [queryClient, applied])

  const sessions     = data?.sessions          ?? []
  const total        = data?.total             ?? 0
  const totalEnergy  = data?.total_energy_kwh  ?? 0
  const totalRevenue = data?.total_revenue_usd ?? 0
  const avgDuration  = data?.avg_duration_min  ?? null

  const allEvseSelected = draft.stationIds.length === 0

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className="space-y-4">

      {/* ── Filter bar ────────────────────────────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm px-5 py-4">
        <div className="flex flex-wrap items-end gap-4">

          {/* Date range — disabled in Live mode */}
          <div className="flex items-end gap-2">
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-gray-500 uppercase tracking-wide">From</label>
              <input
                type="date"
                value={draft.startDate}
                max={draft.endDate || undefined}
                disabled={draft.isLive}
                onChange={e => setDraft(p => ({ ...p, startDate: e.target.value }))}
                className={`border border-gray-200 rounded-lg px-3 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-opacity ${
                  draft.isLive ? 'opacity-40 cursor-not-allowed bg-gray-50' : ''
                }`}
              />
            </div>
            <span className="text-gray-400 pb-2 text-sm">—</span>
            <div className="flex flex-col gap-1">
              <label className="text-xs font-medium text-gray-500 uppercase tracking-wide">To</label>
              <input
                type="date"
                value={draft.endDate}
                min={draft.startDate || undefined}
                disabled={draft.isLive}
                onChange={e => setDraft(p => ({ ...p, endDate: e.target.value }))}
                className={`border border-gray-200 rounded-lg px-3 py-1.5 text-sm text-gray-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition-opacity ${
                  draft.isLive ? 'opacity-40 cursor-not-allowed bg-gray-50' : ''
                }`}
              />
            </div>
          </div>

          {/* Divider */}
          <div className="hidden sm:block h-8 w-px bg-gray-200 self-end mb-0.5" />

          {/* EVSE toggles */}
          <div className="flex flex-col gap-1">
            <label className="text-xs font-medium text-gray-500 uppercase tracking-wide">EVSE</label>
            <div className="flex flex-wrap gap-1.5">
              <button
                onClick={selectAllEvse}
                className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                  allEvseSelected
                    ? 'bg-blue-600 text-white border-blue-600'
                    : 'bg-white text-gray-600 border-gray-200 hover:border-blue-400'
                }`}
              >
                All
              </button>
              {EVSE_OPTIONS.map(ev => {
                const selected = draft.stationIds.includes(ev.id)
                return (
                  <button
                    key={ev.id}
                    onClick={() => toggleEvse(ev.id)}
                    className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
                      selected
                        ? 'bg-blue-600 text-white border-blue-600'
                        : 'bg-white text-gray-600 border-gray-200 hover:border-blue-400'
                    }`}
                  >
                    {ev.name}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Divider */}
          <div className="hidden sm:block h-8 w-px bg-gray-200 self-end mb-0.5" />

          {/* Live toggle */}
          <div className="flex flex-col gap-1">
            <label className="text-xs font-medium text-gray-500 uppercase tracking-wide">Mode</label>
            <button
              onClick={toggleLive}
              title={draft.isLive
                ? 'Live mode — rolling 168-hour window, refreshes every 60 s. Click to switch to static.'
                : 'Click to enable Live mode (rolling 168-hour window, auto-refresh every 60 s)'}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-sm font-medium transition-all ${
                draft.isLive
                  ? 'bg-emerald-500 text-white border-emerald-500 shadow-sm'
                  : 'bg-white text-gray-600 border-gray-200 hover:border-emerald-400 hover:text-emerald-600'
              }`}
            >
              {draft.isLive
                ? <span className="w-2 h-2 rounded-full bg-white animate-pulse" />
                : <Radio size={13} />
              }
              Live
            </button>
          </div>

          {/* Action buttons — hidden in live mode (EVSE changes auto-apply) */}
          {!draft.isLive && (
            <div className="flex gap-2 ml-auto self-end">
              <button
                onClick={clearFilters}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-200 text-sm text-gray-500 hover:bg-gray-50 transition-colors"
              >
                <X size={13} />
                Reset
              </button>
              <button
                onClick={applyFilters}
                disabled={!isDirty}
                className={`flex items-center gap-1.5 px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                  isDirty
                    ? 'bg-blue-600 text-white hover:bg-blue-700 shadow-sm'
                    : 'bg-gray-100 text-gray-400 cursor-default'
                }`}
              >
                <Filter size={13} />
                Apply
              </button>
            </div>
          )}

          {/* In live mode: show a Reset button to exit live + return to defaults */}
          {draft.isLive && (
            <div className="flex gap-2 ml-auto self-end">
              <button
                onClick={clearFilters}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-gray-200 text-sm text-gray-500 hover:bg-gray-50 transition-colors"
              >
                <X size={13} />
                Exit Live
              </button>
            </div>
          )}
        </div>

        {/* Active filter summary */}
        <div className="mt-3 flex flex-wrap gap-1.5 text-xs text-gray-500">
          {applied.isLive ? (
            <>
              <span className="font-medium text-gray-400">Live:</span>
              <span className="bg-emerald-50 text-emerald-700 px-2 py-0.5 rounded-full flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse" />
                Rolling 168-hour window · refreshes every 60 s
              </span>
              {applied.stationIds.length > 0
                ? applied.stationIds.map(id => {
                    const ev = EVSE_OPTIONS.find(e => e.id === id)
                    return (
                      <span key={id} className="bg-blue-50 text-blue-700 px-2 py-0.5 rounded-full">
                        {ev?.name ?? id}
                      </span>
                    )
                  })
                : <span className="bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">All EVSEs</span>
              }
            </>
          ) : (applied.startDate || applied.endDate || applied.stationIds.length > 0) ? (
            <>
              <span className="font-medium text-gray-400">Showing:</span>
              {applied.startDate && applied.endDate && (
                <span className="bg-blue-50 text-blue-700 px-2 py-0.5 rounded-full">
                  {applied.startDate} → {applied.endDate}
                </span>
              )}
              {applied.stationIds.length > 0
                ? applied.stationIds.map(id => {
                    const ev = EVSE_OPTIONS.find(e => e.id === id)
                    return (
                      <span key={id} className="bg-blue-50 text-blue-700 px-2 py-0.5 rounded-full">
                        {ev?.name ?? id}
                      </span>
                    )
                  })
                : <span className="bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">All EVSEs</span>
              }
            </>
          ) : null}
        </div>
      </div>

      {/* ── KPI row ─────────────────────────────────────────────────────────── */}
      {isLoading ? (
        <SkeletonKPIRow />
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <KPICard
            title="Total Sessions"
            value={total.toLocaleString()}
            icon={<Activity size={16} />}
            color="blue"
            subtitle={applied.stationIds.length ? `${applied.stationIds.length} EVSE` : 'All EVSEs'}
          />
          <KPICard
            title="Energy Delivered"
            value={`${totalEnergy.toFixed(1)} kWh`}
            icon={<Zap size={16} />}
            color="green"
            subtitle="across filtered results"
          />
          <KPICard
            title="Est. Revenue"
            value={`$${totalRevenue.toFixed(2)}`}
            icon={<DollarSign size={16} />}
            color="amber"
            subtitle="across filtered results"
          />
          <KPICard
            title="Avg Duration"
            value={avgDuration != null ? `${avgDuration.toFixed(0)} min` : '—'}
            icon={<Clock size={16} />}
            color="gray"
            subtitle="across filtered results"
          />
        </div>
      )}

      {/* ── Daily bar charts ──────────────────────────────────────────────── */}
      <DailyTotalsCharts
        data={analytics?.daily_totals ?? []}
        isLoading={analyticsLoading}
      />

      {/* ── Session start density heatmap ─────────────────────────────────── */}
      <SessionDensityHeatmap
        data={analytics?.density ?? []}
        isLoading={analyticsLoading}
      />

      {/* ── Sessions table ────────────────────────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-700">
            Charging Sessions
            {total > 0 && (
              <span className="ml-2 text-gray-400 font-normal">
                ({total.toLocaleString()} {applied.isLive ? 'in rolling window' : applied.startDate ? 'in range' : 'total'})
              </span>
            )}
          </h2>
          <div className="flex items-center gap-3">
            <span className="text-xs text-gray-400 italic hidden sm:block">
              Click a row for session detail
            </span>
            {applied.isLive && (
              <span className="text-xs text-emerald-600 font-medium flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse inline-block" />
                Live · 60 s refresh
              </span>
            )}
          </div>
        </div>

        <div className="overflow-x-auto overflow-y-auto max-h-[600px]">
          <table className="w-full data-table">
            <thead className="sticky top-0 z-10 bg-white shadow-[0_1px_0_0_#e5e7eb]">
              <tr>
                <th>Start (AK)</th>
                <th>End (AK)</th>
                <th>EVSE</th>
                <th>Connector #</th>
                <th>Type</th>
                <th>Max kW</th>
                <th>Energy kWh</th>
                <th>Duration</th>
                <th>SoC Start</th>
                <th>SoC End</th>
                <th>VID</th>
                <th>Est. Revenue</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <SkeletonTable rows={10} cols={12} />
              ) : isError ? (
                <tr>
                  <td colSpan={12} className="text-center py-12">
                    <span className="text-red-500 font-medium">⚠ Failed to load sessions</span>
                    <span className="block text-gray-400 text-xs mt-1">
                      Check that the API server is running, then refresh.
                    </span>
                  </td>
                </tr>
              ) : sessions.length === 0 ? (
                <tr>
                  <td colSpan={12} className="text-center text-gray-400 py-12">
                    No sessions found for this filter
                  </td>
                </tr>
              ) : (
                sessions.map(s => (
                  <SessionRow
                    key={s.transaction_id}
                    session={s}
                    onSelect={() => setSelectedSession(s)}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* Pagination */}
        {total > PAGE_SIZE && (
          <div className="px-5 py-3 border-t border-gray-100 flex items-center justify-between text-sm text-gray-500">
            <span>
              Page {page} of {Math.ceil(total / PAGE_SIZE)}
              <span className="ml-2 text-gray-400">
                (showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total.toLocaleString()})
              </span>
            </span>
            <div className="flex gap-2">
              <button
                disabled={page === 1}
                onClick={() => setPage(p => p - 1)}
                className="px-3 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50"
              >
                ← Prev
              </button>
              <button
                disabled={page * PAGE_SIZE >= total}
                onClick={() => setPage(p => p + 1)}
                className="px-3 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50"
              >
                Next →
              </button>
            </div>
          </div>
        )}
      </div>

      {/* ── Session detail modal ─────────────────────────────────────────── */}
      {selectedSession && (
        <SessionDetailModal
          session={selectedSession}
          onClose={() => setSelectedSession(null)}
        />
      )}
    </div>
  )
}

// ── Session table row ──────────────────────────────────────────────────────────
function SessionRow({
  session: s,
  onSelect,
}: {
  session: ChargingSession
  onSelect: () => void
}) {
  return (
    <tr
      onClick={onSelect}
      className="cursor-pointer"
      title="Click to view session detail chart"
    >
      <td className="font-mono text-xs">{s.start_dt}</td>
      <td className="font-mono text-xs">{s.end_dt ?? '—'}</td>
      <td className="font-medium">{s.evse_name}</td>
      <td className="text-center">{s.connector_id ?? '—'}</td>
      <td>
        <span className="px-1.5 py-0.5 rounded text-xs bg-gray-100 text-gray-600 font-mono">
          {s.connector_type || '—'}
        </span>
      </td>
      <td className="tabular-nums">{s.max_power_kw != null ? s.max_power_kw.toFixed(1) : '—'}</td>
      <td className="tabular-nums font-medium">{s.energy_kwh != null ? s.energy_kwh.toFixed(3) : '—'}</td>
      <td className="tabular-nums">{s.duration_min != null ? `${s.duration_min} min` : '—'}</td>
      <td className="tabular-nums">{s.soc_start != null ? `${s.soc_start}%` : '—'}</td>
      <td className="tabular-nums">{s.soc_end   != null ? `${s.soc_end}%`   : '—'}</td>
      <td className="font-mono text-xs text-gray-500">
        {s.id_tag?.startsWith('VID:') ? s.id_tag : ''}
      </td>
      <td className="tabular-nums text-emerald-700 font-medium">
        {s.est_revenue_usd != null ? `$${s.est_revenue_usd.toFixed(2)}` : '—'}
      </td>
    </tr>
  )
}
