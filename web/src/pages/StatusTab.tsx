/** Status History tab — fault/error events table with live updates. */

import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { supabase } from '../lib/supabase'
import { fetchStatusHistory } from '../lib/api'
import { StatusBadge } from '../components/StatusBadge'
import { SkeletonTable } from '../components/Skeleton'

export function StatusTab() {
  const queryClient = useQueryClient()

  const { data, isLoading } = useQuery({
    queryKey: ['status-history'],
    queryFn:  () => fetchStatusHistory({ limit: 500 }),
  })

  // Live updates when new StatusNotification rows arrive
  useEffect(() => {
    const channel = supabase
      .channel('ocpp_events_status')
      .on(
        'postgres_changes',
        {
          event:  'INSERT',
          schema: 'public',
          table:  'ocpp_events',
          filter: "action=eq.StatusNotification",
        },
        () => { queryClient.invalidateQueries({ queryKey: ['status-history'] }) },
      )
      .subscribe()

    return () => { supabase.removeChannel(channel) }
  }, [queryClient])

  const events = data?.events ?? []
  const faultCount = events.filter(e => e.status === 'Faulted').length

  return (
    <div className="space-y-4">
      {faultCount > 0 && (
        <div className="bg-red-50 border border-red-200 rounded-xl px-5 py-3 text-sm text-red-700 font-medium">
          🔴 {faultCount} fault event{faultCount !== 1 ? 's' : ''} in the current view
        </div>
      )}

      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div className="px-5 py-4 border-b border-gray-100 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-gray-700">
            Status &amp; Error History
            {data && <span className="ml-2 text-gray-400 font-normal">(faults only — {data.total} events)</span>}
          </h2>
          <span className="text-xs text-emerald-600 font-medium flex items-center gap-1">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 animate-pulse inline-block" />
            Live
          </span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full data-table">
            <thead>
              <tr>
                <th>Time (AK)</th>
                <th>EVSE</th>
                <th>Connector</th>
                <th>Status</th>
                <th>Error Code</th>
                <th>Vendor Code</th>
                <th>Description</th>
              </tr>
            </thead>
            <tbody>
              {isLoading ? (
                <SkeletonTable rows={8} cols={7} />
              ) : events.length === 0 ? (
                <tr>
                  <td colSpan={7} className="text-center text-gray-400 py-12">No fault events found</td>
                </tr>
              ) : (
                events.map(e => (
                  <tr key={e.id}>
                    <td className="font-mono text-xs">{e.received_at_ak}</td>
                    <td className="font-medium">{e.evse_name}</td>
                    <td className="text-center">{e.connector_id ?? '—'}</td>
                    <td><StatusBadge status={e.status} /></td>
                    <td className="font-mono text-xs text-red-600">{e.error_code || '—'}</td>
                    <td className="font-mono text-xs text-gray-500">{e.vendor_error_code || '—'}</td>
                    <td className="text-xs text-gray-700 max-w-xs truncate" title={e.vendor_error_description ?? ''}>
                      {e.vendor_error_description || ''}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}
