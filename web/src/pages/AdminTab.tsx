/**
 * Admin tab — EVSE & Locations · Users · Pricing
 * Only rendered for kris.hall@rechargealaska.net (enforced in App.tsx + backend).
 */

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronUp, RefreshCw, CheckCircle, AlertCircle } from 'lucide-react'
import {
  fetchAdminUsers,    createAdminUser,    updateAdminUser,
  fetchAdminPricing,  createAdminPricing, updateAdminPricing,
  fetchAdminEvse,     fetchAdminUnidentifiedEvse, upsertAdminEvse,
  fetchUtilityAccounts, createUtilityAccount, updateUtilityAccount, deleteUtilityAccount,
  fetchUtilityCredentials, upsertUtilityCredentials, triggerUtilityCollect,
  UTILITY_LABELS,
  type AdminUser, type AdminPricing, type AdminEvse, type AdminUnidentifiedEvse,
  type UtilityAccount, type UtilityCredential, type UtilityName,
} from '../lib/api'

// ── Constants ─────────────────────────────────────────────────────────────────
const PLATFORMS = ['', 'RTM', 'RT50', 'MaxiCharger', 'Autel', 'Other']

// ── Helpers ───────────────────────────────────────────────────────────────────

function fmtDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}

function fmtMoney(v: number | null | undefined): string {
  if (v == null) return '—'
  return `$${v.toFixed(4)}`
}

/** Return current Alaska local date-time as "YYYY-MM-DDTHH:MM" for datetime-local inputs */
function nowAKLocal(): string {
  const now = new Date()
  const ak  = new Date(now.toLocaleString('en-US', { timeZone: 'America/Anchorage' }))
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${ak.getFullYear()}-${pad(ak.getMonth() + 1)}-${pad(ak.getDate())}` +
    `T${pad(ak.getHours())}:${pad(ak.getMinutes())}`
  )
}

/** Convert a datetime-local value (AK) to ISO8601 with -09:00 offset */
function akLocalToISO(val: string): string {
  if (!val) return ''
  // datetime-local gives "YYYY-MM-DDTHH:MM" — append AK standard offset
  // Use -09:00 (AKST); DST difference is handled server-side / display only
  return `${val}:00-09:00`
}

// ── Section wrapper ───────────────────────────────────────────────────────────

function Section({
  title, children, defaultOpen = true,
}: { title: string; children: React.ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-6 py-4 text-left hover:bg-gray-50 transition-colors"
      >
        <span className="font-semibold text-gray-800 text-sm">{title}</span>
        {open ? <ChevronUp size={16} className="text-gray-400" /> : <ChevronDown size={16} className="text-gray-400" />}
      </button>
      {open && <div className="px-6 pb-6 space-y-5">{children}</div>}
    </div>
  )
}

// ── Input helpers ─────────────────────────────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-500 mb-1">{label}</label>
      {children}
    </div>
  )
}

const inputCls = 'w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'
const selectCls = inputCls
const btnPrimary = 'px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold rounded-lg transition-colors disabled:opacity-50'
const btnSecondary = 'px-4 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 text-sm font-medium rounded-lg transition-colors'
const btnDanger = 'px-3 py-1.5 bg-red-50 hover:bg-red-100 text-red-600 text-xs font-medium rounded-lg transition-colors'
const btnGreen  = 'px-3 py-1.5 bg-green-50 hover:bg-green-100 text-green-700 text-xs font-medium rounded-lg transition-colors'

function StatusMsg({ ok, msg }: { ok: boolean; msg: string }) {
  if (!msg) return null
  return (
    <div className={`flex items-center gap-2 text-xs ${ok ? 'text-green-700' : 'text-red-600'}`}>
      {ok ? <CheckCircle size={13} /> : <AlertCircle size={13} />}
      {msg}
    </div>
  )
}

// ════════════════════════════════════════════════════════════════════════════════
// EVSE Section
// ════════════════════════════════════════════════════════════════════════════════

function EvseSection() {
  const qc = useQueryClient()
  const { data: evseList  = [], isLoading: loadingEvse }  = useQuery({ queryKey: ['admin-evse'], queryFn: fetchAdminEvse })
  const { data: unidList  = [], isLoading: loadingUnid, refetch: refetchUnid }  = useQuery({ queryKey: ['admin-unidentified'], queryFn: fetchAdminUnidentifiedEvse })

  const [form, setForm] = useState({ station_id: '', display_name: '', location: '', platform: '', archived: false })
  const [status, setStatus] = useState({ ok: true, msg: '' })

  const upsert = useMutation({
    mutationFn: upsertAdminEvse,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-evse'] })
      setStatus({ ok: true, msg: 'Saved.' })
      setForm({ station_id: '', display_name: '', location: '', platform: '', archived: false })
    },
    onError: (e: Error) => setStatus({ ok: false, msg: e.message }),
  })

  function prefill(row: AdminEvse) {
    setForm({ station_id: row.station_id, display_name: row.display_name, location: row.location, platform: row.platform, archived: row.archived })
    setStatus({ ok: true, msg: '' })
  }

  function prefillUnid(sid: string) {
    setForm(f => ({ ...f, station_id: sid }))
    setStatus({ ok: true, msg: '' })
  }

  return (
    <>
      {/* Unidentified */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Unidentified EVSEs</h3>
          <button onClick={() => refetchUnid()} className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600">
            <RefreshCw size={12} /> Refresh
          </button>
        </div>
        {loadingUnid ? (
          <div className="h-10 bg-gray-100 rounded animate-pulse" />
        ) : unidList.length === 0 ? (
          <p className="text-xs text-green-600">✓ No unidentified EVSEs</p>
        ) : (
          <table className="w-full text-xs border border-gray-200 rounded-lg overflow-hidden">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Station ID</th>
                <th className="px-3 py-2 text-left font-medium text-gray-500">Last Seen (AK)</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody>
              {unidList.map((r: AdminUnidentifiedEvse) => (
                <tr key={r.station_id} className="border-t border-gray-100">
                  <td className="px-3 py-2 font-mono text-gray-700">{r.station_id}</td>
                  <td className="px-3 py-2 text-gray-500">{r.last_seen_ak}</td>
                  <td className="px-3 py-2">
                    <button onClick={() => prefillUnid(r.station_id)} className={btnGreen}>Register</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Registered table */}
      <div>
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">Registered EVSEs</h3>
        {loadingEvse ? (
          <div className="h-24 bg-gray-100 rounded animate-pulse" />
        ) : (
          <table className="w-full text-xs border border-gray-200 rounded-lg overflow-hidden">
            <thead className="bg-gray-50">
              <tr>
                {['Station ID', 'Display Name', 'Location', 'Platform', 'Archived', ''].map(h => (
                  <th key={h} className="px-3 py-2 text-left font-medium text-gray-500">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {evseList.map((r: AdminEvse) => (
                <tr key={r.station_id} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-3 py-2 font-mono text-gray-600">{r.station_id}</td>
                  <td className="px-3 py-2 font-medium">{r.display_name}</td>
                  <td className="px-3 py-2 text-gray-500">{r.location}</td>
                  <td className="px-3 py-2 text-gray-500">{r.platform}</td>
                  <td className="px-3 py-2">{r.archived ? <span className="text-orange-500">Yes</span> : <span className="text-gray-400">No</span>}</td>
                  <td className="px-3 py-2">
                    <button onClick={() => prefill(r)} className={btnSecondary + ' text-xs px-2 py-1'}>Edit</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* Add / Update form */}
      <div className="border border-gray-200 rounded-xl p-4 space-y-3">
        <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Add / Update EVSE</h3>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Station ID *">
            <input className={inputCls} placeholder="as_LYHe6m…" value={form.station_id} onChange={e => setForm(f => ({ ...f, station_id: e.target.value }))} />
          </Field>
          <Field label="Display Name">
            <input className={inputCls} placeholder="Glennallen" value={form.display_name} onChange={e => setForm(f => ({ ...f, display_name: e.target.value }))} />
          </Field>
          <Field label="Location">
            <input className={inputCls} placeholder="Glennallen" value={form.location} onChange={e => setForm(f => ({ ...f, location: e.target.value }))} />
          </Field>
          <Field label="Platform">
            <select className={selectCls} value={form.platform} onChange={e => setForm(f => ({ ...f, platform: e.target.value }))}>
              {PLATFORMS.map(p => <option key={p} value={p}>{p || '— select —'}</option>)}
            </select>
          </Field>
        </div>
        <label className="flex items-center gap-2 text-sm text-gray-600">
          <input type="checkbox" checked={form.archived} onChange={e => setForm(f => ({ ...f, archived: e.target.checked }))} />
          Archive this EVSE
        </label>
        <div className="flex items-center gap-3">
          <button className={btnPrimary} disabled={!form.station_id || upsert.isPending} onClick={() => { setStatus({ ok: true, msg: '' }); upsert.mutate(form) }}>
            {upsert.isPending ? 'Saving…' : 'Save EVSE'}
          </button>
          <StatusMsg {...status} />
        </div>
      </div>
    </>
  )
}

// ════════════════════════════════════════════════════════════════════════════════
// Users Section
// ════════════════════════════════════════════════════════════════════════════════

function UsersSection() {
  const qc = useQueryClient()
  const { data: evseList = [] } = useQuery({ queryKey: ['admin-evse'], queryFn: fetchAdminEvse })
  const { data: users = [], isLoading } = useQuery({ queryKey: ['admin-users'], queryFn: fetchAdminUsers })

  const [form, setForm] = useState<{
    id: string; email: string; name: string; allowed_evse_ids: string[]; active: boolean; mode: 'create' | 'update'
  }>({ id: '', email: '', name: '', allowed_evse_ids: [], active: true, mode: 'create' })

  const [status, setStatus] = useState({ ok: true, msg: '' })

  const saveUser = useMutation({
    mutationFn: async () => {
      const evses = form.allowed_evse_ids.length > 0 ? form.allowed_evse_ids : null
      if (form.mode === 'create') {
        return createAdminUser({ email: form.email, name: form.name, allowed_evse_ids: evses, active: form.active })
      } else {
        return updateAdminUser(form.id, { email: form.email, name: form.name, allowed_evse_ids: evses, active: form.active })
      }
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-users'] })
      setStatus({ ok: true, msg: form.mode === 'create' ? 'User created.' : 'User updated.' })
      resetForm()
    },
    onError: (e: Error) => setStatus({ ok: false, msg: e.message }),
  })

  const deactivate = useMutation({
    mutationFn: (id: string) => updateAdminUser(id, { active: false }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['admin-users'] }); setStatus({ ok: true, msg: 'Deactivated.' }) },
    onError: (e: Error) => setStatus({ ok: false, msg: e.message }),
  })

  function resetForm() {
    setForm({ id: '', email: '', name: '', allowed_evse_ids: [], active: true, mode: 'create' })
  }

  function editUser(u: AdminUser) {
    setForm({
      id: u.id, email: u.email, name: u.name ?? '',
      allowed_evse_ids: u.allowed_evse_ids ?? [],
      active: u.active, mode: 'update',
    })
    setStatus({ ok: true, msg: '' })
  }

  function toggleEvse(sid: string) {
    setForm(f => ({
      ...f,
      allowed_evse_ids: f.allowed_evse_ids.includes(sid)
        ? f.allowed_evse_ids.filter(x => x !== sid)
        : [...f.allowed_evse_ids, sid],
    }))
  }

  return (
    <>
      {/* Users table */}
      {isLoading ? (
        <div className="h-24 bg-gray-100 rounded animate-pulse" />
      ) : (
        <table className="w-full text-xs border border-gray-200 rounded-lg overflow-hidden">
          <thead className="bg-gray-50">
            <tr>
              {['Email', 'Name', 'Active', 'Allowed EVSEs', ''].map(h => (
                <th key={h} className="px-3 py-2 text-left font-medium text-gray-500">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {users.map((u: AdminUser) => (
              <tr key={u.id} className="border-t border-gray-100 hover:bg-gray-50">
                <td className="px-3 py-2 font-medium text-gray-800">{u.email}</td>
                <td className="px-3 py-2 text-gray-500">{u.name || '—'}</td>
                <td className="px-3 py-2">
                  <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${u.active ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'}`}>
                    {u.active ? 'Active' : 'Inactive'}
                  </span>
                </td>
                <td className="px-3 py-2 text-gray-500 max-w-xs truncate">
                  {u.allowed_evse_ids == null ? 'All' : u.allowed_evse_ids.length === 0 ? 'None' : u.allowed_evse_ids.length + ' EVSEs'}
                </td>
                <td className="px-3 py-2">
                  <div className="flex gap-1">
                    <button onClick={() => editUser(u)} className={btnSecondary + ' text-xs px-2 py-1'}>Edit</button>
                    {u.active && (
                      <button onClick={() => deactivate.mutate(u.id)} className={btnDanger}>Deactivate</button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {/* Add / Update form */}
      <div className="border border-gray-200 rounded-xl p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
            {form.mode === 'create' ? 'Add New User' : `Editing: ${form.email}`}
          </h3>
          {form.mode === 'update' && (
            <button onClick={resetForm} className={btnSecondary + ' text-xs px-2 py-1'}>+ New</button>
          )}
        </div>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Email *">
            <input className={inputCls} type="email" placeholder="user@example.com" value={form.email} onChange={e => setForm(f => ({ ...f, email: e.target.value }))} />
          </Field>
          <Field label="Name">
            <input className={inputCls} placeholder="Full Name" value={form.name} onChange={e => setForm(f => ({ ...f, name: e.target.value }))} />
          </Field>
        </div>

        <Field label="Allowed EVSEs (leave all unchecked = full access)">
          <div className="grid grid-cols-2 gap-1.5 mt-1">
            {evseList.map((e: AdminEvse) => (
              <label key={e.station_id} className="flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
                <input
                  type="checkbox"
                  checked={form.allowed_evse_ids.includes(e.station_id)}
                  onChange={() => toggleEvse(e.station_id)}
                />
                {e.display_name || e.station_id}
              </label>
            ))}
          </div>
        </Field>

        <label className="flex items-center gap-2 text-sm text-gray-600">
          <input type="checkbox" checked={form.active} onChange={e => setForm(f => ({ ...f, active: e.target.checked }))} />
          Active
        </label>

        <div className="flex items-center gap-3">
          <button className={btnPrimary} disabled={!form.email || saveUser.isPending} onClick={() => { setStatus({ ok: true, msg: '' }); saveUser.mutate() }}>
            {saveUser.isPending ? 'Saving…' : form.mode === 'create' ? 'Create User' : 'Update User'}
          </button>
          <StatusMsg {...status} />
        </div>
      </div>
    </>
  )
}

// ════════════════════════════════════════════════════════════════════════════════
// Pricing Section
// ════════════════════════════════════════════════════════════════════════════════

function PricingSection() {
  const qc = useQueryClient()
  const { data: evseList  = [] } = useQuery({ queryKey: ['admin-evse'], queryFn: fetchAdminEvse })
  const { data: pricing   = [], isLoading } = useQuery({ queryKey: ['admin-pricing'], queryFn: fetchAdminPricing })

  const defaultForm = () => ({
    id: '', station_id: '', connection_fee: '', price_per_kwh: '', price_per_min: '',
    idle_fee_per_min: '', effective_start: nowAKLocal(), effective_end: '',
    mode: 'create' as 'create' | 'update',
  })
  const [form, setForm] = useState(defaultForm)
  const [status, setStatus] = useState({ ok: true, msg: '' })

  // friendly name lookup
  const evseMap = Object.fromEntries(evseList.map((e: AdminEvse) => [e.station_id, e.display_name]))

  const savePricing = useMutation({
    mutationFn: async () => {
      const payload = {
        station_id:       form.station_id,
        connection_fee:   form.connection_fee   ? parseFloat(form.connection_fee)   : null,
        price_per_kwh:    form.price_per_kwh    ? parseFloat(form.price_per_kwh)    : null,
        price_per_min:    form.price_per_min    ? parseFloat(form.price_per_min)    : null,
        idle_fee_per_min: form.idle_fee_per_min ? parseFloat(form.idle_fee_per_min) : null,
        effective_start:  akLocalToISO(form.effective_start),
        effective_end:    form.effective_end ? akLocalToISO(form.effective_end) : null,
      }
      if (form.mode === 'create') return createAdminPricing(payload as Parameters<typeof createAdminPricing>[0])
      return updateAdminPricing(form.id, payload)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['admin-pricing'] })
      setStatus({ ok: true, msg: form.mode === 'create' ? 'Pricing rule created.' : 'Pricing rule updated.' })
      setForm(defaultForm())
    },
    onError: (e: Error) => setStatus({ ok: false, msg: e.message }),
  })

  function editPricing(r: AdminPricing) {
    setForm({
      id: r.id,
      station_id: r.station_id,
      connection_fee:   r.connection_fee   != null ? String(r.connection_fee)   : '',
      price_per_kwh:    r.price_per_kwh    != null ? String(r.price_per_kwh)    : '',
      price_per_min:    r.price_per_min    != null ? String(r.price_per_min)    : '',
      idle_fee_per_min: r.idle_fee_per_min != null ? String(r.idle_fee_per_min) : '',
      effective_start: r.effective_start
        ? new Date(r.effective_start).toLocaleString('sv-SE', { timeZone: 'America/Anchorage' }).replace(' ', 'T').slice(0, 16)
        : nowAKLocal(),
      effective_end: r.effective_end
        ? new Date(r.effective_end).toLocaleString('sv-SE', { timeZone: 'America/Anchorage' }).replace(' ', 'T').slice(0, 16)
        : '',
      mode: 'update',
    })
    setStatus({ ok: true, msg: '' })
  }

  const f = (v: string, prefix = '$') => v ? `${prefix}${parseFloat(v).toFixed(4)}` : '—'

  return (
    <>
      {/* Pricing table */}
      {isLoading ? (
        <div className="h-24 bg-gray-100 rounded animate-pulse" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs border border-gray-200 rounded-lg overflow-hidden">
            <thead className="bg-gray-50">
              <tr>
                {['EVSE', 'Conn. Fee', '$/kWh', '$/min', 'Idle $/min', 'Effective Start (AK)', 'Effective End (AK)', ''].map(h => (
                  <th key={h} className="px-3 py-2 text-left font-medium text-gray-500 whitespace-nowrap">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pricing.map((r: AdminPricing) => (
                <tr key={r.id} className="border-t border-gray-100 hover:bg-gray-50">
                  <td className="px-3 py-2 font-medium">{evseMap[r.station_id] || r.station_id}</td>
                  <td className="px-3 py-2">{fmtMoney(r.connection_fee)}</td>
                  <td className="px-3 py-2">{fmtMoney(r.price_per_kwh)}</td>
                  <td className="px-3 py-2">{fmtMoney(r.price_per_min)}</td>
                  <td className="px-3 py-2">{fmtMoney(r.idle_fee_per_min)}</td>
                  <td className="px-3 py-2 whitespace-nowrap">{fmtDate(r.effective_start)}</td>
                  <td className="px-3 py-2 whitespace-nowrap">{fmtDate(r.effective_end)}</td>
                  <td className="px-3 py-2">
                    <button onClick={() => editPricing(r)} className={btnSecondary + ' text-xs px-2 py-1'}>Edit</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Add / Update form */}
      <div className="border border-gray-200 rounded-xl p-4 space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide">
            {form.mode === 'create' ? 'Add Pricing Rule' : `Editing Rule — ${evseMap[form.station_id] || form.station_id}`}
          </h3>
          {form.mode === 'update' && (
            <button onClick={() => setForm(defaultForm())} className={btnSecondary + ' text-xs px-2 py-1'}>+ New</button>
          )}
        </div>

        <Field label="EVSE *">
          <select className={selectCls} value={form.station_id} onChange={e => setForm(f => ({ ...f, station_id: e.target.value }))} disabled={form.mode === 'update'}>
            <option value="">— select EVSE —</option>
            {evseList.map((e: AdminEvse) => (
              <option key={e.station_id} value={e.station_id}>{e.display_name || e.station_id}</option>
            ))}
          </select>
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Connection Fee ($)">
            <input className={inputCls} type="number" step="0.01" min="0" placeholder="0.00" value={form.connection_fee} onChange={e => setForm(f => ({ ...f, connection_fee: e.target.value }))} />
          </Field>
          <Field label="Price per kWh ($/kWh)">
            <input className={inputCls} type="number" step="0.0001" min="0" placeholder="0.4500" value={form.price_per_kwh} onChange={e => setForm(f => ({ ...f, price_per_kwh: e.target.value }))} />
          </Field>
          <Field label="Price per minute ($/min)">
            <input className={inputCls} type="number" step="0.0001" min="0" placeholder="optional" value={form.price_per_min} onChange={e => setForm(f => ({ ...f, price_per_min: e.target.value }))} />
          </Field>
          <Field label="Idle fee ($/min)">
            <input className={inputCls} type="number" step="0.0001" min="0" placeholder="optional" value={form.idle_fee_per_min} onChange={e => setForm(f => ({ ...f, idle_fee_per_min: e.target.value }))} />
          </Field>
          <Field label="Effective Start (AK local) *">
            <input className={inputCls} type="datetime-local" value={form.effective_start} onChange={e => setForm(f => ({ ...f, effective_start: e.target.value }))} />
          </Field>
          <Field label="Effective End (AK local — leave blank = open-ended)">
            <input className={inputCls} type="datetime-local" value={form.effective_end} onChange={e => setForm(f => ({ ...f, effective_end: e.target.value }))} />
          </Field>
        </div>

        <p className="text-xs text-gray-400">
          Preview: conn={f(form.connection_fee)} · kWh={f(form.price_per_kwh)} · min={f(form.price_per_min)} · idle={f(form.idle_fee_per_min)}
        </p>

        <div className="flex items-center gap-3">
          <button
            className={btnPrimary}
            disabled={!form.station_id || !form.effective_start || savePricing.isPending}
            onClick={() => { setStatus({ ok: true, msg: '' }); savePricing.mutate() }}
          >
            {savePricing.isPending ? 'Saving…' : form.mode === 'create' ? 'Create Rule' : 'Update Rule'}
          </button>
          <StatusMsg {...status} />
        </div>
      </div>
    </>
  )
}

// ════════════════════════════════════════════════════════════════════════════════
// Utility Accounts Section
// ════════════════════════════════════════════════════════════════════════════════

const UTILITY_OPTIONS: UtilityName[] = ['gvea', 'cvea', 'cea']

/** Fields that differ per utility — shown/hidden in the Add Account form */
const UTILITY_FIELDS: Record<UtilityName, { srvLoc: boolean; custNbr: boolean; meterGroup: boolean }> = {
  gvea: { srvLoc: true,  custNbr: true,  meterGroup: false },
  cvea: { srvLoc: true,  custNbr: true,  meterGroup: false },
  cea:  { srvLoc: false, custNbr: false, meterGroup: true  },
}

function fmtCollected(iso: string | null): string {
  if (!iso) return 'Never'
  const d = new Date(iso)
  return d.toLocaleString('en-US', {
    timeZone: 'America/Anchorage', month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', hour12: true,
  })
}

function UtilityAccountsSection() {
  const qc = useQueryClient()

  const { data: accounts = [], isLoading: acctLoading } =
    useQuery({ queryKey: ['utility-accounts'], queryFn: fetchUtilityAccounts })

  const { data: creds = [], isLoading: credsLoading } =
    useQuery({ queryKey: ['utility-credentials'], queryFn: fetchUtilityCredentials })

  // ── Add account form state ─────────────────────────────────────────────────
  const blankAcct = {
    utility: 'gvea' as UtilityName, account_number: '', display_name: '',
    service_location_number: '', customer_number: '', meter_group_id: '',
  }
  const [acctForm, setAcctForm] = useState(blankAcct)
  const [acctStatus, setAcctStatus] = useState({ ok: true, msg: '' })

  const addAccount = useMutation({
    mutationFn: () => createUtilityAccount({
      utility:                 acctForm.utility,
      account_number:          acctForm.account_number.trim(),
      display_name:            acctForm.display_name.trim(),
      service_location_number: acctForm.service_location_number.trim() || null,
      customer_number:         acctForm.customer_number.trim() || null,
      meter_group_id:          acctForm.meter_group_id.trim() || null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['utility-accounts'] })
      setAcctForm(blankAcct)
      setAcctStatus({ ok: true, msg: 'Account added!' })
    },
    onError: (e: Error) => setAcctStatus({ ok: false, msg: e.message }),
  })

  // ── Credentials form state ─────────────────────────────────────────────────
  const blankCred = { utility: 'gvea' as UtilityName, username: '', password: '' }
  const [credForm, setCredForm] = useState(blankCred)
  const [credStatus, setCredStatus] = useState({ ok: true, msg: '' })

  const saveCred = useMutation({
    mutationFn: () => upsertUtilityCredentials(credForm.utility, {
      username: credForm.username, password: credForm.password,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['utility-credentials'] })
      setCredForm(blankCred)
      setCredStatus({ ok: true, msg: 'Credentials saved!' })
    },
    onError: (e: Error) => setCredStatus({ ok: false, msg: e.message }),
  })

  // ── Manual collect trigger ─────────────────────────────────────────────────
  const [collectMsg, setCollectMsg] = useState('')
  const triggerCollect = useMutation({
    mutationFn: () => triggerUtilityCollect(2),
    onSuccess: (r) => {
      setCollectMsg(r.message)
      setTimeout(() => {
        qc.invalidateQueries({ queryKey: ['utility-accounts'] })
        setCollectMsg('')
      }, 4000)
    },
    onError: (e: Error) => setCollectMsg(`Error: ${e.message}`),
  })

  const fields = UTILITY_FIELDS[acctForm.utility]

  const inputCls = 'w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'
  const btnPrimary = 'px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors'
  const btnDanger  = 'px-2 py-1 bg-red-50 text-red-600 text-xs rounded hover:bg-red-100 transition-colors'
  const btnSecondary = 'px-3 py-1.5 border border-gray-300 text-gray-700 text-xs rounded-lg hover:bg-gray-50 transition-colors'

  // Group creds by utility for quick lookup
  const credMap = Object.fromEntries(creds.map(c => [c.utility, c]))

  return (
    <div className="space-y-8">

      {/* ── Account table ───────────────────────────────────────────────── */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-gray-700">Configured Accounts</h3>
          <button
            className={btnSecondary + ' flex items-center gap-1.5'}
            disabled={triggerCollect.isPending}
            onClick={() => triggerCollect.mutate()}
          >
            <RefreshCw size={13} className={triggerCollect.isPending ? 'animate-spin' : ''} />
            {triggerCollect.isPending ? 'Collecting…' : 'Collect Now'}
          </button>
        </div>
        {collectMsg && (
          <p className="text-xs text-blue-600 mb-2">{collectMsg}</p>
        )}
        {acctLoading ? (
          <div className="h-20 bg-gray-100 rounded-lg animate-pulse" />
        ) : accounts.length === 0 ? (
          <p className="text-sm text-gray-400 italic">No accounts configured yet.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-b border-gray-200 text-left text-gray-500">
                  <th className="pb-2 pr-4 font-medium">Utility</th>
                  <th className="pb-2 pr-4 font-medium">Account #</th>
                  <th className="pb-2 pr-4 font-medium">Site Name</th>
                  <th className="pb-2 pr-4 font-medium">Last Collected</th>
                  <th className="pb-2 pr-4 font-medium">Status</th>
                  <th className="pb-2 pr-4 font-medium">Enabled</th>
                  <th className="pb-2 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {accounts.map(a => (
                  <AccountRow
                    key={a.id}
                    account={a}
                    onToggle={(enabled) => updateUtilityAccount(a.id, { enabled })
                      .then(() => qc.invalidateQueries({ queryKey: ['utility-accounts'] }))}
                    onDelete={() => {
                      if (!confirm(`Delete ${a.utility}/${a.account_number}?`)) return
                      deleteUtilityAccount(a.id)
                        .then(() => qc.invalidateQueries({ queryKey: ['utility-accounts'] }))
                    }}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Add account form ─────────────────────────────────────────────── */}
      <div className="border border-gray-200 rounded-xl p-5 space-y-4">
        <h3 className="text-sm font-semibold text-gray-700">Add Account</h3>
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Field label="Utility *">
            <select
              className={inputCls}
              value={acctForm.utility}
              onChange={e => setAcctForm(f => ({ ...f, utility: e.target.value as UtilityName }))}
            >
              {UTILITY_OPTIONS.map(u => (
                <option key={u} value={u}>{UTILITY_LABELS[u]}</option>
              ))}
            </select>
          </Field>
          <Field label="Account Number *">
            <input className={inputCls} placeholder="e.g. 641977"
              value={acctForm.account_number}
              onChange={e => setAcctForm(f => ({ ...f, account_number: e.target.value }))} />
          </Field>
          <Field label="Display Name">
            <input className={inputCls} placeholder="e.g. Delta Junction EVSE"
              value={acctForm.display_name}
              onChange={e => setAcctForm(f => ({ ...f, display_name: e.target.value }))} />
          </Field>
          {fields.srvLoc && (
            <Field label="Service Location #">
              <input className={inputCls} placeholder="e.g. 637330"
                value={acctForm.service_location_number}
                onChange={e => setAcctForm(f => ({ ...f, service_location_number: e.target.value }))} />
            </Field>
          )}
          {fields.custNbr && (
            <Field label="Customer Number">
              <input className={inputCls} placeholder="e.g. 6159"
                value={acctForm.customer_number}
                onChange={e => setAcctForm(f => ({ ...f, customer_number: e.target.value }))} />
            </Field>
          )}
          {fields.meterGroup && (
            <Field label="Meter Group ID">
              <input className={inputCls} placeholder="e.g. 365681"
                value={acctForm.meter_group_id}
                onChange={e => setAcctForm(f => ({ ...f, meter_group_id: e.target.value }))} />
            </Field>
          )}
        </div>
        <div className="flex items-center gap-3">
          <button
            className={btnPrimary}
            disabled={!acctForm.account_number.trim() || addAccount.isPending}
            onClick={() => { setAcctStatus({ ok: true, msg: '' }); addAccount.mutate() }}
          >
            {addAccount.isPending ? 'Adding…' : 'Add Account'}
          </button>
          <StatusMsg {...acctStatus} />
        </div>
      </div>

      {/* ── Credentials ──────────────────────────────────────────────────── */}
      <div className="border border-gray-200 rounded-xl p-5 space-y-4">
        <div>
          <h3 className="text-sm font-semibold text-gray-700">Login Credentials</h3>
          <p className="text-xs text-gray-400 mt-0.5">
            One login per utility — used by the automated collector. Passwords are stored server-side only.
          </p>
        </div>

        {/* Current creds status */}
        {!credsLoading && (
          <div className="flex gap-3 flex-wrap">
            {UTILITY_OPTIONS.map(u => {
              const c = credMap[u]
              return (
                <div key={u} className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-full border border-gray-200">
                  {c
                    ? <><CheckCircle size={12} className="text-green-500" /><span className="text-gray-700">{UTILITY_LABELS[u]}: <span className="font-medium">{c.username}</span></span></>
                    : <><AlertCircle size={12} className="text-amber-500" /><span className="text-gray-500">{UTILITY_LABELS[u]}: not set</span></>
                  }
                </div>
              )
            })}
          </div>
        )}

        {/* Set/update credentials form */}
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <Field label="Utility *">
            <select
              className={inputCls}
              value={credForm.utility}
              onChange={e => setCredForm(f => ({ ...f, utility: e.target.value as UtilityName }))}
            >
              {UTILITY_OPTIONS.map(u => (
                <option key={u} value={u}>{UTILITY_LABELS[u]}</option>
              ))}
            </select>
          </Field>
          <Field label="Username / Email *">
            <input className={inputCls} type="email" placeholder="user@example.com"
              autoComplete="off"
              value={credForm.username}
              onChange={e => setCredForm(f => ({ ...f, username: e.target.value }))} />
          </Field>
          <Field label="Password *">
            <input className={inputCls} type="password" placeholder="••••••••"
              autoComplete="new-password"
              value={credForm.password}
              onChange={e => setCredForm(f => ({ ...f, password: e.target.value }))} />
          </Field>
        </div>
        <div className="flex items-center gap-3">
          <button
            className={btnPrimary}
            disabled={!credForm.username || !credForm.password || saveCred.isPending}
            onClick={() => { setCredStatus({ ok: true, msg: '' }); saveCred.mutate() }}
          >
            {saveCred.isPending ? 'Saving…' : `Save ${UTILITY_LABELS[credForm.utility]} Credentials`}
          </button>
          <StatusMsg {...credStatus} />
        </div>
      </div>

    </div>
  )
}

function AccountRow({
  account: a,
  onToggle,
  onDelete,
}: {
  account: UtilityAccount
  onToggle: (enabled: boolean) => void
  onDelete: () => void
}) {
  return (
    <tr className="border-b border-gray-100 hover:bg-gray-50">
      <td className="py-2 pr-4">
        <span className="font-medium text-gray-700">{UTILITY_LABELS[a.utility] ?? a.utility}</span>
      </td>
      <td className="py-2 pr-4 font-mono text-gray-600">{a.account_number}</td>
      <td className="py-2 pr-4 text-gray-600">{a.display_name || <span className="text-gray-300 italic">—</span>}</td>
      <td className="py-2 pr-4 text-gray-500">{fmtCollected(a.last_collected)}</td>
      <td className="py-2 pr-4">
        {a.last_error
          ? <span className="text-red-500 flex items-center gap-1"><AlertCircle size={11} /> Error</span>
          : a.last_collected
            ? <span className="text-green-600 flex items-center gap-1"><CheckCircle size={11} /> OK</span>
            : <span className="text-gray-400">—</span>
        }
      </td>
      <td className="py-2 pr-4">
        <button
          onClick={() => onToggle(!a.enabled)}
          className={`relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus:outline-none ${a.enabled ? 'bg-blue-600' : 'bg-gray-200'}`}
        >
          <span className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${a.enabled ? 'translate-x-4' : 'translate-x-0'}`} />
        </button>
      </td>
      <td className="py-2">
        <button onClick={onDelete} className="px-2 py-1 text-xs text-red-500 hover:text-red-700 hover:bg-red-50 rounded transition-colors">
          Remove
        </button>
      </td>
    </tr>
  )
}

// ════════════════════════════════════════════════════════════════════════════════
// Main export
// ════════════════════════════════════════════════════════════════════════════════

export function AdminTab() {
  return (
    <div className="space-y-4 max-w-5xl">
      <p className="text-xs text-gray-400">
        Admin panel — changes to EVSEs write to <code>runtime_overrides.json</code>; user and pricing changes go directly to Supabase.
      </p>

      <Section title="EVSE & Locations">
        <EvseSection />
      </Section>

      <Section title="Users" defaultOpen={false}>
        <UsersSection />
      </Section>

      <Section title="Pricing" defaultOpen={false}>
        <PricingSection />
      </Section>

      <Section title="Utility Data Collection" defaultOpen={false}>
        <UtilityAccountsSection />
      </Section>
    </div>
  )
}
