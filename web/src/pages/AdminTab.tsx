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
  type AdminUser, type AdminPricing, type AdminEvse, type AdminUnidentifiedEvse,
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
    </div>
  )
}
