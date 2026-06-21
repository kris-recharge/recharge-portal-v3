/**
 * Connector Count tab — lifetime plug-in odometer per cord.
 *
 * One card per charger: manufacturer name on top, EVSE name below, then the
 * connection (plug-in) count for connector 1 on the LEFT and connector 2 on
 * the RIGHT. Drives the cord-replacement strategy — connection-count-based for
 * Autel/ABB/Alpitronics, time-based for Tritium.
 *
 * Cord reset (admin + PM submitters): a 2-step Reset → confirm-with-note logs a
 * cord change and restarts the displayed count at 0. The prior cord's lifetime
 * is retained and shown as "Previous cord: N".
 */

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  fetchConnectorCount,
  resetConnectorCord,
  type ConnectorCountCard,
  type ConnectorCount,
} from '../lib/api'
import { Cable, RefreshCw, AlertCircle, RotateCcw, X } from 'lucide-react'

interface ResetTarget {
  stationId: string
  evseName: string
  connectorId: number
  connectorType: string
  current: number
}

export function ConnectorCountTab({ canReset = false }: { canReset?: boolean }) {
  const queryClient = useQueryClient()
  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: ['connector-count'],
    queryFn: fetchConnectorCount,
    refetchInterval: 60_000,
  })

  const [target, setTarget] = useState<ResetTarget | null>(null)
  const chargers = data?.chargers ?? []

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-gray-900 flex items-center gap-2">
            <Cable size={16} className="text-gray-400" /> Connector Count
          </h2>
          <p className="text-xs text-gray-400 mt-0.5">
            Lifetime plug-ins per cord since the current cord was installed. Connector 1 (left) and connector 2 (right).
          </p>
        </div>
        <button onClick={() => refetch()}
          className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-700 transition-colors pb-2">
          <RefreshCw size={13} className={isFetching ? 'animate-spin' : ''} /> Refresh
        </button>
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center py-20 text-sm text-gray-400">
          <RefreshCw size={16} className="animate-spin mr-2" /> Loading connector counts…
        </div>
      ) : error ? (
        <div className="flex items-center gap-2 text-red-500 text-sm py-8">
          <AlertCircle size={16} />
          Failed to load connector counts.{' '}
          <button onClick={() => refetch()} className="underline">Retry</button>
        </div>
      ) : chargers.length === 0 ? (
        <div className="text-center py-16 text-gray-400 text-sm">
          No chargers available for your units.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {chargers.map(c => (
            <ChargerCard key={c.station_id} charger={c} canReset={canReset} onReset={setTarget} />
          ))}
        </div>
      )}

      {target && (
        <ResetModal
          target={target}
          onClose={() => setTarget(null)}
          onDone={() => {
            setTarget(null)
            queryClient.invalidateQueries({ queryKey: ['connector-count'] })
          }}
        />
      )}
    </div>
  )
}

// ── One charger card ──────────────────────────────────────────────────────────
function ChargerCard({
  charger, canReset, onReset,
}: {
  charger: ConnectorCountCard
  canReset: boolean
  onReset: (t: ResetTarget) => void
}) {
  const left  = charger.connectors.find(c => c.connector_id === 1)
  const right = charger.connectors.find(c => c.connector_id === 2)

  const toTarget = (c: ConnectorCount): ResetTarget => ({
    stationId: charger.station_id,
    evseName: charger.evse_name,
    connectorId: c.connector_id,
    connectorType: c.connector_type,
    current: c.attempts,
  })

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      {/* Header: manufacturer on top, EVSE name below */}
      <div className="px-5 pt-4 pb-3 border-b border-gray-100">
        <div className="text-xs font-semibold uppercase tracking-wide text-blue-600">
          {charger.manufacturer || 'Unknown'}
        </div>
        <div className="text-sm font-semibold text-gray-900">{charger.evse_name}</div>
      </div>

      {/* Left = connector 1, Right = connector 2 */}
      <div className="grid grid-cols-2 divide-x divide-gray-100">
        <ConnectorSide side="Connector 1" connector={left}
          canReset={canReset} onReset={left && (() => onReset(toTarget(left)))} />
        <ConnectorSide side="Connector 2" connector={right}
          canReset={canReset} onReset={right && (() => onReset(toTarget(right)))} />
      </div>
    </div>
  )
}

function ConnectorSide({
  side, connector, canReset, onReset,
}: {
  side: string
  connector: ConnectorCount | undefined
  canReset: boolean
  onReset: (() => void) | false | undefined
}) {
  return (
    <div className="px-5 py-5 text-center">
      <div className="text-[11px] font-medium text-gray-400">
        {side}{connector?.connector_type ? ` · ${connector.connector_type}` : ''}
      </div>
      <div className="mt-2 text-3xl font-bold text-gray-800 leading-none tabular-nums">
        {(connector?.attempts ?? 0).toLocaleString()}
      </div>
      <div className="mt-1 text-[11px] text-gray-400">plug-ins</div>

      {connector?.previous_cord_attempts != null && (
        <div className="mt-2 text-[11px] text-gray-400">
          Previous cord: {connector.previous_cord_attempts.toLocaleString()}
        </div>
      )}

      {canReset && connector && onReset && (
        <button onClick={onReset}
          className="mt-3 inline-flex items-center gap-1 text-[11px] text-gray-400 hover:text-gray-700 transition-colors">
          <RotateCcw size={11} /> Cord replaced
        </button>
      )}
    </div>
  )
}

// ── Reset confirmation modal (step 2) ─────────────────────────────────────────
function ResetModal({
  target, onClose, onDone,
}: {
  target: ResetTarget
  onClose: () => void
  onDone: () => void
}) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr]   = useState<string | null>(null)

  async function confirm() {
    if (!note.trim()) return
    setBusy(true)
    setErr(null)
    try {
      await resetConnectorCord(target.stationId, target.connectorId, note.trim())
      onDone()
    } catch {
      setErr('Reset failed. Please try again.')
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onClose}>
      <div className="bg-white rounded-2xl shadow-lg w-full max-w-md p-6"
        onClick={e => e.stopPropagation()}>
        <div className="flex items-start justify-between mb-3">
          <h3 className="text-sm font-semibold text-gray-900 flex items-center gap-2">
            <RotateCcw size={15} className="text-amber-500" /> Confirm cord replacement
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X size={16} />
          </button>
        </div>

        <p className="text-xs text-gray-500 mb-1">
          Reset the plug-in count for:
        </p>
        <div className="text-sm font-medium text-gray-900 mb-4">
          {target.evseName} — Connector {target.connectorId}
          {target.connectorType ? ` (${target.connectorType})` : ''}
        </div>

        <div className="bg-amber-50 border border-amber-100 rounded-lg px-3 py-2 mb-4 text-xs text-amber-800">
          The current count of <strong>{target.current.toLocaleString()}</strong> plug-ins will be
          archived as the retired cord's lifetime, and the displayed count restarts at 0.
        </div>

        <label className="block text-[11px] font-medium text-gray-500 mb-1">
          Note (e.g. work order #, technician) — required
        </label>
        <textarea
          value={note}
          onChange={e => setNote(e.target.value)}
          rows={2}
          placeholder="Cord replaced under WO-…"
          className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        {err && <p className="text-xs text-red-500 mt-2">{err}</p>}

        <div className="flex justify-end gap-2 mt-4">
          <button onClick={onClose}
            className="px-3 py-1.5 text-sm text-gray-500 hover:text-gray-800 transition-colors">
            Cancel
          </button>
          <button onClick={confirm} disabled={busy || !note.trim()}
            className="px-4 py-1.5 bg-amber-600 text-white rounded-lg text-sm font-medium hover:bg-amber-700 transition-colors disabled:opacity-50">
            {busy ? 'Resetting…' : 'Confirm reset'}
          </button>
        </div>
      </div>
    </div>
  )
}
