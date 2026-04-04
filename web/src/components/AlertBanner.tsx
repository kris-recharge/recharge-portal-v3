/**
 * Alert banner — subscribes to /api/alerts/stream (SSE) and shows
 * a dismissible notification bar when a new alert arrives.
 */

import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'

interface AlertItem {
  id: number
  alert_type: string
  evse_name: string
  message: string
  timestamp_ak: string
}

const TYPE_LABELS: Record<string, string> = {
  offline_idle:       '⚠ Charger Offline',
  offline_mid_session:'⚠ Offline Mid-Session',
  fault:              '🔴 Fault Detected',
  suspicious_vid:     '🔍 Suspicious VID',
}

export function AlertBanner() {
  const [alerts, setAlerts] = useState<AlertItem[]>([])
  const idRef = useRef(0)

  useEffect(() => {
    const es = new EventSource('/api/alerts/stream', { withCredentials: true })

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data)
        setAlerts(prev => [
          { ...data, id: ++idRef.current },
          ...prev.slice(0, 4), // keep at most 5 visible
        ])
      } catch {
        // ignore parse errors
      }
    }

    return () => es.close()
  }, [])

  if (alerts.length === 0) return null

  return (
    <div className="fixed top-4 right-4 z-50 flex flex-col gap-2 w-96 max-w-full">
      {alerts.map(alert => (
        <div
          key={alert.id}
          className="bg-white border border-red-200 shadow-lg rounded-xl px-4 py-3 flex items-start gap-3"
        >
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-red-700">
              {TYPE_LABELS[alert.alert_type] ?? alert.alert_type}
            </div>
            <div className="text-sm text-gray-700 truncate">{alert.evse_name}</div>
            <div className="text-xs text-gray-400 mt-0.5">{alert.timestamp_ak}</div>
          </div>
          <button
            onClick={() => setAlerts(prev => prev.filter(a => a.id !== alert.id))}
            className="shrink-0 text-gray-400 hover:text-gray-600 mt-0.5"
          >
            <X size={14} />
          </button>
        </div>
      ))}
    </div>
  )
}
