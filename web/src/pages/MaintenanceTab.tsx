/**
 * Maintenance Tracker tab — v3.1
 *
 * Views:
 *   FleetOverview  — searchable grid of unit cards (landing view)
 *   UnitDetail     — unit header + PM history + action buttons (click-through)
 *   PMForm         — modal: dynamic PM checklist form
 *   LogRepairModal — modal: log repair / warranty / inspection / other
 *   MoveUnitModal  — modal: move unit to new site (admin)
 *   RetireUnitModal — modal: retire unit (admin)
 */

import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft, Search, RefreshCw, AlertTriangle, CheckCircle,
  Clock, MapPin, Wrench, ChevronDown, ChevronUp, X, Plus, Trash2,
  Shield, AlertCircle, Package, FileText, Star,
} from 'lucide-react'
import {
  fetchMaintenanceOverview, fetchMaintenanceUnit, fetchPMTemplate,
  submitMaintenanceRecord, updateHyperdocSubmission, moveFleetUnit,
  retireFleetUnit, patchFleetUnit, fetchSites,
  type MaintenanceUnit, type MaintenanceRecord, type PMTemplateTask,
  type TaskResult, type PartReplaced, type Site,
} from '../lib/api'

// ── Constants ─────────────────────────────────────────────────────────────────

const ADMIN_EMAIL = 'kris.hall@rechargealaska.net'

const PM_TYPE_LABELS: Record<string, string> = {
  pm_quarterly:   'Quarterly PM',
  pm_semi_annual: 'Semi-Annual PM',
  pm_annual:      'Annual PM',
  pm_general:     'General Inspection',
  repair:         'Repair',
  warranty:       'Warranty Work',
  inspection:     'Inspection',
  other:          'Other',
}

const RECORD_TYPE_COLORS: Record<string, string> = {
  pm_quarterly:   'bg-blue-50 text-blue-700',
  pm_semi_annual: 'bg-indigo-50 text-indigo-700',
  pm_annual:      'bg-violet-50 text-violet-700',
  pm_general:     'bg-gray-50 text-gray-600',
  repair:         'bg-orange-50 text-orange-700',
  warranty:       'bg-emerald-50 text-emerald-700',
  inspection:     'bg-sky-50 text-sky-700',
  other:          'bg-gray-50 text-gray-500',
}

const RESULT_COLORS: Record<string, string> = {
  pass:        'text-green-600',
  conditional: 'text-amber-600',
  fail:        'text-red-600',
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC',
  })
}

function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric',
    hour: 'numeric', minute: '2-digit', timeZone: 'America/Anchorage',
  })
}

function pmDueColor(dueDate: string | null, overdue: boolean): string {
  if (!dueDate) return 'text-gray-400'
  if (overdue) return 'text-red-600'
  const days = Math.round((new Date(dueDate).getTime() - Date.now()) / 86_400_000)
  if (days <= 30) return 'text-amber-600'
  return 'text-green-600'
}

// ── Warranty badge ─────────────────────────────────────────────────────────────

function WarrantyBadge({ unit }: { unit: MaintenanceUnit }) {
  const w = unit.warranty
  if (!w) return null

  const colorMap: Record<string, string> = {
    green: 'bg-green-50 text-green-700 border-green-200',
    amber: 'bg-amber-50 text-amber-700 border-amber-200',
    gray:  'bg-gray-100 text-gray-500 border-gray-200',
  }
  const cls = colorMap[w.color] ?? colorMap.gray

  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs font-medium border ${cls}`}>
      <Shield size={10} />
      {w.label}{w.asterisk ? ' *' : ''}
    </span>
  )
}

// ── Alert badges ──────────────────────────────────────────────────────────────

function AlertBadges({ unit }: { unit: MaintenanceUnit }) {
  return (
    <div className="flex flex-wrap gap-1 mt-1">
      {unit.service_needed && (
        <span className="px-1.5 py-0.5 rounded text-xs font-bold bg-red-100 text-red-700 border border-red-200">
          SERVICE NEEDED
        </span>
      )}
      {unit.parts_on_order && (
        <span className="px-1.5 py-0.5 rounded text-xs font-bold bg-amber-100 text-amber-700 border border-amber-200">
          PARTS ON ORDER
        </span>
      )}
      {unit.hyperdoc_pending && (
        <span className="px-1.5 py-0.5 rounded text-xs font-bold bg-blue-100 text-blue-700 border border-blue-200">
          HYPERDOC PENDING
        </span>
      )}
      {unit.pm_overdue && (
        <span className="px-1.5 py-0.5 rounded text-xs font-bold bg-red-100 text-red-700 border border-red-200">
          PM OVERDUE
        </span>
      )}
      {unit.pm_template_pending && (
        <span className="px-1.5 py-0.5 rounded text-xs font-bold bg-yellow-100 text-yellow-700 border border-yellow-200">
          PM TEMPLATE PENDING
        </span>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Unit Card
// ═══════════════════════════════════════════════════════════════════════════════

function UnitCard({ unit, onClick }: { unit: MaintenanceUnit; onClick: () => void }) {
  const nextDueColor = pmDueColor(unit.next_pm_due_date, unit.pm_overdue)
  const isRetired = unit.status === 'retired'

  return (
    <button
      onClick={onClick}
      className={`w-full text-left bg-white rounded-xl border shadow-sm hover:shadow-md hover:border-blue-300 transition-all p-4 ${
        isRetired ? 'border-gray-200 opacity-70' : 'border-gray-200'
      }`}
    >
      {/* Identity row */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="font-semibold text-gray-900 text-sm truncate">
            {unit.name}
            {isRetired && (
              <span className="ml-2 px-1.5 py-0.5 rounded text-xs bg-gray-100 text-gray-400 font-normal">
                RETIRED
              </span>
            )}
          </div>
          <div className="text-xs text-gray-400 font-mono mt-0.5 truncate">
            {unit.serial_number ?? 'S/N not recorded'}
          </div>
        </div>
        <div className="flex flex-col items-end gap-1 shrink-0">
          {unit.unit_type_name && (
            <span className="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600 font-medium">
              {unit.unit_type_name}
            </span>
          )}
          {unit.owner_name && (
            <span className="px-2 py-0.5 rounded-full text-xs bg-orange-50 text-orange-600 font-medium border border-orange-100">
              Third-Party
            </span>
          )}
        </div>
      </div>

      {/* Site row */}
      <div className="flex items-center gap-1.5 mt-2 text-xs text-gray-500">
        <MapPin size={11} />
        {unit.site_name ?? 'Site TBD'}
        {unit.network_platform && (
          <span className="ml-1 text-gray-400">· {unit.network_platform}</span>
        )}
      </div>

      {/* Warranty */}
      <div className="mt-2">
        <WarrantyBadge unit={unit} />
      </div>

      {/* PM Status */}
      <div className="mt-3 pt-3 border-t border-gray-100 grid grid-cols-2 gap-2 text-xs">
        <div>
          <div className="text-gray-400 mb-0.5">Last PM</div>
          <div className="text-gray-600">
            {unit.last_pm_annual ?? unit.last_pm_semi_annual ?? unit.last_pm_quarterly
              ? fmtDate(unit.last_pm_annual ?? unit.last_pm_semi_annual ?? unit.last_pm_quarterly)
              : 'None recorded'}
          </div>
        </div>
        <div>
          <div className="text-gray-400 mb-0.5">Next PM Due</div>
          <div className={`font-medium ${nextDueColor}`}>
            {unit.next_pm_due_date ? fmtDate(unit.next_pm_due_date) : 'Not scheduled'}
          </div>
        </div>
      </div>

      {/* Alert badges */}
      <AlertBadges unit={unit} />
    </button>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Fleet Overview
// ═══════════════════════════════════════════════════════════════════════════════

interface FleetOverviewProps {
  userEmail: string
  onSelectUnit: (id: string) => void
}

function FleetOverview({ userEmail, onSelectUnit }: FleetOverviewProps) {
  const [search, setSearch]           = useState('')
  const [siteFilter, setSiteFilter]   = useState('')
  const [showRetired, setShowRetired] = useState(false)
  const isAdmin = userEmail === ADMIN_EMAIL

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['maintenance-overview', showRetired],
    queryFn: () => fetchMaintenanceOverview(showRetired),
    refetchInterval: 60_000,
  })

  const allSites = useMemo(() => {
    const s = new Set<string>()
    data?.chargers.forEach(c => { if (c.site_name) s.add(c.site_name) })
    return Array.from(s).sort()
  }, [data])

  const filtered = useMemo(() => {
    if (!data) return []
    const q = search.toLowerCase()
    return data.chargers.filter(c => {
      if (siteFilter && c.site_name !== siteFilter) return false
      if (!q) return true
      return (
        c.name.toLowerCase().includes(q) ||
        (c.serial_number?.toLowerCase().includes(q) ?? false) ||
        (c.site_name?.toLowerCase().includes(q) ?? false)
      )
    })
  }, [data, search, siteFilter])

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20 text-sm text-gray-400">
        <RefreshCw size={16} className="animate-spin mr-2" /> Loading fleet…
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex items-center gap-2 text-red-500 text-sm py-8">
        <AlertCircle size={16} />
        Failed to load fleet data.{' '}
        <button onClick={() => refetch()} className="underline">Retry</button>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-gray-900">Fleet Maintenance Overview</h2>
          <p className="text-xs text-gray-400 mt-0.5">
            {filtered.length} unit{filtered.length !== 1 ? 's' : ''}
            {data && filtered.length !== data.chargers.length ? ` of ${data.chargers.length} total` : ''}
          </p>
        </div>
        <button
          onClick={() => refetch()}
          className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-gray-700 transition-colors"
        >
          <RefreshCw size={13} /> Refresh
        </button>
      </div>

      {/* Search and filters */}
      <div className="flex flex-wrap gap-2">
        <div className="relative flex-1 min-w-48">
          <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            placeholder="Search name or S/N…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full pl-8 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        <select
          value={siteFilter}
          onChange={e => setSiteFilter(e.target.value)}
          className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
        >
          <option value="">All sites</option>
          {allSites.map(s => <option key={s} value={s}>{s}</option>)}
        </select>

        {isAdmin && (
          <label className="flex items-center gap-2 text-sm text-gray-500 cursor-pointer select-none px-2">
            <input
              type="checkbox"
              checked={showRetired}
              onChange={e => setShowRetired(e.target.checked)}
              className="rounded"
            />
            Show retired
          </label>
        )}
      </div>

      {/* Grid */}
      {filtered.length === 0 ? (
        <div className="text-center py-12 text-gray-400 text-sm">
          {search || siteFilter ? 'No units match your filters.' : 'No units found.'}
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
          {filtered.map(unit => (
            <UnitCard key={unit.id} unit={unit} onClick={() => onSelectUnit(unit.id)} />
          ))}
        </div>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// PM Form modal
// ═══════════════════════════════════════════════════════════════════════════════

interface PMFormProps {
  chargerId: string
  chargerName: string
  pmType: string
  technicianName: string
  hyperdocRequired: boolean
  onClose: () => void
  onSubmitted: () => void
}

function PMForm({ chargerId, chargerName, pmType, technicianName, hyperdocRequired, onClose, onSubmitted }: PMFormProps) {
  const queryClient = useQueryClient()

  const { data: tmpl, isLoading } = useQuery({
    queryKey: ['pm-template', chargerId, pmType],
    queryFn: () => fetchPMTemplate(chargerId, pmType),
  })

  const [technician, setTechnician]         = useState(technicianName)
  const [firmware,   setFirmware]           = useState('')
  const [onsiteHours, setOnsiteHours]       = useState('')
  const [mobilizedHours, setMobilizedHours] = useState('')
  const [taskResults, setTaskResults]       = useState<Record<string, TaskResult>>({})
  const [addlWork, setAddlWork]             = useState(false)
  const [plannedWork, setPlannedWork]       = useState('')
  const [notes, setNotes]                   = useState('')
  const [parts, setParts]                   = useState<Partial<PartReplaced>[]>([])
  const [submitErr, setSubmitErr]           = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: submitMaintenanceRecord,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenance-overview'] })
      queryClient.invalidateQueries({ queryKey: ['maintenance-unit', chargerId] })
      onSubmitted()
    },
    onError: (e: Error) => setSubmitErr(e.message),
  })

  // Compute overall result from task results
  const overallResult = useMemo((): 'pass' | 'conditional' | 'fail' | undefined => {
    if (!tmpl?.tasks.length) return undefined
    const results = Object.values(taskResults)
    const hasFail = results.some(r => r.result_pass_fail === 'fail')
    const allDone = tmpl.tasks.filter(t => t.is_required && !t.is_conditional)
      .every(t => {
        const r = taskResults[t.id]
        if (!r) return false
        if (t.input_type === 'completed') return r.result_completed === true
        if (t.input_type === 'pass_fail' || t.input_type === 'pass_fail_action') return !!r.result_pass_fail
        if (t.input_type === 'measured_value') return !!r.result_measured_value
        return true
      })
    if (!allDone) return undefined
    if (hasFail) return 'conditional'
    return 'pass'
  }, [taskResults, tmpl])

  const hasCriticalFail = useMemo(() =>
    tmpl?.tasks.some(t => t.critical_fail && taskResults[t.id]?.result_pass_fail === 'fail') ?? false,
  [taskResults, tmpl])

  const canSubmit = !!technician.trim() && (!addlWork || !!plannedWork.trim())

  function updateTask(taskId: string, update: Partial<TaskResult>) {
    setTaskResults(prev => ({
      ...prev,
      [taskId]: { ...prev[taskId], task_id: taskId, ...update },
    }))
  }

  function addPart() {
    setParts(p => [...p, { action_taken: 'replaced' }])
  }

  function removePart(i: number) {
    setParts(p => p.filter((_, idx) => idx !== i))
  }

  function updatePart(i: number, update: Partial<PartReplaced>) {
    setParts(p => p.map((x, idx) => idx === i ? { ...x, ...update } : x))
  }

  function handleSubmit() {
    setSubmitErr(null)
    const recordTypeMap: Record<string, string> = {
      quarterly: 'pm_quarterly', semi_annual: 'pm_semi_annual', annual: 'pm_annual',
    }
    const recordType = tmpl?.fallback === 'general_inspection' ? 'pm_general' : (recordTypeMap[pmType] ?? pmType)

    mutation.mutate({
      charger_id:            chargerId,
      record_type:           recordType,
      pm_template_id:        tmpl?.template?.id ?? null,
      pm_template_version:   tmpl?.template?.template_version ?? null,
      overall_result:        overallResult ?? null,
      firmware_version:      firmware.trim() || null,
      technician_name:       technician.trim(),
      work_description:      notes.trim() || null,
      onsite_hours:          onsiteHours !== '' ? parseFloat(onsiteHours) : null,
      mobilized_hours:       mobilizedHours !== '' ? parseFloat(mobilizedHours) : null,
      additional_work_needed: addlWork,
      planned_future_work:   addlWork ? plannedWork.trim() : null,
      task_results:          Object.values(taskResults),
      parts:                 parts.filter(p => p.part_name?.trim()) as PartReplaced[],
    })
  }

  const tasks = tmpl?.tasks ?? []
  const tasksByCategory = tasks.reduce((acc, t) => {
    acc[t.task_category] ??= []
    acc[t.task_category].push(t)
    return acc
  }, {} as Record<string, PMTemplateTask[]>)

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 overflow-y-auto py-6 px-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl">
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-gray-100">
          <div>
            <h3 className="font-semibold text-gray-900">
              {PM_TYPE_LABELS[`pm_${pmType}`] ?? 'PM Form'} — {chargerName}
            </h3>
            <p className="text-xs text-gray-400 mt-0.5">
              Timestamp will be server-generated at submission and cannot be modified.
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors">
            <X size={18} />
          </button>
        </div>

        {isLoading ? (
          <div className="p-8 text-center text-sm text-gray-400">
            <RefreshCw size={16} className="animate-spin inline mr-2" /> Loading checklist…
          </div>
        ) : (
          <div className="p-6 space-y-6">
            {/* Fallback notice */}
            {tmpl?.fallback === 'general_inspection' && (
              <div className="p-3 rounded-lg bg-yellow-50 border border-yellow-200 text-xs text-yellow-700">
                No PM template is configured for this unit type. This record will be saved as a General Inspection.
                Contact your administrator to add a PM template.
              </div>
            )}

            {/* Hyperdoc notice */}
            {hyperdocRequired && (
              <div className="p-3 rounded-lg bg-blue-50 border border-blue-200 text-xs text-blue-700 flex items-start gap-2">
                <AlertCircle size={13} className="mt-0.5 shrink-0" />
                <span>
                  This unit requires Hyperdoc submission to Alpitronic. Submit PM work at{' '}
                  <strong>account.hypercharger.us</strong> with photo documentation after completing this record.
                  Failure to submit may void warranty.
                </span>
              </div>
            )}

            {/* Common header fields */}
            <div className="grid grid-cols-2 gap-4">
              <FormField label="Technician *">
                <input
                  value={technician}
                  onChange={e => setTechnician(e.target.value)}
                  className="input-base"
                  placeholder="Name"
                />
              </FormField>
              <FormField label="Firmware Version (optional)">
                <input
                  value={firmware}
                  onChange={e => setFirmware(e.target.value)}
                  className="input-base"
                  placeholder="e.g. 3.4.1"
                />
              </FormField>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <FormField label="Onsite Hours">
                <input
                  type="number"
                  min="0"
                  step="0.25"
                  value={onsiteHours}
                  onChange={e => setOnsiteHours(e.target.value)}
                  className="input-base"
                  placeholder="e.g. 2.5"
                />
              </FormField>
              <FormField label="Mobilized Hours">
                <input
                  type="number"
                  min="0"
                  step="0.25"
                  value={mobilizedHours}
                  onChange={e => setMobilizedHours(e.target.value)}
                  className="input-base"
                  placeholder="e.g. 1.0"
                />
              </FormField>
            </div>

            {/* Checklist */}
            {tasks.length > 0 && (
              <div className="space-y-4">
                {Object.entries(tasksByCategory).map(([category, catTasks]) => (
                  <div key={category}>
                    <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">
                      {category}
                    </h4>
                    <div className="space-y-3">
                      {catTasks.map(task => (
                        <TaskRow
                          key={task.id}
                          task={task}
                          result={taskResults[task.id]}
                          onChange={upd => updateTask(task.id, upd)}
                        />
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Critical fail hard stop */}
            {hasCriticalFail && (
              <div className="p-3 rounded-lg bg-red-50 border border-red-300 text-xs text-red-700 font-medium">
                HARD STOP — A critical task has failed. Do not re-energize the unit.
                Contact Alpitronic support immediately.
              </div>
            )}

            {/* Overall result display */}
            {overallResult && (
              <div className={`p-3 rounded-lg border text-sm font-medium ${
                overallResult === 'pass'        ? 'bg-green-50 border-green-200 text-green-700' :
                overallResult === 'conditional' ? 'bg-amber-50 border-amber-200 text-amber-700' :
                                                  'bg-red-50   border-red-200   text-red-700'
              }`}>
                Overall result: {overallResult.toUpperCase()}
                {overallResult === 'conditional' && ' — fails noted, unit stays in service'}
              </div>
            )}

            {/* Parts replaced */}
            <div>
              <div className="flex items-center justify-between mb-2">
                <label className="text-xs font-medium text-gray-500">Parts / Components Actioned</label>
                <button
                  onClick={addPart}
                  className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800"
                >
                  <Plus size={12} /> Add part
                </button>
              </div>
              {parts.map((p, i) => (
                <div key={i} className="flex items-start gap-2 mb-2">
                  <div className="grid grid-cols-3 gap-2 flex-1">
                    <input
                      placeholder="Part name *"
                      value={p.part_name ?? ''}
                      onChange={e => updatePart(i, { part_name: e.target.value })}
                      className="input-base col-span-1"
                    />
                    <input
                      placeholder="Part # (optional)"
                      value={p.part_number ?? ''}
                      onChange={e => updatePart(i, { part_number: e.target.value })}
                      className="input-base col-span-1"
                    />
                    <select
                      value={p.action_taken ?? 'replaced'}
                      onChange={e => updatePart(i, { action_taken: e.target.value as PartReplaced['action_taken'] })}
                      className="input-base col-span-1"
                    >
                      <option value="replaced">Replaced</option>
                      <option value="repaired">Repaired</option>
                      <option value="cleaned">Cleaned</option>
                      <option value="adjusted">Adjusted</option>
                    </select>
                  </div>
                  <button onClick={() => removePart(i)} className="text-gray-300 hover:text-red-400 mt-2">
                    <Trash2 size={14} />
                  </button>
                </div>
              ))}
            </div>

            {/* Additional work needed */}
            <div>
              <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                <input
                  type="checkbox"
                  checked={addlWork}
                  onChange={e => setAddlWork(e.target.checked)}
                  className="rounded"
                />
                Additional work needed after this PM?
              </label>
              {addlWork && (
                <textarea
                  value={plannedWork}
                  onChange={e => setPlannedWork(e.target.value)}
                  rows={3}
                  placeholder="Describe planned follow-up work (required)…"
                  className="input-base mt-2 w-full resize-none"
                  maxLength={2000}
                />
              )}
            </div>

            {/* Notes */}
            <FormField label="Notes / Observations">
              <textarea
                value={notes}
                onChange={e => setNotes(e.target.value)}
                rows={3}
                placeholder="Any additional observations…"
                className="input-base w-full resize-none"
                maxLength={2000}
              />
            </FormField>

            {submitErr && (
              <p className="text-xs text-red-500">{submitErr}</p>
            )}

            {/* Submit */}
            <div className="flex items-center justify-end gap-3 pt-2 border-t border-gray-100">
              <button
                onClick={onClose}
                className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleSubmit}
                disabled={!canSubmit || mutation.isPending}
                className="px-5 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
              >
                {mutation.isPending ? 'Saving…' : 'Submit Record'}
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

// ── Task row inside PM form ───────────────────────────────────────────────────

function TaskRow({
  task, result, onChange,
}: {
  task: PMTemplateTask
  result: TaskResult | undefined
  onChange: (u: Partial<TaskResult>) => void
}) {
  const [skipped, setSkipped] = useState(false)

  if (task.is_conditional && skipped) {
    return (
      <div className="flex items-center gap-2 text-xs text-gray-400 py-1">
        <span className="font-mono">{task.task_code}</span>
        <span className="italic">N/A — {task.conditional_label}</span>
        <button onClick={() => { setSkipped(false); onChange({ result_pass_fail: undefined }) }}
          className="underline text-blue-400 hover:text-blue-600">Undo</button>
      </div>
    )
  }

  const isCritFail = task.critical_fail && result?.result_pass_fail === 'fail'

  return (
    <div className={`rounded-lg border p-3 ${isCritFail ? 'border-red-300 bg-red-50' : 'border-gray-100 bg-gray-50'}`}>
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="flex-1">
          <span className="font-mono text-xs text-gray-400 mr-2">{task.task_code}</span>
          <span className="text-sm font-medium text-gray-800">{task.task_name}</span>
          {task.is_conditional && (
            <button
              onClick={() => { setSkipped(true); onChange({ result_pass_fail: 'na' }) }}
              className="ml-2 text-xs text-blue-500 hover:text-blue-700 underline"
            >
              Mark N/A
            </button>
          )}
        </div>
      </div>
      <p className="text-xs text-gray-500 mb-2">{task.task_description}</p>

      {/* Input */}
      {(task.input_type === 'pass_fail' || task.input_type === 'pass_fail_action') && (
        <div className="flex gap-2 flex-wrap">
          {(['pass', 'fail'] as const).map(v => (
            <button
              key={v}
              onClick={() => onChange({ result_pass_fail: v })}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg border transition-colors ${
                result?.result_pass_fail === v
                  ? v === 'pass'
                    ? 'bg-green-600 text-white border-green-600'
                    : 'bg-red-600 text-white border-red-600'
                  : 'border-gray-200 text-gray-500 hover:border-gray-400'
              }`}
            >
              {v.charAt(0).toUpperCase() + v.slice(1)}
            </button>
          ))}
          {task.input_type === 'pass_fail_action' && result?.result_pass_fail === 'pass' && (
            <label className="flex items-center gap-1.5 text-xs text-gray-600 ml-2">
              <input
                type="checkbox"
                checked={result?.result_completed ?? false}
                onChange={e => onChange({ result_completed: e.target.checked })}
              />
              Cleaned / actioned
            </label>
          )}
        </div>
      )}

      {task.input_type === 'completed' && (
        <label className="flex items-center gap-2 text-xs text-gray-700 cursor-pointer">
          <input
            type="checkbox"
            checked={result?.result_completed ?? false}
            onChange={e => onChange({ result_completed: e.target.checked })}
            className="rounded"
          />
          Completed
        </label>
      )}

      {task.input_type === 'measured_value' && (
        <div className="flex items-center gap-2">
          <input
            type="text"
            placeholder="Measured value"
            value={result?.result_measured_value ?? ''}
            onChange={e => onChange({ result_measured_value: e.target.value })}
            className="input-base w-32 text-sm"
          />
          {task.unit_of_measure && (
            <span className="text-xs text-gray-500">{task.unit_of_measure}</span>
          )}
        </div>
      )}

      {/* Task notes */}
      <input
        type="text"
        placeholder="Notes for this task (optional)"
        value={result?.task_notes ?? ''}
        onChange={e => onChange({ task_notes: e.target.value })}
        className="input-base mt-2 w-full text-xs"
        maxLength={500}
      />

      {/* Fail guidance */}
      {isCritFail && task.fail_guidance && (
        <div className="mt-2 p-2 bg-red-100 rounded text-xs text-red-700 font-medium">
          {task.fail_guidance}
        </div>
      )}
      {!isCritFail && result?.result_pass_fail === 'fail' && task.fail_guidance && (
        <div className="mt-2 p-2 bg-amber-50 rounded text-xs text-amber-700">
          {task.fail_guidance}
        </div>
      )}
    </div>
  )
}

// ── Shared form field wrapper ─────────────────────────────────────────────────

function FormField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-500 mb-1">{label}</label>
      {children}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Log Repair / Other modal
// ═══════════════════════════════════════════════════════════════════════════════

interface LogRepairModalProps {
  chargerId: string
  chargerName: string
  technicianName: string
  onClose: () => void
  onSubmitted: () => void
}

function LogRepairModal({ chargerId, chargerName, technicianName, onClose, onSubmitted }: LogRepairModalProps) {
  const queryClient = useQueryClient()
  const [recordType, setRecordType]         = useState('repair')
  const [technician, setTechnician]         = useState(technicianName)
  const [description, setDescription]      = useState('')
  const [onsiteHours, setOnsiteHours]       = useState('')
  const [mobilizedHours, setMobilizedHours] = useState('')
  const [addlWork, setAddlWork]             = useState(false)
  const [plannedWork, setPlannedWork]       = useState('')
  const [parts, setParts]                  = useState<Partial<PartReplaced>[]>([])
  const [result, setResult]                = useState<'pass' | 'conditional' | 'fail'>('pass')
  const [submitErr, setSubmitErr]          = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: submitMaintenanceRecord,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenance-overview'] })
      queryClient.invalidateQueries({ queryKey: ['maintenance-unit', chargerId] })
      onSubmitted()
    },
    onError: (e: Error) => setSubmitErr(e.message),
  })

  function handleSubmit() {
    mutation.mutate({
      charger_id: chargerId,
      record_type: recordType,
      overall_result: result,
      technician_name: technician.trim(),
      work_description: description.trim() || null,
      onsite_hours: onsiteHours !== '' ? parseFloat(onsiteHours) : null,
      mobilized_hours: mobilizedHours !== '' ? parseFloat(mobilizedHours) : null,
      additional_work_needed: addlWork,
      planned_future_work: addlWork ? plannedWork.trim() : null,
      task_results: [],
      parts: parts.filter(p => p.part_name?.trim()) as PartReplaced[],
    })
  }

  const canSubmit = !!technician.trim() && !!description.trim() && (!addlWork || !!plannedWork.trim())

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 overflow-y-auto py-6 px-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-md">
        <div className="flex items-center justify-between p-5 border-b border-gray-100">
          <h3 className="font-semibold text-gray-900">Log Work — {chargerName}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-4">
          <FormField label="Record Type">
            <select
              value={recordType}
              onChange={e => setRecordType(e.target.value)}
              className="input-base w-full"
            >
              <option value="repair">Repair</option>
              <option value="warranty">Warranty Work</option>
              <option value="inspection">Inspection</option>
              <option value="other">Other</option>
            </select>
          </FormField>

          <FormField label="Technician *">
            <input value={technician} onChange={e => setTechnician(e.target.value)} className="input-base w-full" />
          </FormField>

          <div className="grid grid-cols-2 gap-3">
            <FormField label="Onsite Hours">
              <input
                type="number"
                min="0"
                step="0.25"
                value={onsiteHours}
                onChange={e => setOnsiteHours(e.target.value)}
                className="input-base w-full"
                placeholder="e.g. 2.5"
              />
            </FormField>
            <FormField label="Mobilized Hours">
              <input
                type="number"
                min="0"
                step="0.25"
                value={mobilizedHours}
                onChange={e => setMobilizedHours(e.target.value)}
                className="input-base w-full"
                placeholder="e.g. 1.0"
              />
            </FormField>
          </div>

          <FormField label="Description *">
            <textarea
              value={description}
              onChange={e => setDescription(e.target.value)}
              rows={3}
              className="input-base w-full resize-none"
              placeholder="Describe the work performed…"
              maxLength={2000}
            />
          </FormField>

          <FormField label="Result">
            <div className="flex gap-2">
              {(['pass', 'conditional', 'fail'] as const).map(v => (
                <button
                  key={v}
                  onClick={() => setResult(v)}
                  className={`flex-1 py-1.5 text-xs font-medium rounded-lg border transition-colors ${
                    result === v
                      ? v === 'pass' ? 'bg-green-600 text-white border-green-600'
                      : v === 'fail' ? 'bg-red-600 text-white border-red-600'
                      : 'bg-amber-500 text-white border-amber-500'
                      : 'border-gray-200 text-gray-500 hover:border-gray-400'
                  }`}
                >
                  {v.charAt(0).toUpperCase() + v.slice(1)}
                </button>
              ))}
            </div>
          </FormField>

          {/* Parts */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <label className="text-xs font-medium text-gray-500">Parts</label>
              <button onClick={() => setParts(p => [...p, { action_taken: 'replaced' }])}
                className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800">
                <Plus size={12} /> Add
              </button>
            </div>
            {parts.map((p, i) => (
              <div key={i} className="flex items-start gap-2 mb-2">
                <div className="grid grid-cols-2 gap-2 flex-1">
                  <input placeholder="Part name *" value={p.part_name ?? ''} onChange={e => setParts(prev => prev.map((x, idx) => idx === i ? { ...x, part_name: e.target.value } : x))} className="input-base" />
                  <select value={p.action_taken ?? 'replaced'} onChange={e => setParts(prev => prev.map((x, idx) => idx === i ? { ...x, action_taken: e.target.value as PartReplaced['action_taken'] } : x))} className="input-base">
                    <option value="replaced">Replaced</option>
                    <option value="repaired">Repaired</option>
                    <option value="cleaned">Cleaned</option>
                    <option value="adjusted">Adjusted</option>
                  </select>
                </div>
                <button onClick={() => setParts(p => p.filter((_, idx) => idx !== i))} className="text-gray-300 hover:text-red-400 mt-2"><Trash2 size={14} /></button>
              </div>
            ))}
          </div>

          <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
            <input type="checkbox" checked={addlWork} onChange={e => setAddlWork(e.target.checked)} className="rounded" />
            Additional work needed?
          </label>
          {addlWork && (
            <textarea value={plannedWork} onChange={e => setPlannedWork(e.target.value)} rows={2} placeholder="Describe planned follow-up (required)…" className="input-base w-full resize-none" maxLength={2000} />
          )}

          {submitErr && <p className="text-xs text-red-500">{submitErr}</p>}

          <div className="flex items-center justify-end gap-3 pt-2 border-t border-gray-100">
            <button onClick={onClose} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">Cancel</button>
            <button onClick={handleSubmit} disabled={!canSubmit || mutation.isPending} className="px-5 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50">
              {mutation.isPending ? 'Saving…' : 'Save Record'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Move Unit modal
// ═══════════════════════════════════════════════════════════════════════════════

function MoveUnitModal({ chargerId, chargerName, onClose, onMoved }: {
  chargerId: string; chargerName: string; onClose: () => void; onMoved: () => void
}) {
  const queryClient = useQueryClient()
  const [siteId, setSiteId] = useState('')
  const [notes,  setNotes]  = useState('')
  const [err,    setErr]    = useState<string | null>(null)

  const { data: sites = [] } = useQuery({ queryKey: ['sites'], queryFn: fetchSites })

  const mutation = useMutation({
    mutationFn: () => moveFleetUnit(chargerId, { site_id: siteId, notes: notes.trim() || null }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenance-overview'] })
      queryClient.invalidateQueries({ queryKey: ['maintenance-unit', chargerId] })
      onMoved()
    },
    onError: (e: Error) => setErr(e.message),
  })

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm">
        <div className="flex items-center justify-between p-5 border-b border-gray-100">
          <h3 className="font-semibold text-gray-900">Move Unit — {chargerName}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-4">
          <FormField label="New Site *">
            <select value={siteId} onChange={e => setSiteId(e.target.value)} className="input-base w-full">
              <option value="">Select site…</option>
              {sites.map((s: Site) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </FormField>
          <FormField label="Notes (optional)">
            <input value={notes} onChange={e => setNotes(e.target.value)} className="input-base w-full" placeholder="Reason for move…" />
          </FormField>
          {err && <p className="text-xs text-red-500">{err}</p>}
          <div className="flex justify-end gap-3 pt-2 border-t border-gray-100">
            <button onClick={onClose} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">Cancel</button>
            <button onClick={() => mutation.mutate()} disabled={!siteId || mutation.isPending} className="px-5 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50">
              {mutation.isPending ? 'Moving…' : 'Move Unit'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Retire Unit modal
// ═══════════════════════════════════════════════════════════════════════════════

function RetireUnitModal({ chargerId, chargerName, onClose, onRetired }: {
  chargerId: string; chargerName: string; onClose: () => void; onRetired: () => void
}) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const [err,    setErr]    = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => retireFleetUnit(chargerId, reason.trim()),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenance-overview'] })
      onRetired()
    },
    onError: (e: Error) => setErr(e.message),
  })

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4">
      <div className="bg-white rounded-2xl shadow-2xl w-full max-w-sm">
        <div className="flex items-center justify-between p-5 border-b border-gray-100">
          <h3 className="font-semibold text-gray-900 text-red-700">Retire Unit — {chargerName}</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600"><X size={18} /></button>
        </div>
        <div className="p-5 space-y-4">
          <p className="text-xs text-gray-500">
            Retiring removes the unit from the active fleet view. All maintenance records are permanently preserved.
            This action cannot be undone via the UI.
          </p>
          <FormField label="Retirement Reason *">
            <textarea value={reason} onChange={e => setReason(e.target.value)} rows={3} className="input-base w-full resize-none" placeholder="Explain why this unit is being retired…" maxLength={2000} />
          </FormField>
          {err && <p className="text-xs text-red-500">{err}</p>}
          <div className="flex justify-end gap-3 pt-2 border-t border-gray-100">
            <button onClick={onClose} className="px-4 py-2 text-sm text-gray-500 hover:text-gray-700">Cancel</button>
            <button onClick={() => mutation.mutate()} disabled={!reason.trim() || mutation.isPending} className="px-5 py-2 bg-red-600 text-white text-sm font-medium rounded-lg hover:bg-red-700 disabled:opacity-50">
              {mutation.isPending ? 'Retiring…' : 'Retire Unit'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Hyperdoc update inline form
// ═══════════════════════════════════════════════════════════════════════════════

function HyperdocRow({ record, chargerId }: { record: MaintenanceRecord; chargerId: string }) {
  const queryClient = useQueryClient()
  const [date, setDate] = useState('')
  const [err, setErr]   = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () => updateHyperdocSubmission(record.id, date),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenance-unit', chargerId] })
      queryClient.invalidateQueries({ queryKey: ['maintenance-overview'] })
    },
    onError: (e: Error) => setErr(e.message),
  })

  if (!record.hyperdoc_required) return null

  return (
    <div className="mt-2 p-2 bg-blue-50 rounded-lg border border-blue-100 flex items-center gap-3 flex-wrap">
      <span className="text-xs text-blue-700 font-medium">
        {record.hyperdoc_submitted
          ? `Hyperdoc submitted: ${fmtDate(record.hyperdoc_submitted_at)}`
          : 'Hyperdoc: Pending'}
      </span>
      {!record.hyperdoc_submitted && (
        <>
          <input type="date" value={date} onChange={e => setDate(e.target.value)} className="input-base text-xs" />
          <button onClick={() => mutation.mutate()} disabled={!date || mutation.isPending} className="px-2 py-1 text-xs bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50">
            {mutation.isPending ? '…' : 'Mark Submitted'}
          </button>
          {err && <span className="text-xs text-red-500">{err}</span>}
        </>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Unit Detail view
// ═══════════════════════════════════════════════════════════════════════════════

type ModalType = 'pm_quarterly' | 'pm_semi_annual' | 'pm_annual' | 'log_repair' | 'move_unit' | 'retire_unit' | null

interface UnitDetailProps {
  chargerId: string
  userEmail: string
  onBack: () => void
}

function UnitDetail({ chargerId, userEmail, onBack }: UnitDetailProps) {
  const isAdmin = userEmail === ADMIN_EMAIL
  const queryClient = useQueryClient()

  const [modal, setModal]               = useState<ModalType>(null)
  const [expandedRecord, setExpanded]   = useState<string | null>(null)
  const [partsOrderPatch, setPOP]       = useState<boolean | null>(null)

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['maintenance-unit', chargerId],
    queryFn: () => fetchMaintenanceUnit(chargerId),
  })

  const patchMutation = useMutation({
    mutationFn: (v: boolean) => patchFleetUnit(chargerId, { parts_on_order: v }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['maintenance-unit', chargerId] })
      queryClient.invalidateQueries({ queryKey: ['maintenance-overview'] })
    },
  })

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20 text-sm text-gray-400">
        <RefreshCw size={16} className="animate-spin mr-2" /> Loading unit…
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="flex items-center gap-2 text-red-500 text-sm py-8">
        <AlertCircle size={16} />
        Failed to load unit details.{' '}
        <button onClick={() => refetch()} className="underline">Retry</button>
      </div>
    )
  }

  const { charger: c, maintenance_records: records, location_history } = data

  const techName = userEmail.split('@')[0] ?? 'Technician'
  const isRetired = c.status === 'retired'

  // Determine if PM template exists (used to decide if annual/SA/Q buttons are available)
  const hasIntervalPM = !c.pm_template_pending

  return (
    <div className="space-y-4 max-w-4xl">
      {/* Back */}
      <button
        onClick={onBack}
        className="flex items-center gap-1.5 text-sm text-gray-500 hover:text-gray-800 transition-colors"
      >
        <ArrowLeft size={14} /> Fleet Overview
      </button>

      {/* Unit header */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div>
            <h2 className="text-lg font-bold text-gray-900">{c.name}</h2>
            <div className="text-sm text-gray-500 font-mono mt-0.5">{c.serial_number ?? 'S/N not recorded'}</div>
            <div className="flex flex-wrap items-center gap-2 mt-2">
              {c.unit_type_name && (
                <span className="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-600 font-medium">
                  {c.unit_type_name}
                </span>
              )}
              {c.site_name && (
                <span className="flex items-center gap-1 text-xs text-gray-500">
                  <MapPin size={10} /> {c.site_name}
                </span>
              )}
              {c.network_platform && (
                <span className="text-xs text-gray-400">{c.network_platform}</span>
              )}
              {c.owner_name && (
                <span className="px-2 py-0.5 rounded-full text-xs bg-orange-50 text-orange-600 border border-orange-100">
                  Owner: {c.owner_name}
                </span>
              )}
              {isRetired && (
                <span className="px-2 py-0.5 rounded-full text-xs bg-gray-100 text-gray-400 font-semibold">
                  RETIRED
                </span>
              )}
            </div>
          </div>
          <div className="flex flex-col items-end gap-2">
            <WarrantyBadge unit={c as MaintenanceUnit} />
            {c.warranty_end && (
              <span className="text-xs text-gray-400">Expires {fmtDate(c.warranty_end)}</span>
            )}
            {c.warranty_notes && (
              <span className="text-xs text-gray-400 italic max-w-48 text-right">{c.warranty_notes}</span>
            )}
          </div>
        </div>

        {/* PM status panel */}
        <div className="mt-4 pt-4 border-t border-gray-100 grid grid-cols-1 sm:grid-cols-3 gap-3">
          {[
            { label: 'Quarterly PM', last: c.last_pm_quarterly, next: c.next_pm_quarterly_due, months: c.interval_quarterly_months },
            { label: 'Semi-Annual PM', last: c.last_pm_semi_annual, next: c.next_pm_semi_annual_due, months: c.interval_semiannual_months },
            { label: 'Annual PM', last: c.last_pm_annual, next: c.next_pm_annual_due, months: c.interval_annual_months },
          ].map(({ label, last, next, months }) => {
            if (!months) return null
            const overdue = next ? next < new Date().toISOString().slice(0, 10) : false
            return (
              <div key={label} className="bg-gray-50 rounded-lg p-3 text-xs">
                <div className="font-medium text-gray-600 mb-1">{label}</div>
                <div className="text-gray-500">Last: {last ? fmtDate(last) : 'None'}</div>
                <div className={`font-medium mt-0.5 ${pmDueColor(next, overdue)}`}>
                  Next: {next ? fmtDate(next) : 'Not scheduled'}
                </div>
              </div>
            )
          })}
        </div>

        {/* Alert badges */}
        <div className="mt-3">
          <AlertBadges unit={c as MaintenanceUnit} />
        </div>
      </div>

      {/* Action buttons (admin only, active units) */}
      {isAdmin && !isRetired && (
        <div className="flex flex-wrap gap-2">
          {(c.interval_quarterly_months) && (
            <ActionBtn
              label="Start Quarterly PM"
              color="blue"
              onClick={() => setModal('pm_quarterly')}
            />
          )}
          {(c.interval_semiannual_months) && (
            <ActionBtn
              label="Start Semi-Annual PM"
              color="blue"
              onClick={() => setModal('pm_semi_annual')}
            />
          )}
          <ActionBtn
            label="Start Annual PM"
            color="blue"
            onClick={() => setModal('pm_annual')}
          />
          <ActionBtn label="Log Repair / Other" color="gray" onClick={() => setModal('log_repair')} />
          <ActionBtn label="Move Unit" color="gray" onClick={() => setModal('move_unit')} />
          <ActionBtn
            label={c.parts_on_order ? 'Clear Parts on Order' : 'Mark Parts on Order'}
            color={c.parts_on_order ? 'amber' : 'gray'}
            onClick={() => patchMutation.mutate(!c.parts_on_order)}
          />
          <ActionBtn label="Retire Unit" color="red" onClick={() => setModal('retire_unit')} />
        </div>
      )}

      {/* Maintenance history */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm">
        <div className="px-5 py-4 border-b border-gray-100">
          <h3 className="font-semibold text-gray-800 text-sm">Maintenance History</h3>
        </div>
        {records.length === 0 ? (
          <div className="p-8 text-center text-sm text-gray-400">No maintenance records yet.</div>
        ) : (
          <div className="divide-y divide-gray-50">
            {records.map(r => (
              <RecordRow
                key={r.id}
                record={r}
                chargerId={chargerId}
                isAdmin={isAdmin}
                expanded={expandedRecord === r.id}
                onToggle={() => setExpanded(expandedRecord === r.id ? null : r.id)}
              />
            ))}
          </div>
        )}
      </div>

      {/* Location history */}
      {location_history.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm">
          <div className="px-5 py-4 border-b border-gray-100">
            <h3 className="font-semibold text-gray-800 text-sm">Location History</h3>
          </div>
          <div className="divide-y divide-gray-50">
            {location_history.map(lh => (
              <div key={lh.id} className="px-5 py-3 flex items-center justify-between text-sm">
                <span className="text-gray-700">{lh.site_name ?? '—'}</span>
                <span className="text-xs text-gray-400">{fmtDateTime(lh.assigned_at)}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Modals */}
      {(modal === 'pm_quarterly' || modal === 'pm_semi_annual' || modal === 'pm_annual') && (
        <PMForm
          chargerId={chargerId}
          chargerName={c.name}
          pmType={modal.replace('pm_', '')}
          technicianName={techName}
          hyperdocRequired={c.hyperdoc_required}
          onClose={() => setModal(null)}
          onSubmitted={() => { setModal(null); refetch() }}
        />
      )}
      {modal === 'log_repair' && (
        <LogRepairModal
          chargerId={chargerId}
          chargerName={c.name}
          technicianName={techName}
          onClose={() => setModal(null)}
          onSubmitted={() => { setModal(null); refetch() }}
        />
      )}
      {modal === 'move_unit' && (
        <MoveUnitModal
          chargerId={chargerId}
          chargerName={c.name}
          onClose={() => setModal(null)}
          onMoved={() => { setModal(null); refetch() }}
        />
      )}
      {modal === 'retire_unit' && (
        <RetireUnitModal
          chargerId={chargerId}
          chargerName={c.name}
          onClose={() => setModal(null)}
          onRetired={() => { setModal(null); onBack() }}
        />
      )}
    </div>
  )
}

// ── Action button helper ──────────────────────────────────────────────────────

function ActionBtn({ label, color, onClick }: {
  label: string; color: 'blue' | 'gray' | 'red' | 'amber'; onClick: () => void
}) {
  const cls = {
    blue:  'bg-blue-600 text-white hover:bg-blue-700',
    gray:  'bg-white text-gray-700 border border-gray-200 hover:bg-gray-50',
    red:   'bg-white text-red-600 border border-red-200 hover:bg-red-50',
    amber: 'bg-white text-amber-600 border border-amber-200 hover:bg-amber-50',
  }[color]
  return (
    <button onClick={onClick} className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors ${cls}`}>
      {label}
    </button>
  )
}

// ── Maintenance record row ────────────────────────────────────────────────────

function RecordRow({ record: r, chargerId, isAdmin, expanded, onToggle }: {
  record: MaintenanceRecord; chargerId: string; isAdmin: boolean; expanded: boolean; onToggle: () => void
}) {
  const typeLabel = PM_TYPE_LABELS[r.record_type] ?? r.record_type
  const typeColor = RECORD_TYPE_COLORS[r.record_type] ?? 'bg-gray-50 text-gray-500'

  return (
    <div>
      <button onClick={onToggle} className="w-full px-5 py-3 flex items-center justify-between hover:bg-gray-50 transition-colors text-left">
        <div className="flex items-center gap-3 flex-1 min-w-0">
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium shrink-0 ${typeColor}`}>
            {typeLabel}
          </span>
          <span className="text-xs text-gray-500 shrink-0">{fmtDateTime(r.record_timestamp)}</span>
          <span className="text-xs text-gray-600 truncate">{r.technician_name}</span>
          {r.overall_result && (
            <span className={`text-xs font-semibold ${RESULT_COLORS[r.overall_result] ?? ''}`}>
              {r.overall_result.toUpperCase()}
            </span>
          )}
          {r.additional_work_needed && (
            <span className="px-1.5 py-0.5 rounded text-xs bg-orange-50 text-orange-600 border border-orange-100 font-medium">
              Follow-up needed
            </span>
          )}
        </div>
        {expanded ? <ChevronUp size={14} className="text-gray-400 shrink-0" /> : <ChevronDown size={14} className="text-gray-400 shrink-0" />}
      </button>

      {expanded && (
        <div className="px-5 pb-4 space-y-2 text-sm bg-gray-50 border-t border-gray-100">
          {r.work_description && (
            <p className="text-gray-600 text-xs pt-3">{r.work_description}</p>
          )}
          {(r.onsite_hours != null || r.mobilized_hours != null) && (
            <div className="flex gap-4 text-xs text-gray-600 pt-1">
              {r.onsite_hours != null && (
                <span><span className="font-medium text-gray-500">Onsite:</span> {r.onsite_hours} hr{r.onsite_hours !== 1 ? 's' : ''}</span>
              )}
              {r.mobilized_hours != null && (
                <span><span className="font-medium text-gray-500">Mobilized:</span> {r.mobilized_hours} hr{r.mobilized_hours !== 1 ? 's' : ''}</span>
              )}
            </div>
          )}
          {r.planned_future_work && (
            <div className="p-2 rounded bg-orange-50 border border-orange-100 text-xs text-orange-700">
              <strong>Planned follow-up:</strong> {r.planned_future_work}
            </div>
          )}
          {r.parts.length > 0 && (
            <div>
              <div className="text-xs font-medium text-gray-500 mb-1">Parts / Actions</div>
              <div className="space-y-0.5">
                {r.parts.map((p, i) => (
                  <div key={i} className="text-xs text-gray-600 flex gap-2">
                    <span className="font-medium">{p.part_name}</span>
                    {p.part_number && <span className="text-gray-400">#{p.part_number}</span>}
                    <span className="text-gray-400 capitalize">{p.action_taken}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {isAdmin && <HyperdocRow record={r} chargerId={chargerId} />}
        </div>
      )}
    </div>
  )
}

// ═══════════════════════════════════════════════════════════════════════════════
// Main export
// ═══════════════════════════════════════════════════════════════════════════════

interface MaintenanceTabProps {
  userEmail: string
}

export function MaintenanceTab({ userEmail }: MaintenanceTabProps) {
  const [selectedChargerId, setSelectedChargerId] = useState<string | null>(null)

  return (
    <div className="space-y-4">
      {selectedChargerId ? (
        <UnitDetail
          chargerId={selectedChargerId}
          userEmail={userEmail}
          onBack={() => setSelectedChargerId(null)}
        />
      ) : (
        <FleetOverview
          userEmail={userEmail}
          onSelectUnit={setSelectedChargerId}
        />
      )}
    </div>
  )
}
