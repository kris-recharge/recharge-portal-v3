/** Data Export tab — date-range picker + CSV/XLSX download. */

import { useState } from 'react'
import { buildExportUrl } from '../lib/api'
import { supabase } from '../lib/supabase'
import { Download, Loader2 } from 'lucide-react'

// Default date range: last 30 days
function todayAK() {
  return new Date().toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}
function daysAgoAK(n: number) {
  const d = new Date()
  d.setDate(d.getDate() - n)
  return d.toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}

export function ExportTab() {
  const [startDate, setStartDate] = useState(daysAgoAK(30))
  const [endDate,   setEndDate]   = useState(todayAK())
  const [format,    setFormat]    = useState<'csv' | 'xlsx'>('csv')
  const [loading,   setLoading]   = useState(false)
  const [error,     setError]     = useState<string | null>(null)

  const handleDownload = async () => {
    setLoading(true)
    setError(null)
    try {
      const url = buildExportUrl({ start_date: startDate, end_date: endDate, format })
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
      link.download = `sessions_${startDate}_to_${endDate}.${format}`
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
    <div className="max-w-lg">
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 space-y-5">
        <h2 className="text-sm font-semibold text-gray-700">Export Charging Sessions</h2>

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
            <label className="block text-xs font-medium text-gray-500 mb-1">Format</label>
            <div className="flex gap-3">
              {(['csv', 'xlsx'] as const).map(f => (
                <button
                  key={f}
                  onClick={() => setFormat(f)}
                  className={`px-4 py-2 rounded-lg border text-sm font-medium transition-colors ${
                    format === f
                      ? 'bg-blue-600 text-white border-blue-600'
                      : 'bg-white text-gray-600 border-gray-200 hover:border-blue-300'
                  }`}
                >
                  {f.toUpperCase()}
                </button>
              ))}
            </div>
          </div>
        </div>

        <button
          onClick={handleDownload}
          disabled={loading}
          className="w-full flex items-center justify-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white text-sm font-semibold rounded-lg transition-colors"
        >
          {loading
            ? <><Loader2 size={16} className="animate-spin" /> Preparing…</>
            : <><Download size={16} /> Download {format.toUpperCase()}</>
          }
        </button>
        {error && <p className="text-xs text-red-500">{error}</p>}

        <p className="text-xs text-gray-400">
          Sessions sheet: Start/End DateTime (AK), EVSE, Location, Connector, Type,
          Max kW, Energy kWh, Duration (min), SoC Start/End, Authentication, Est. Revenue, VID.
          {' '}<span className="font-medium text-gray-500">XLSX only:</span>{' '}
          includes a second <em>Vendor Faults</em> sheet with all non-NoError
          StatusNotifications for the selected date range.
        </p>
      </div>
    </div>
  )
}
