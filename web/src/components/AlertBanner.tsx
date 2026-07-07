/**
 * Alert banner — subscribes to /api/alerts/stream (SSE) and shows
 * a dismissible notification bar when a new alert arrives.
 *
 * Uses a fetch-based SSE reader instead of EventSource: the backend
 * authenticates via Authorization: Bearer (Supabase session lives in
 * localStorage, so there is no auth cookie for EventSource to send).
 * Reconnects with exponential backoff and re-reads the session token
 * on every (re)connect so hourly token rotation doesn't kill the stream.
 */

import { useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { supabase } from '../lib/supabase'

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
  pm_due_14d:         '🔔 PM Due Soon',
  pm_overdue:         '⚠ PM Overdue',
}

const INITIAL_RETRY_MS = 3_000
const MAX_RETRY_MS     = 60_000

export function AlertBanner() {
  const [alerts, setAlerts] = useState<AlertItem[]>([])
  const idRef = useRef(0)

  useEffect(() => {
    let cancelled = false
    let abort: AbortController | null = null

    function handleData(raw: string) {
      try {
        const data = JSON.parse(raw)
        setAlerts(prev => [
          { ...data, id: ++idRef.current },
          ...prev.slice(0, 4), // keep at most 5 visible
        ])
      } catch {
        // ignore parse errors
      }
    }

    async function run() {
      let retryMs = INITIAL_RETRY_MS
      while (!cancelled) {
        try {
          const { data: { session } } = await supabase.auth.getSession()
          const token = session?.access_token
          if (!token) throw new Error('no session')

          abort = new AbortController()
          const res = await fetch('/api/alerts/stream', {
            headers: { Authorization: `Bearer ${token}` },
            credentials: 'include',
            signal: abort.signal,
          })
          if (!res.ok || !res.body) throw new Error(`stream ${res.status}`)

          retryMs = INITIAL_RETRY_MS // connected — reset backoff
          const reader = res.body.getReader()
          const decoder = new TextDecoder()
          let buf = ''

          while (!cancelled) {
            const { done, value } = await reader.read()
            if (done) break
            buf += decoder.decode(value, { stream: true })
            // SSE frames are separated by a blank line
            let idx: number
            while ((idx = buf.indexOf('\n\n')) !== -1) {
              const frame = buf.slice(0, idx)
              buf = buf.slice(idx + 2)
              for (const line of frame.split('\n')) {
                if (line.startsWith('data: ')) handleData(line.slice(6))
              }
            }
          }
        } catch {
          // connection failed or dropped — fall through to backoff
        }
        if (!cancelled) {
          await new Promise(r => setTimeout(r, retryMs))
          retryMs = Math.min(retryMs * 2, MAX_RETRY_MS)
        }
      }
    }

    run()
    return () => {
      cancelled = true
      abort?.abort()
    }
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
            <div className="text-xs text-gray-600 mt-0.5 line-clamp-3">{alert.message}</div>
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
