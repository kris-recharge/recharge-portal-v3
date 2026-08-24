/**
 * Alerts tab — per-user alert subscriptions + 15-day history.
 * Each user opts in to the alert types they want sent to their login email.
 * History shows only alerts the user is subscribed to, filtered to their EVSEs.
 */

import { useState, useEffect, useCallback } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  fetchAlertSubscriptions,
  fetchAlertHistory,
  saveAlertSubscriptions,
  sendTestPush,
  setBannerScope,
  AlertSubscription,
  AlertType,
} from '../lib/api'
import {
  disablePush,
  enablePush,
  hasLocalSubscription,
  isStandalone,
  permissionState,
  pushSupported,
} from '../lib/push'
import {
  Bell, BellOff, Clock, AlertTriangle, Wifi, Search, CalendarClock,
  Smartphone, Mail, Send, Monitor,
} from 'lucide-react'

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
  {
    type: 'pm_due_14d',
    label: 'PM Due in 14 Days',
    description: 'A scheduled preventive maintenance visit is coming up within 14 days. Sent once per PM cycle.',
    icon: <CalendarClock size={18} />,
    color: 'text-blue-700',
  },
  {
    type: 'pm_overdue',
    label: 'PM Overdue',
    description: 'A PM is due today or past due. Sent on the due date, then weekly until the PM is logged.',
    icon: <CalendarClock size={18} />,
    color: 'text-red-700',
  },
]

const TYPE_LABELS: Record<string, string> = {
  offline_idle:        '⚠ Offline – Idle',
  offline_mid_session: '⚠ Offline – Mid-Session',
  fault:               '🔴 Fault',
  suspicious_vid:      '🔍 Suspicious VID',
  pm_due_14d:          '🔔 PM Due Soon',
  pm_overdue:          '⚠ PM Overdue',
}

// ── Component ─────────────────────────────────────────────────────────────────

type Channels = { enabled: boolean; email_enabled: boolean; push_enabled: boolean }

const BLANK: Channels = { enabled: false, email_enabled: true, push_enabled: false }

const EMPTY_DRAFT: Record<AlertType, Channels> = {
  offline_idle:        { ...BLANK },
  offline_mid_session: { ...BLANK },
  fault:               { ...BLANK },
  suspicious_vid:      { ...BLANK },
  pm_due_14d:          { ...BLANK },
  pm_overdue:          { ...BLANK },
}

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

  // Local draft of subscription + per-channel state
  const [draft, setDraft] = useState<Record<AlertType, Channels>>(EMPTY_DRAFT)
  const [dirty, setDirty] = useState(false)
  const [saved, setSaved] = useState(false)

  // ── Push state for THIS device ──────────────────────────────────────────────
  const [deviceOn, setDeviceOn] = useState(false)
  const [pushBusy, setPushBusy] = useState(false)
  const [pushMsg, setPushMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)

  const refreshDeviceState = useCallback(async () => {
    setDeviceOn(await hasLocalSubscription())
  }, [])

  useEffect(() => { void refreshDeviceState() }, [refreshDeviceState])

  // Sync draft when server data loads
  useEffect(() => {
    if (!subData) return
    const map: Record<string, Channels> = {}
    subData.subscriptions.forEach(s => {
      map[s.alert_type] = {
        enabled:       s.enabled,
        email_enabled: s.email_enabled,
        push_enabled:  s.push_enabled,
      }
    })
    setDraft({
      offline_idle:        map['offline_idle']        ?? { ...BLANK },
      offline_mid_session: map['offline_mid_session'] ?? { ...BLANK },
      fault:               map['fault']               ?? { ...BLANK },
      suspicious_vid:      map['suspicious_vid']      ?? { ...BLANK },
      pm_due_14d:          map['pm_due_14d']          ?? { ...BLANK },
      pm_overdue:          map['pm_overdue']          ?? { ...BLANK },
    })
    setDirty(false)
  }, [subData])

  const saveMutation = useMutation({
    mutationFn: () => {
      const payload: AlertSubscription[] = ALERT_DEFS.map(d => ({
        alert_type:    d.type,
        enabled:       draft[d.type].enabled,
        email_enabled: draft[d.type].email_enabled,
        push_enabled:  draft[d.type].push_enabled,
      }))
      return saveAlertSubscriptions(payload)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['alert-subscriptions'] })
      qc.invalidateQueries({ queryKey: ['alert-history'] })
      setDirty(false)
      setSaved(true)
      setTimeout(() => setSaved(false), 3000)
    },
  })

  const bannerScopeMutation = useMutation({
    mutationFn: (value: boolean) => setBannerScope(value),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['alert-subscriptions'] }),
  })

  const toggle = (type: AlertType) => {
    setDraft(prev => {
      const next = !prev[type].enabled
      return {
        ...prev,
        [type]: {
          ...prev[type],
          enabled: next,
          // Turning a type on with no channel selected would subscribe the user
          // to silence. Default the freshly-enabled row to whichever channel is
          // actually usable on this account.
          email_enabled:
            next && !prev[type].email_enabled && !prev[type].push_enabled
              ? !deviceOn
              : prev[type].email_enabled,
          push_enabled:
            next && !prev[type].email_enabled && !prev[type].push_enabled
              ? deviceOn
              : prev[type].push_enabled,
        },
      }
    })
    setDirty(true)
    setSaved(false)
  }

  const toggleChannel = (type: AlertType, channel: 'email_enabled' | 'push_enabled') => {
    setDraft(prev => ({
      ...prev,
      [type]: { ...prev[type], [channel]: !prev[type][channel] },
    }))
    setDirty(true)
    setSaved(false)
  }

  // ── Push enable / disable / test ────────────────────────────────────────────
  const handleEnablePush = async () => {
    setPushBusy(true); setPushMsg(null)
    try {
      const res = await enablePush(subData?.vapid_public_key ?? '')
      if (res.ok) {
        setPushMsg({ kind: 'ok', text: 'Notifications enabled on this device.' })
        qc.invalidateQueries({ queryKey: ['alert-subscriptions'] })
      } else {
        setPushMsg({ kind: 'err', text: res.reason ?? 'Could not enable notifications.' })
      }
    } catch (e) {
      setPushMsg({ kind: 'err', text: (e as Error).message })
    } finally {
      await refreshDeviceState()
      setPushBusy(false)
    }
  }

  const handleDisablePush = async () => {
    setPushBusy(true); setPushMsg(null)
    try {
      await disablePush()
      setPushMsg({ kind: 'ok', text: 'Notifications turned off for this device.' })
      qc.invalidateQueries({ queryKey: ['alert-subscriptions'] })
    } catch (e) {
      setPushMsg({ kind: 'err', text: (e as Error).message })
    } finally {
      await refreshDeviceState()
      setPushBusy(false)
    }
  }

  const handleTestPush = async () => {
    setPushBusy(true); setPushMsg(null)
    try {
      const res = await sendTestPush()
      setPushMsg(
        res.delivered > 0
          ? { kind: 'ok', text: `Test sent to ${res.delivered} of ${res.devices} device(s).` }
          : { kind: 'err', text: 'No device accepted the test notification.' },
      )
    } catch (e) {
      setPushMsg({ kind: 'err', text: (e as Error).message })
    } finally {
      setPushBusy(false)
    }
  }

  const anyEnabled = ALERT_DEFS.some(d => draft[d.type].enabled)
  const enabledCount = ALERT_DEFS.filter(d => draft[d.type].enabled).length

  const pushReady    = subData?.push_supported ?? false
  const canSubscribe = pushSupported()
  const permission   = permissionState()
  const standalone   = isStandalone()

  return (
    <div className="space-y-6">

      {/* ── Notifications on this device ──────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center gap-2">
          <Smartphone size={16} className="text-blue-600" />
          <h2 className="font-semibold text-gray-900 text-sm">Notifications on This Device</h2>
        </div>

        <div className="px-6 py-4">
          {!pushReady ? (
            <p className="text-sm text-gray-500">
              Push notifications aren't configured on the server yet. Alerts will keep
              arriving by email until they are.
            </p>
          ) : !canSubscribe ? (
            <div className="text-sm text-gray-600 space-y-2">
              <p className="font-medium text-gray-800">
                {standalone
                  ? 'This browser does not support push notifications.'
                  : 'Add the dashboard to your Home Screen to enable notifications.'}
              </p>
              {!standalone && (
                <ol className="list-decimal ml-5 space-y-1 text-xs text-gray-500">
                  <li>Tap the Share button in Safari.</li>
                  <li>Choose <span className="font-medium">Add to Home Screen</span>.</li>
                  <li>Open the dashboard from the new Home Screen icon, then come back here.</li>
                </ol>
              )}
              <p className="text-xs text-gray-400">
                iPhone and iPad only deliver web notifications to apps installed on the
                Home Screen — a Safari tab can't receive them.
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="text-sm font-medium text-gray-800">
                    {deviceOn ? 'Enabled on this device' : 'Not enabled on this device'}
                  </div>
                  <p className="text-xs text-gray-500 mt-0.5">
                    {deviceOn
                      ? 'Alerts you route to Push will arrive here even when the app is closed.'
                      : 'Turn this on to receive alerts without an email.'}
                  </p>
                  {permission === 'denied' && (
                    <p className="text-xs text-red-600 mt-1">
                      Notifications are blocked. Enable them in iOS Settings → Notifications → ReCharge.
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {deviceOn && (
                    <button
                      disabled={pushBusy}
                      onClick={handleTestPush}
                      className="px-3 py-1.5 text-xs font-medium rounded-lg border border-gray-200
                                 text-gray-600 hover:bg-gray-50 disabled:opacity-40 transition-colors
                                 inline-flex items-center gap-1.5"
                    >
                      <Send size={12} /> Test
                    </button>
                  )}
                  <button
                    disabled={pushBusy || permission === 'denied'}
                    onClick={deviceOn ? handleDisablePush : handleEnablePush}
                    className={`px-4 py-1.5 text-xs font-medium rounded-lg transition-colors
                                disabled:opacity-40 disabled:cursor-not-allowed ${
                      deviceOn
                        ? 'border border-gray-200 text-gray-600 hover:bg-gray-50'
                        : 'bg-blue-600 text-white hover:bg-blue-700'
                    }`}
                  >
                    {pushBusy ? 'Working…' : deviceOn ? 'Turn Off' : 'Enable Notifications'}
                  </button>
                </div>
              </div>

              {pushMsg && (
                <p className={`text-xs ${pushMsg.kind === 'ok' ? 'text-green-600' : 'text-red-600'}`}>
                  {pushMsg.text}
                </p>
              )}

              {(subData?.push_devices.length ?? 0) > 0 && (
                <div className="pt-2 border-t border-gray-50">
                  <p className="text-xs text-gray-400 mb-1.5">
                    Registered devices ({subData!.push_devices.length})
                  </p>
                  <ul className="space-y-1">
                    {subData!.push_devices.map(d => (
                      <li key={d.id} className="flex items-center gap-2 text-xs text-gray-500">
                        <Monitor size={12} className="shrink-0 text-gray-300" />
                        <span className="truncate flex-1">{describeDevice(d.user_agent)}</span>
                        {d.is_current && (
                          <span className="shrink-0 px-1.5 py-0.5 rounded bg-blue-50 text-blue-600 font-medium">
                            this device
                          </span>
                        )}
                        <span className="shrink-0 text-gray-300 tabular-nums">{d.last_seen_at_ak}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── Subscription Settings ─────────────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200">
        <div className="px-6 py-4 border-b border-gray-100">
          <div className="flex items-center gap-2">
            <Bell size={16} className="text-blue-600" />
            <h2 className="font-semibold text-gray-900 text-sm">Alert Subscriptions</h2>
          </div>
          {subData && (
            <p className="text-xs text-gray-500 mt-0.5">
              Email alerts go to <span className="font-medium text-gray-700">{subData.email}</span>
              {' · '}push goes to your enabled devices
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
                  draft[def.type].enabled ? 'bg-blue-600' : 'bg-gray-200'
                }`}
                aria-label={`Toggle ${def.label}`}
              >
                <span
                  className={`block w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 mx-1 ${
                    draft[def.type].enabled ? 'translate-x-4' : 'translate-x-0'
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

                {/* Delivery channels — only meaningful once the type is on */}
                {draft[def.type].enabled && (
                  <div className="flex items-center gap-2 mt-2">
                    <ChannelChip
                      active={draft[def.type].email_enabled}
                      onClick={() => toggleChannel(def.type, 'email_enabled')}
                      icon={<Mail size={11} />}
                      label="Email"
                    />
                    <ChannelChip
                      active={draft[def.type].push_enabled}
                      onClick={() => toggleChannel(def.type, 'push_enabled')}
                      icon={<Smartphone size={11} />}
                      label="Push"
                      disabled={!pushReady}
                      title={pushReady ? undefined : 'Push is not configured on the server'}
                    />
                    {!draft[def.type].email_enabled && !draft[def.type].push_enabled && (
                      <span className="text-xs text-amber-600">
                        No delivery — banner and history only
                      </span>
                    )}
                    {draft[def.type].push_enabled && !deviceOn && (
                      <span className="text-xs text-gray-400">
                        enable notifications above to receive these
                      </span>
                    )}
                  </div>
                )}
              </div>

              {/* Status badge */}
              <span className={`shrink-0 mt-1 text-xs px-2 py-0.5 rounded-full font-medium ${
                draft[def.type].enabled
                  ? 'bg-blue-50 text-blue-700'
                  : 'bg-gray-100 text-gray-400'
              }`}>
                {draft[def.type].enabled ? 'On' : 'Off'}
              </span>
            </div>
          ))}
        </div>

        {/* Save bar */}
        <div className="px-6 py-3 border-t border-gray-100 flex items-center justify-between bg-gray-50 rounded-b-xl">
          <span className="text-xs text-gray-400">
            {!anyEnabled
              ? 'No alerts enabled — you will not receive notifications.'
              : `${enabledCount} alert type${enabledCount > 1 ? 's' : ''} enabled`}
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

      {/* ── In-app banner scope ───────────────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200 px-6 py-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 className="font-semibold text-gray-900 text-sm">Show All Alert Types In-App</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              While you're signed in, show every alert type on your chargers — fault
              codes, offline, suspicious VID, PM — in the toasts and the history below.
              Subscriptions above only control what gets emailed or pushed to you.
            </p>
          </div>
          <button
            disabled={bannerScopeMutation.isPending || !subData}
            onClick={() => bannerScopeMutation.mutate(!(subData?.banner_all_alert_types ?? false))}
            className={`mt-0.5 shrink-0 w-10 h-6 rounded-full transition-colors duration-200 focus:outline-none ${
              subData?.banner_all_alert_types ? 'bg-blue-600' : 'bg-gray-200'
            }`}
            aria-label="Toggle banners for all alert types"
          >
            <span
              className={`block w-4 h-4 rounded-full bg-white shadow transition-transform duration-200 mx-1 ${
                subData?.banner_all_alert_types ? 'translate-x-4' : 'translate-x-0'
              }`}
            />
          </button>
        </div>
        <p className="text-xs text-gray-400 mt-2">
          {subData?.banner_all_alert_types
            ? 'On — showing every alert type for your chargers. Always limited to your own EVSEs.'
            : 'Off — narrowed to the alert types you subscribed to above.'}
        </p>
      </div>

      {/* ── Alert History ─────────────────────────────────────────────── */}
      <div className="bg-white rounded-xl border border-gray-200">
        <div className="px-6 py-4 border-b border-gray-100 flex items-center justify-between">
          <div>
            <h2 className="font-semibold text-gray-900 text-sm">Alert History</h2>
            <p className="text-xs text-gray-500 mt-0.5">
              Last 15 days · always limited to your EVSEs
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
              <p className="text-xs text-gray-400 mt-1">
                Nothing has fired recently. Enable alert types above to also get them by email or push.
              </p>
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
    pm_due_14d:          'bg-blue-50 text-blue-700',
    pm_overdue:          'bg-red-50 text-red-700',
  }
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium whitespace-nowrap ${
      styles[type] ?? 'bg-gray-100 text-gray-600'
    }`}>
      {TYPE_LABELS[type] ?? type}
    </span>
  )
}


// ── Delivery channel chip ─────────────────────────────────────────────────────
function ChannelChip({
  active, onClick, icon, label, disabled, title,
}: {
  active: boolean
  onClick: () => void
  icon: React.ReactNode
  label: string
  disabled?: boolean
  title?: string
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium
                  border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
        active
          ? 'bg-blue-50 text-blue-700 border-blue-200'
          : 'bg-white text-gray-400 border-gray-200 hover:border-gray-300'
      }`}
    >
      {icon}
      {label}
    </button>
  )
}

// ── Device label ──────────────────────────────────────────────────────────────
/** Turn a raw user-agent into something a person can recognise in a list. */
function describeDevice(ua: string): string {
  if (!ua) return 'Unknown device'
  if (/iPad/i.test(ua)) return 'iPad'
  if (/iPhone/i.test(ua)) return 'iPhone'
  if (/Android/i.test(ua)) return 'Android device'
  if (/Macintosh|Mac OS X/i.test(ua)) return 'Mac'
  if (/Windows/i.test(ua)) return 'Windows PC'
  return 'Other device'
}
