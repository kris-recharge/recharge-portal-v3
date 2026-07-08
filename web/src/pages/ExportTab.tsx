/** Data Export tab — inherits filters from Sessions tab; date-range, EVSE, XLSX download.
 *  v3.2: CSV option removed — export is XLSX-only.
 *  v3.2: second box "Export Maintenance Activities" (PM logs, Q&A, parts) added to
 *  the right of the sessions export. Both boxes share the same date + EVSE filters;
 *  the maintenance export is EVSE-scoped server-side via portal_users. */

import { useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { buildExportUrl, buildMaintenanceExportUrl, fetchEvseOptions, EVSE_OPTIONS_FALLBACK } from '../lib/api'
import { EvseFilterGroups } from '../components/EvseFilterGroups'
import { supabase } from '../lib/supabase'
import { Download, Loader2 } from 'lucide-react'

function todayAK() {
  return new Date().toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}
function daysAgoAK(n: number) {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d.toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}

interface ExportFilters {
  startDate:  string
  endDate:    string
  stationIds: string[]
}

interface Props {
  initialFilters?: ExportFilters
}

interface ExportCardProps {
  title:     string
  initialFilters?: ExportFilters
  carriedOver: boolean
  /** Build the download URL for the selected filters. */
  buildUrl:  (f: { start_date: string; end_date: string; station_id?: string[] }) => string
  /** Downloaded filename prefix, e.g. "sessions" → sessions_<start>_to_<end>.xlsx */
  filePrefix: string
  footer:    ReactNode
}

function ExportCard({ title, initialFilters, carriedOver, buildUrl, filePrefix, footer }: ExportCardProps) {
  const [startDate,  setStartDate]  = useState(initialFilters?.startDate  || daysAgoAK(7))
  const [endDate,    setEndDate]    = useState(initialFilters?.endDate    || todayAK())
  const [stationIds, setStationIds] = useState<string[]>(initialFilters?.stationIds ?? [])
  const [loading,    setLoading]    = useState(false)
  const [error,      setError]      = useState<string | null>(null)

  // Live EVSE roster (reflects Admin-registered chargers); fallback while loading
  const { data: EVSE_OPTIONS = EVSE_OPTIONS_FALLBACK } = useQuery({
    queryKey: ['evse-options'],
    queryFn:  fetchEvseOptions,
    staleTime: 5 * 60_000,
  })


  const handleDownload = async () => {
    setLoading(true)
    setError(null)
    try {
      const url = buildUrl({
        start_date: startDate,
        end_date:   endDate,
        station_id: stationIds.length ? stationIds : undefined,
      })
      const { data: { session } } = await supabase.auth.getSession()
      const headers: Record<string, string> = {}
      if (session?.access_token) {
        headers['Authorization'] = `Bearer ${session.access_token}`
      }
      const res = await fetch(url, { headers })
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new Error(`Export failed (${res.status})${text ? ': ' + text : ''}`)
      }
      const blob = await res.blob()
      const link = document.createElement('a')
      link.href = URL.createObjectURL(blob)
      link.download = `${filePrefix}_${startDate}_to_${endDate}.xlsx`
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      URL.revokeObjectURL(link.href)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Download failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 space-y-5">
      <h2 className="text-sm font-semibold text-gray-700">{title}</h2>

      {carriedOver && (
        <p className="text-xs text-blue-600 bg-blue-50 rounded-lg px-3 py-2">
          Filters carried over from Charging Sessions tab. Adjust as needed.
        </p>
      )}

      <div className="space-y-4">
        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">Start Date (Alaska)</label>
          <input
            type="date"
            value={startDate}
            onChange={e => setStartDate(e.target.value)}
            className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">End Date (Alaska)</label>
          <input
            type="date"
            value={endDate}
            onChange={e => setEndDate(e.target.value)}
            className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <div>
          <label className="block text-xs font-medium text-gray-500 mb-1">EVSE</label>
          <EvseFilterGroups
            options={EVSE_OPTIONS}
            selected={stationIds}
            onChange={setStationIds}
          />
        </div>
      </div>

      <button
        onClick={handleDownload}
        disabled={loading}
        className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white text-sm font-semibold rounded-lg transition-colors"
      >
        {loading
          ? <><Loader2 size={16} className="animate-spin" /> Preparing…</>
          : <><Download size={16} /> Download XLSX</>
        }
      </button>
      {error && <p className="text-xs text-red-500">{error}</p>}

      {footer}
    </div>
  )
}

export function ExportTab({ initialFilters }: Props) {
  return (
    <div className="grid gap-6 lg:grid-cols-2 max-w-4xl">
      <ExportCard
        title="Export Charging Sessions"
        initialFilters={initialFilters}
        carriedOver={!!initialFilters}
        buildUrl={f => buildExportUrl({ ...f, format: 'xlsx' })}
        filePrefix="sessions"
        footer={
          <p className="text-xs text-gray-400">
            Sessions sheet: Start/End DateTime (AK), EVSE, Location, Connector, Type,
            Max kW, Energy kWh, Duration (min), SoC Start/End, Authentication, Authentication Method, Est. Revenue, VID.
            {' '}Also includes a <em>Vendor Faults</em> sheet (non-NoError
            StatusNotifications) and a <em>Utility Reads</em> sheet (daily metered vs.
            dispensed kWh and efficiency % per site) for the selected date range.
          </p>
        }
      />

      <ExportCard
        title="Export Maintenance Activities"
        initialFilters={initialFilters}
        carriedOver={false}
        buildUrl={buildMaintenanceExportUrl}
        filePrefix="maintenance"
        footer={
          <p className="text-xs text-gray-400">
            PM/maintenance logs submitted in the selected date range, for your
            approved EVSEs. <em>PM Logs</em> sheet: one row per log (EVSE, technician,
            submitted timestamp, labor hours, overall result, work description).
            {' '}<em>Task Results</em> sheet: every question and the response captured.
            {' '}<em>Parts Replaced</em> sheet: parts logged on each visit.
          </p>
        }
      />
    </div>
  )
}
