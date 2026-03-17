/**
 * Alerts tab — per-user alert subscriptions + 15-day history.
 * Each user opts in to the alert types they want sent to their login email.
 * History shows only alerts the user is subscribed to, filtered to their EVSEs.
 */

import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  fetchAlertSubscriptions,
  fetchAlertHistory,
  saveAlertSubscriptions,
  AlertSubscription,
  AlertType,
} from '../lib/api'
import { Bell, BellOff, Clock, AlertTriangle, Wifi, Search } from 'lucide-react'

// ── Alert type metadata ───────────────────────────────────────────────────────

interface AlertMeta {
  type: AlertType
  label: string
  description: string
  icon: React.ReactNode
  color: string
}

const ALERT_DEFS: AlertMeta[] = [
  {
    type: 'offline_idle',
    label: 'Charger Offline – Idle',
    description: 'No messages received from charger for 20+ minutes while idle.',
    icon: <Wifi size={18} />,
    color: 'text-orange-600',
  },
  {
    type: 'offline_mid_session',
    label: 'Charger Offline – Mid-Session',
    description: 'Charger goes silent for 5+ minutes during an active charging session.',
    icon: <Clock size={18} />,
    color: 'text-red-600',
  },
  {
    type: 'fault',
    label: 'Fault / Error Code',
    description: 'StatusNotification received with a non-NoError error code.',
    icon: <AlertTriangle size={18} />,
    color: 'text-red-700',
  },
  {
    type: 'suspicious_vid',
    label: 'Suspicious VID Activity',
    description: 'Same vehicle ID delivers < 1 kWh then starts a new session within 5 minutes.',
    icon: <Search size={18} />,
    color: 'text-yellow-700',
  },
]

const TYPE_LABELS: Record<string, string> = {
  offline_idle:        '⚠ Offline – Idle',
  offline_mid_session: '⚠ Offline – Mid-Session',
  fault:               '🔴 Fault',
  suspicious_vid:      '🔍 Suspicious VID',
}

// ── Component ─────────────────────────────────────────────────────────────────

export function AlertsTab() {
  const qc = useQueryClient()

  const { data: subData, isLoading: subLoading } = useQuery({
    queryKey: ['alert-subscriptions'],
    queryFn:  fetchAlertSubscriptions,
  })

  const { data: histData, isLoading: histLoading } = useQuery({
    queryKey: ['alert-history'],
    queryFn:  fetchAlertHistory,
    refetchInterval: 60_000,   // refresh history every 60s
  })

  // Local draft of subscription enabled state
  const [draft, setDraft] = useState<Record<AlertType, boolean>>({
    offline_idle:        false,
    offline_mid_session: false,
    fault:               false,
    suspicious_vid:      false,
  })
  const [dirty, setDirty] = useState(false)
  const [saved, setSaved] = useState(false)

  // Sync draft when server data loads
  useEffect(() => {
    if (!subData) return
    const map: Record<string, boolean> = {}
    subData.subscriptions.forEach(s => { map[s.alert_type] = s.enabled })
    setDraft({
      offline_idle:        map['offline_idle']        ?? false,
      offline_mid_session: map['offline_mid_session'] ?? false,
      fault:               map['fault']               ?? false,
      suspicious_vid:      map['suspicious_vid']      ?? false,
    })
    setDirty(false)
  }, [subData])

  const saveMutation = useMutation({
    mutationFn: () => {
      const payload: AlertSubscription[] = ALERT_DEFS.map(d => ({
        alert_type: d.type,
        enabled:    draft[d.type],
      }))
      return saveAlertSubscriptions(payload)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alert-subscriptions'] })
      setDirty(false)
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    },
  })

  const toggle = (type: AlertType) => {
    setDraft(prev => ({ ...prev, [type]: !prev[type] }))
    setDirty(true)
    setSaved(false)
  }

  const anyEnabled = Object.values(draft).some(Boolean)

  return (
    <div className="space-y-6">

      {/* ── Subscription Settings ─────────────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200">
        <div className="px-6 py-4 border-b border-gray-100">
          <div className="flex items-center gap-2">
            <Bell size={16} className="text-blue-600" />
            <h2 className="font-semibold text-gray-900 text-sm">Alert Subscriptions</h2>
          </div>
          {subData && (
            <p className="text-xs text-gray-500 mt-0.5">
              Alerts will be sent to <span className="font-medium text-gray-700">{subData.email}</span>
            </p>
          )}
        </div>

        <div className="divide-y divide-gray-50">
          {ALERT_DEFS.map(def => (
            <div
              key={def.type}
              className="px-6 py-4 flex items-start gap-4 hover:bg-gray-50 transition-colors"
            >
              {/* Toggle */}
              <button
                disabled={subLoading}
                onClick={() => toggle(def.type)}
                className={`mt-0.5 shrink-0 w-10 h-6 rounded-full transition-colors duration-200 focus:outline-none ${
                  draft[def.type] ? 'bg-blue-600' : 'bg-gray-200'
                }`}
                aria-label={`Toggle ${def.label}`}
              >
                <span
                  className={`block w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 mx-1 ${
                    draft[def.type] ? 'translate-x-4' : 'translate-x-0'
                  }`}
                />
              </button>

              {/* Icon + text */}
              <div className="flex-1 min-w-0">
                <div className={`flex items-center gap-2 text-sm font-medium ${def.color}`}>
                  {def.icon}
                  {def.label}
                </div>
                <p className="text-xs text-gray-500 mt-0.5">{def.description}</p>
              </div>

              {/* Status badge */}
              <span className={`shrink-0 mt-1 text-xs px-2 py-0.5 rounded-full font-medium ${
                draft[def.type]
                  ? 'bg-blue-50 text-blue-700'
                  : 'bg-gray-100 text-gray-400'
              }`}>
                {draft[def.type] ? 'On' : 'Off'}
              </span>
            </div>
          ))}
        </div>

        {/* Save bar */}
        <div className="px-6 py-3 border-t border-gray-100 flex items-center justify-between bg-gray-50 rounded-b-xl">
          <span className="text-xs text-gray-400">
            {!anyEnabled
              ? 'No alerts enabled — you will not receive notifications.'
              : `${Object.values(draft).filter(Boolean).length} alert type${Object.values(draft).filter(Boolean).length > 1 ? 's' : ''} enabled`}
          </span>
          <div className="flex items-center gap-3">
            {saved && (
              <span className="text-xs text-green-600 font-medium">✓ Preferences saved</span>
            )}
            <button
              disabled={!dirty || saveMutation.isPending}
              onClick={() => saveMutation.mutate()}
              className="px-4 py-1.5 text-xs font-medium rounded-lg bg-blue-600 text-white
                         hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
            >
              {saveMutation.isPending ? 'Saving…' : 'Save Preferences'}
            </button>
          </div>
        </div>
      </div>

      {/* ── Alert History ─────────────────────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <div>
            <h2 className="font-semibold text-gray-900 text-sm">Alert History</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Last 15 days · filtered to your EVSEs and subscribed alert types
            </p>
          </div>
          {histData && (
            <span className="text-xs text-gray-400 tabular-nums">
              {histData.alerts.length} alert{histData.alerts.length !== 1 ? 's' : ''}
            </span>
          )}
        </div>

        {histLoading ? (
          <div className="px-6 py-8 text-center text-sm text-gray-400">Loading history…</div>
        ) : !histData || histData.alerts.length === 0 ? (
          <div className="px-6 py-10 text-center">
            <BellOff size={28} className="mx-auto text-gray-300 mb-2" />
            <p className="text-sm text-gray-400">No alerts in the last 15 days</p>
            {!anyEnabled && (
              <p className="text-xs text-gray-400 mt-1">Enable alert types above to start receiving notifications.</p>
            )}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 text-left text-xs text-gray-400 font-medium uppercase tracking-wide">
                  <th className="px-6 py-3">Time (AK)</th>
                  <th className="px-4 py-3">Type</th>
                  <th className="px-4 py-3">Charger</th>
                  <th className="px-4 py-3">Detail</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {histData.alerts.map(alert => (
                  <tr key={alert.id} className="hover:bg-gray-50">
                    <td className="px-6 py-3 font-mono text-xs text-gray-500 whitespace-nowrap">
                      {alert.fired_at_ak}
                    </td>
                    <td className="px-4 py-3">
                      <TypeBadge type={alert.alert_type} />
                    </td>
                    <td className="px-4 py-3 font-medium text-gray-800 whitespace-nowrap">
                      {alert.evse_name}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500 max-w-xs truncate">
                      {alert.message}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Type badge ────────────────────────────────────────────────────────────────
function TypeBadge({ type }: { type: string }) {
  const styles: Record<string, string> = {
    offline_idle:        'bg-orange-50 text-orange-700',
    offline_mid_session: 'bg-red-50 text-red-700',
    fault:               'bg-red-100 text-red-800',
    suspicious_vid:      'bg-yellow-50 text-yellow-800',
  }
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap ${
      styles[type] ?? 'bg-gray-100 text-gray-600'
    }`}>
      {TYPE_LABELS[type] ?? type}
    </span>
  )
}
