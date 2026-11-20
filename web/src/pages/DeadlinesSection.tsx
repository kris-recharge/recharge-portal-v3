/**
 * Admin → Deadlines — ReCharge's bills, filings and renewals with countdowns.
 *
 * Shown to @rechargealaska.net accounts only. App.tsx/AdminTab hide it from
 * everyone else, and every /api/admin/deadlines route returns 403 to them
 * regardless (app/routers/deadlines.py).
 *
 * Dates are Anchorage calendar dates ("YYYY-MM-DD"). They are formatted
 * without ever going through a UTC Date, so a due date never slips a day.
 */

import { useEffect, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle, CalendarClock, Check, DollarSign, ExternalLink, FileText, Mail, Pencil,
  Plus, Repeat, RotateCcw, Trash2, BellOff, X,
} from 'lucide-react'
import {
  fetchDeadlines, createDeadline, updateDeadline, completeDeadline,
  scheduleDeadline, snoozeDeadline, cancelDeadline,
  type Deadline, type DeadlineCategory, type DeadlineInput,
  type DeadlineRecurrence, type DeadlineStatus,
} from '../lib/api'

// ── Labels ────────────────────────────────────────────────────────────────────

const CATEGORY_LABELS: Record<DeadlineCategory, string> = {
  payable: 'Payables',
  state_filing: 'State',
  tax: 'Tax',
  insurance: 'Insurance',
  permit: 'Permits',
  grant: 'Grants',
  maintenance: 'Maintenance',
  other: 'Other',
}
const CATEGORIES = Object.keys(CATEGORY_LABELS) as DeadlineCategory[]

const RECURRENCE_LABELS: Record<DeadlineRecurrence, string> = {
  none: 'One-time',
  monthly: 'Monthly',
  quarterly: 'Quarterly',
  yearly: 'Yearly',
  biennial: 'Every 2 years',
}

const DEFAULT_OFFSETS = [14, 7, 3, 1, 0]

/** Wells Fargo Bill Pay checks take about this many business days to arrive. */
const CHECK_LEAD_DAYS = 7

// ── Formatting ────────────────────────────────────────────────────────────────

function parseYmd(ymd: string): Date {
  const [y, m, d] = ymd.split('-').map(Number)
  return new Date(y, m - 1, d)
}

function fmtDay(ymd: string | null, withYear = true): string {
  if (!ymd) return '—'
  return parseYmd(ymd).toLocaleDateString('en-US', {
    month: 'short', day: 'numeric', ...(withYear ? { year: 'numeric' } : {}),
  })
}

function fmtUsd(v: number | null): string {
  if (v == null) return ''
  return v.toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

function daysLabel(d: number): string {
  if (d === 0) return 'Today'
  if (d < 0) return `${-d}d late`
  return `${d}d`
}

/** Today in Anchorage as YYYY-MM-DD (fallback before the server answers). */
function akToday(): string {
  return new Date().toLocaleDateString('en-CA', { timeZone: 'America/Anchorage' })
}

// ── Styles (match AdminTab) ───────────────────────────────────────────────────

const inputCls = 'w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500'
const btnPrimary = 'px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm font-semibold rounded-lg transition-colors disabled:opacity-50'
const btnSecondary = 'px-4 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 text-sm font-medium rounded-lg transition-colors'
const btnAction = 'inline-flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-lg transition-colors disabled:opacity-50'

const pillCls = (selected: boolean) =>
  `px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
    selected
      ? 'bg-blue-600 text-white border-blue-600'
      : 'bg-white text-gray-600 border-gray-200 hover:border-blue-400'
  }`

/** Colour by urgency; a scheduled item is handled, so it drops to neutral. */
function tone(item: Deadline) {
  if (item.status === 'done' || item.status === 'cancelled') {
    return { text: 'text-gray-400', bar: 'bg-gray-300', ring: 'border-gray-200' }
  }
  if (item.status === 'scheduled') {
    // Scheduled, but the check won't land by the due date: still a problem.
    if (item.arrives_late) return { text: 'text-red-600', bar: 'bg-red-500', ring: 'border-red-200' }
    return { text: 'text-emerald-600', bar: 'bg-emerald-400', ring: 'border-gray-200' }
  }
  switch (item.urgency) {
    case 'overdue':  return { text: 'text-red-600',   bar: 'bg-red-500',   ring: 'border-red-200' }
    case 'critical': return { text: 'text-red-600',   bar: 'bg-red-500',   ring: 'border-red-100' }
    case 'soon':     return { text: 'text-amber-600', bar: 'bg-amber-400', ring: 'border-gray-200' }
    default:         return { text: 'text-gray-700',  bar: 'bg-gray-300',  ring: 'border-gray-200' }
  }
}

/** Countdown bar: empty 30+ days out, full on the due date and after. */
function barFill(days: number): number {
  return Math.min(1, Math.max(0.04, 1 - days / 30))
}

// ── Summary cards ─────────────────────────────────────────────────────────────

function SummaryCards({ items }: { items: Deadline[] }) {
  const live = items.filter(i => i.status === 'open' || i.status === 'scheduled')
  const urgent = live.filter(i => i.status === 'open' && i.days_remaining <= 7)
  const overdue = urgent.filter(i => i.days_remaining < 0).length
  const within30 = live.filter(i => i.days_remaining <= 30)
  const payables30 = within30
    .filter(i => i.category === 'payable' && i.amount != null)
    .reduce((s, i) => s + (i.amount ?? 0), 0)

  const card = 'rounded-xl border p-2.5 sm:p-4 flex flex-col gap-0.5 sm:gap-1 min-w-0'
  return (
    <div className="grid grid-cols-3 gap-2 sm:gap-3">
      <div className={`${card} ${urgent.length ? 'border-red-200 bg-red-50' : 'border-gray-200 bg-white'}`}>
        <span className="text-[11px] sm:text-xs font-medium text-gray-500 flex items-start sm:items-center gap-1 sm:gap-1.5 leading-tight">
          <AlertTriangle size={13} className={`shrink-0 mt-px sm:mt-0 ${urgent.length ? 'text-red-500' : 'text-gray-400'}`} />
          Overdue / due ≤ 7 days
        </span>
        <span className={`text-lg sm:text-2xl font-bold tabular-nums ${urgent.length ? 'text-red-600' : 'text-gray-900'}`}>
          {urgent.length}
        </span>
        <span className="hidden sm:block text-xs text-gray-400">
          {overdue ? `${overdue} overdue` : 'Nothing overdue'}
        </span>
      </div>
      <div className={`${card} ${within30.length ? 'border-amber-200 bg-amber-50' : 'border-gray-200 bg-white'}`}>
        <span className="text-[11px] sm:text-xs font-medium text-gray-500 flex items-start sm:items-center gap-1 sm:gap-1.5 leading-tight">
          <CalendarClock size={13} className={`shrink-0 mt-px sm:mt-0 ${within30.length ? 'text-amber-500' : 'text-gray-400'}`} />
          Due in 30 days
        </span>
        <span className={`text-lg sm:text-2xl font-bold tabular-nums ${within30.length ? 'text-amber-600' : 'text-gray-900'}`}>
          {within30.length}
        </span>
        <span className="hidden sm:block text-xs text-gray-400">Open and scheduled</span>
      </div>
      <div className={`${card} border-gray-200 bg-white`}>
        <span className="text-[11px] sm:text-xs font-medium text-gray-500 flex items-start sm:items-center gap-1 sm:gap-1.5 leading-tight">
          <DollarSign size={13} className="shrink-0 mt-px sm:mt-0 text-gray-400" />
          Payables due in 30 days
        </span>
        <span className="text-lg sm:text-2xl font-bold tabular-nums text-gray-900" title={fmtUsd(payables30)}>
          {/* Whole dollars on phones so it fits a third of a 380px screen. */}
          <span className="sm:hidden">${Math.round(payables30).toLocaleString('en-US')}</span>
          <span className="hidden sm:inline">{fmtUsd(payables30)}</span>
        </span>
        <span className="hidden sm:block text-xs text-gray-400">Includes overdue</span>
      </div>
    </div>
  )
}

// ── Add / edit form ───────────────────────────────────────────────────────────

interface FormState {
  title: string
  category: DeadlineCategory
  vendor: string
  amount: string
  due_date: string
  recurrence: DeadlineRecurrence
  link_url: string
  document_url: string
  external_ref: string
  notes: string
  alert_offsets: string
  lead_business_days: string
}

function toForm(d?: Deadline): FormState {
  return {
    title: d?.title ?? '',
    category: d?.category ?? 'payable',
    vendor: d?.vendor ?? '',
    amount: d?.amount != null ? String(d.amount) : '',
    due_date: d?.due_date ?? '',
    recurrence: d?.recurrence ?? 'none',
    link_url: d?.link_url ?? '',
    document_url: d?.document_url ?? '',
    external_ref: d?.external_ref ?? '',
    notes: d?.notes ?? '',
    alert_offsets: (d?.alert_offsets ?? DEFAULT_OFFSETS).join(', '),
    // New bills default to a mailed check — that's how ReCharge pays them.
    lead_business_days: String(d ? d.lead_business_days : CHECK_LEAD_DAYS),
  }
}

function fromForm(f: FormState): DeadlineInput | string {
  if (!f.title.trim()) return 'Title is required'
  if (!f.due_date) return 'Due date is required'
  const amount = f.amount.trim() ? Number(f.amount.replace(/[$,]/g, '')) : null
  if (amount != null && (!Number.isFinite(amount) || amount < 0)) return 'Amount must be a positive number'
  const link = f.link_url.trim()
  if (link && !/^https?:\/\//i.test(link)) return 'Link must start with http:// or https://'
  const doc = f.document_url.trim()
  if (doc && !/^https?:\/\//i.test(doc)) return 'Invoice link must start with http:// or https://'
  const lead = Number(f.lead_business_days || 0)
  if (!Number.isInteger(lead) || lead < 0 || lead > 30) return 'Mail time must be 0–30 business days'
  const offsets = f.alert_offsets.split(/[,\s]+/).filter(Boolean).map(Number)
  if (offsets.some(o => !Number.isInteger(o) || o < 0 || o > 365)) {
    return 'Alert days must be whole numbers from 0 to 365'
  }
  return {
    title: f.title.trim(),
    category: f.category,
    vendor: f.vendor.trim() || null,
    amount,
    due_date: f.due_date,
    recurrence: f.recurrence,
    link_url: link || null,
    document_url: doc || null,
    external_ref: f.external_ref.trim() || null,
    notes: f.notes.trim() || null,
    alert_offsets: offsets,
    lead_business_days: lead,
  }
}

function DeadlineForm({ initial, onDone }: { initial?: Deadline; onDone: () => void }) {
  const qc = useQueryClient()
  const [f, setF] = useState<FormState>(() => toForm(initial))
  const [err, setErr] = useState('')
  const set = <K extends keyof FormState>(k: K, v: FormState[K]) => setF(p => ({ ...p, [k]: v }))

  const save = useMutation({
    mutationFn: (body: DeadlineInput) =>
      initial ? updateDeadline(initial.id, body) : createDeadline(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['deadlines'] })
      onDone()
    },
    onError: (e: Error) =>
      setErr(e.message.includes('409')
        ? 'Already on the board: that vendor + reference number (or this date in the series) exists'
        : 'Save failed — check the fields and try again'),
  })

  function submit(e: React.FormEvent) {
    e.preventDefault()
    const body = fromForm(f)
    if (typeof body === 'string') { setErr(body); return }
    setErr('')
    save.mutate(body)
  }

  const label = 'block text-xs font-medium text-gray-500 mb-1'
  return (
    <form onSubmit={submit} className="border border-blue-200 bg-blue-50/40 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold text-gray-800">{initial ? 'Edit deadline' : 'New deadline'}</span>
        <button type="button" onClick={onDone} className="text-gray-400 hover:text-gray-600" aria-label="Close">
          <X size={16} />
        </button>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div className="sm:col-span-2">
          <label className={label}>Title</label>
          <input className={inputCls} value={f.title} onChange={e => set('title', e.target.value)}
                 placeholder="LynkWell INV1539" required />
        </div>
        <div>
          <label className={label}>Category</label>
          <select className={inputCls} value={f.category}
                  onChange={e => set('category', e.target.value as DeadlineCategory)}>
            {CATEGORIES.map(c => <option key={c} value={c}>{CATEGORY_LABELS[c]}</option>)}
          </select>
        </div>
        <div>
          <label className={label}>Vendor / agency</label>
          <input className={inputCls} value={f.vendor} onChange={e => set('vendor', e.target.value)}
                 placeholder="LynkWell, State of Alaska, KPB" />
        </div>
        <div>
          <label className={label}>Due date</label>
          <input type="date" className={inputCls} value={f.due_date}
                 onChange={e => set('due_date', e.target.value)} required />
        </div>
        <div>
          <label className={label}>Amount (optional)</label>
          <input className={inputCls} inputMode="decimal" value={f.amount}
                 onChange={e => set('amount', e.target.value)} placeholder="1105.29" />
        </div>
        <div>
          <label className={label}>Repeats</label>
          <select className={inputCls} value={f.recurrence}
                  onChange={e => set('recurrence', e.target.value as DeadlineRecurrence)}>
            {(Object.keys(RECURRENCE_LABELS) as DeadlineRecurrence[]).map(r =>
              <option key={r} value={r}>{RECURRENCE_LABELS[r]}</option>)}
          </select>
        </div>
        <div>
          <label className={label}>Reference # (invoice, filing ID)</label>
          <input className={inputCls} value={f.external_ref}
                 onChange={e => set('external_ref', e.target.value)} placeholder="INV1539" />
        </div>
        <div className="sm:col-span-2">
          <label className={label}>How it's paid / sent</label>
          <div className="flex flex-col sm:flex-row gap-2">
            <select className={inputCls}
                    value={f.lead_business_days === '0' ? 'online'
                      : f.lead_business_days === String(CHECK_LEAD_DAYS) ? 'check' : 'custom'}
                    onChange={e => set('lead_business_days',
                      e.target.value === 'online' ? '0'
                        : e.target.value === 'check' ? String(CHECK_LEAD_DAYS) : '5')}>
              <option value="check">Mailed check, Wells Fargo Bill Pay ({CHECK_LEAD_DAYS} business days)</option>
              <option value="online">Online / same day</option>
              <option value="custom">Other mail time…</option>
            </select>
            {!['0', String(CHECK_LEAD_DAYS)].includes(f.lead_business_days) && (
              <div className="flex items-center gap-2 shrink-0">
                <input className={`${inputCls} w-20`} inputMode="numeric" value={f.lead_business_days}
                       onChange={e => set('lead_business_days', e.target.value.replace(/\D/g, ''))} />
                <span className="text-xs text-gray-500">business days</span>
              </div>
            )}
          </div>
          {f.lead_business_days !== '0' && (
            <p className="text-xs text-gray-400 mt-1">
              The countdown and alerts run to the mail-by date: the due date minus this many business
              days (weekends and federal holidays skipped).
            </p>
          )}
        </div>
        <div className="sm:col-span-2">
          <label className={label}>Invoice file (OneDrive link)</label>
          <input type="url" className={inputCls} value={f.document_url}
                 onChange={e => set('document_url', e.target.value)}
                 placeholder="OneDrive → Share → Copy link (People in ReCharge Alaska)" />
        </div>
        <div className="sm:col-span-2">
          <label className={label}>Payment / filing link (optional)</label>
          <input type="url" className={inputCls} value={f.link_url}
                 onChange={e => set('link_url', e.target.value)} placeholder="https://…" />
        </div>
        <div className="sm:col-span-2">
          <label className={label}>Notes</label>
          <textarea className={inputCls} rows={2} value={f.notes} onChange={e => set('notes', e.target.value)} />
        </div>
        <details className="sm:col-span-2">
          <summary className="text-xs text-gray-500 cursor-pointer select-none">Advanced: alert days</summary>
          <div className="mt-2">
            <label className={label}>Alert this many days before the due date (the mail-by date for checks)</label>
            <input className={inputCls} value={f.alert_offsets}
                   onChange={e => set('alert_offsets', e.target.value)} placeholder="14, 7, 3, 1, 0" />
            <p className="text-xs text-gray-400 mt-1">
              Overdue items alert every day until done or scheduled, whatever is set here.
            </p>
          </div>
        </details>
      </div>
      {err && <p className="text-xs text-red-600">{err}</p>}
      <div className="flex gap-2">
        <button type="submit" className={btnPrimary} disabled={save.isPending}>
          {save.isPending ? 'Saving…' : initial ? 'Save changes' : 'Add deadline'}
        </button>
        <button type="button" className={btnSecondary} onClick={onDone}>Cancel</button>
      </div>
    </form>
  )
}

// ── One row ───────────────────────────────────────────────────────────────────

function DeadlineRow({ item, today, onEdit }: { item: Deadline; today: string; onEdit: () => void }) {
  const qc = useQueryClient()
  const [mode, setMode] = useState<'schedule' | 'snooze' | null>(null)
  const [pickDate, setPickDate] = useState(today)
  const [msg, setMsg] = useState('')
  const t = tone(item)
  const live = item.status === 'open' || item.status === 'scheduled'
  const snoozed = item.snoozed_until && item.snoozed_until >= today

  const refresh = () => qc.invalidateQueries({ queryKey: ['deadlines'] })
  const act = useMutation({
    mutationFn: async (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => { setMode(null); refresh() },
    onError: () => setMsg('That didn’t save — try again'),
  })

  // A recurring item's next instance shows up in the list on refresh.
  const markDone = () => { setMsg(''); act.mutate(() => completeDeadline(item.id)) }

  const isCheck = item.lead_business_days > 0
  const meta = [
    CATEGORY_LABELS[item.category], item.vendor, fmtUsd(item.amount),
    `Due ${fmtDay(item.due_date)}`, item.external_ref,
  ].filter(Boolean)

  return (
    <div id={`deadline-${item.id}`}
         className={`bg-white border ${t.ring} rounded-xl p-3 sm:p-4 space-y-2 scroll-mt-24`}>
      <div className="flex gap-3 sm:gap-4">
      {/* Countdown number */}
      <div className="w-14 sm:w-20 shrink-0 text-center pt-0.5">
        <div className={`text-lg sm:text-2xl font-bold tabular-nums leading-tight ${t.text}`}>
          {live ? daysLabel(item.days_remaining) : item.status === 'done' ? <Check size={22} className="mx-auto" /> : '—'}
        </div>
        <div className="text-[11px] text-gray-400 mt-0.5 leading-tight">
          {live && isCheck ? <>mail by<br />{fmtDay(item.action_date, false)}</> : fmtDay(item.due_date, false)}
        </div>
      </div>

      <div className="flex-1 min-w-0 space-y-1.5">
        <div className="flex flex-wrap items-start gap-x-2 gap-y-1">
          <span className={`font-semibold text-sm break-words ${live ? 'text-gray-900' : 'text-gray-500 line-through decoration-gray-300'}`}>
            {item.title}
          </span>
          {item.status === 'scheduled' && (
            <span className={`px-2 py-0.5 rounded-full text-[11px] font-medium border ${
              item.arrives_late
                ? 'bg-red-50 text-red-700 border-red-200'
                : 'bg-emerald-50 text-emerald-700 border-emerald-200'}`}>
              {isCheck ? 'Check mails' : 'Scheduled'} {fmtDay(item.scheduled_on, false)}
              {item.expected_arrival && ` · arrives ~${fmtDay(item.expected_arrival, false)}`}
              {item.arrives_late && ' (after due date)'}
            </span>
          )}
          {item.status === 'done' && (
            <span className="px-2 py-0.5 rounded-full text-[11px] font-medium bg-gray-100 text-gray-500">
              Done {item.completed_at ? new Date(item.completed_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'America/Anchorage' }) : ''}
            </span>
          )}
          {item.status === 'cancelled' && (
            <span className="px-2 py-0.5 rounded-full text-[11px] font-medium bg-gray-100 text-gray-500">Deleted</span>
          )}
          {item.recurrence !== 'none' && (
            <span className="inline-flex items-center gap-1 text-[11px] text-gray-400" title="Repeats">
              <Repeat size={11} /> {RECURRENCE_LABELS[item.recurrence]}
            </span>
          )}
          {live && snoozed && (
            <span className="inline-flex items-center gap-1 text-[11px] text-gray-400">
              <BellOff size={11} /> Alerts quiet until {fmtDay(item.snoozed_until, false)}
            </span>
          )}
        </div>

        <p className="text-xs text-gray-500 break-words">{meta.join(' · ')}</p>
        {live && isCheck && item.status === 'open' && (
          <p className={`text-xs font-medium flex items-center gap-1 ${t.text}`}>
            <Mail size={12} className="shrink-0" />
            Mail check by {fmtDay(item.action_date)} ({item.lead_business_days} business days to arrive)
          </p>
        )}
      </div>
      </div>

      {/* Full card width on phones; lined up under the title on wider screens. */}
      <div className="space-y-2 sm:pl-24">

        {live && (
          <div className="h-1.5 bg-gray-100 rounded-full overflow-hidden" aria-hidden>
            <div className={`h-full rounded-full ${t.bar}`} style={{ width: `${barFill(item.days_remaining) * 100}%` }} />
          </div>
        )}

        {item.notes && <p className="text-xs text-gray-500 whitespace-pre-line break-words">{item.notes}</p>}

        {/* Actions */}
        <div className="flex flex-wrap gap-1.5 pt-0.5">
          {item.document_url && (
            <a href={item.document_url} target="_blank" rel="noopener noreferrer"
               className={`${btnAction} bg-blue-600 text-white hover:bg-blue-700`}>
              <FileText size={12} /> Invoice
            </a>
          )}
          {item.link_url && (
            <a href={item.link_url} target="_blank" rel="noopener noreferrer"
               className={`${btnAction} bg-blue-50 text-blue-700 hover:bg-blue-100`}>
              <ExternalLink size={12} /> {item.category === 'payable' ? 'Pay' : 'Open link'}
            </a>
          )}
          {live && (
            <>
              <button className={`${btnAction} bg-green-50 text-green-700 hover:bg-green-100`}
                      disabled={act.isPending} onClick={markDone}>
                <Check size={12} /> Mark done
              </button>
              {item.status === 'open' ? (
                <button className={`${btnAction} bg-emerald-50 text-emerald-700 hover:bg-emerald-100`}
                        onClick={() => { setPickDate(today); setMode(mode === 'schedule' ? null : 'schedule') }}>
                  <CalendarClock size={12} /> {isCheck ? 'Check scheduled' : 'Mark scheduled'}
                </button>
              ) : (
                <button className={`${btnAction} bg-gray-100 text-gray-600 hover:bg-gray-200`}
                        disabled={act.isPending}
                        onClick={() => act.mutate(() => updateDeadline(item.id, { status: 'open' }))}>
                  <RotateCcw size={12} /> Unschedule
                </button>
              )}
              <button className={`${btnAction} bg-gray-100 text-gray-600 hover:bg-gray-200`}
                      onClick={() => { setPickDate(item.snoozed_until ?? today); setMode(mode === 'snooze' ? null : 'snooze') }}>
                <BellOff size={12} /> Snooze
              </button>
              <button className={`${btnAction} bg-gray-100 text-gray-600 hover:bg-gray-200`} onClick={onEdit}>
                <Pencil size={12} /> Edit
              </button>
              <button className={`${btnAction} text-red-600 hover:bg-red-50`}
                      disabled={act.isPending}
                      onClick={() => {
                        if (confirm(`Delete “${item.title}”? It moves to history as deleted.`)) {
                          act.mutate(() => cancelDeadline(item.id))
                        }
                      }}>
                <Trash2 size={12} /> Delete
              </button>
            </>
          )}
          {item.status === 'done' && (
            <button className={`${btnAction} bg-gray-100 text-gray-600 hover:bg-gray-200`}
                    disabled={act.isPending}
                    onClick={() => act.mutate(() => updateDeadline(item.id, { status: 'open' }))}>
              <RotateCcw size={12} /> Reopen
            </button>
          )}
        </div>

        {mode && (
          <div className="flex flex-wrap items-end gap-2 bg-gray-50 border border-gray-200 rounded-lg p-2.5">
            <div>
              <label className="block text-[11px] font-medium text-gray-500 mb-1">
                {mode === 'schedule'
                  ? (isCheck ? 'Wells Fargo mails the check on' : 'Payment scheduled for')
                  : 'No alerts through'}
              </label>
              <input type="date" className="px-2 py-1.5 border border-gray-200 rounded-lg text-sm"
                     value={pickDate} onChange={e => setPickDate(e.target.value)} />
            </div>
            {mode === 'schedule' && isCheck && (
              <p className={`basis-full text-[11px] ${pickDate > item.action_date ? 'text-red-600' : 'text-gray-500'}`}>
                {pickDate > item.action_date
                  ? `After the ${fmtDay(item.action_date, false)} mail-by date: it may arrive after ${fmtDay(item.due_date, false)}.`
                  : `Mail by ${fmtDay(item.action_date, false)} to arrive by ${fmtDay(item.due_date, false)}.`}
              </p>
            )}
            <button className={`${btnAction} bg-blue-600 text-white hover:bg-blue-700`}
                    disabled={!pickDate || act.isPending}
                    onClick={() => act.mutate(() => mode === 'schedule'
                      ? scheduleDeadline(item.id, pickDate)
                      : snoozeDeadline(item.id, pickDate))}>
              Save
            </button>
            {mode === 'snooze' && item.snoozed_until && (
              <button className={`${btnAction} bg-gray-100 text-gray-600 hover:bg-gray-200`}
                      onClick={() => act.mutate(() => snoozeDeadline(item.id, null))}>
                Clear snooze
              </button>
            )}
            <button className={`${btnAction} text-gray-500 hover:bg-gray-100`} onClick={() => setMode(null)}>
              Cancel
            </button>
          </div>
        )}
        {msg && <p className="text-xs text-gray-500">{msg}</p>}
      </div>
    </div>
  )
}

// ── Section ───────────────────────────────────────────────────────────────────

export function DeadlinesSection() {
  const [category, setCategory] = useState<DeadlineCategory | null>(null)
  const [showHistory, setShowHistory] = useState(false)
  const [editing, setEditing] = useState<Deadline | 'new' | null>(null)

  const statuses: DeadlineStatus[] = showHistory
    ? ['open', 'scheduled', 'done', 'cancelled']
    : ['open', 'scheduled']

  // Summary cards always reflect everything open, whatever the filter.
  const all = useQuery({
    queryKey: ['deadlines', 'open,scheduled', null],
    queryFn: () => fetchDeadlines(['open', 'scheduled']),
    refetchInterval: 10 * 60_000,  // keep "days left" honest if the tab stays open past midnight
  })
  const list = useQuery({
    queryKey: ['deadlines', statuses.join(','), category],
    queryFn: () => fetchDeadlines(statuses, category),
    refetchInterval: 10 * 60_000,
  })

  const today = list.data?.today ?? all.data?.today ?? akToday()
  const items = list.data?.items ?? []

  // Alert emails / push notifications link to #deadline-<id>: bring that row
  // into view and flash it once the list has loaded.
  const [linked, setLinked] = useState<string | null>(() =>
    window.location.hash.startsWith('#deadline-') ? window.location.hash.slice(1) : null)
  useEffect(() => {
    if (!linked || !list.data) return
    const el = document.getElementById(linked)
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' })
      el.classList.add('ring-2', 'ring-blue-400')
      setTimeout(() => el.classList.remove('ring-2', 'ring-blue-400'), 4000)
    }
    setLinked(null)
  }, [linked, list.data])

  return (
    <div className="space-y-4">
      {all.data && <SummaryCards items={all.data.items} />}

      <div className="flex flex-col sm:flex-row sm:items-center gap-3 sm:justify-between">
        <div className="flex flex-wrap gap-1.5">
          <button className={pillCls(category === null)} onClick={() => setCategory(null)}>All</button>
          {CATEGORIES.map(c => (
            <button key={c} className={pillCls(category === c)} onClick={() => setCategory(c)}>
              {CATEGORY_LABELS[c]}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <label className="flex items-center gap-1.5 text-xs text-gray-500 cursor-pointer select-none">
            <input type="checkbox" checked={showHistory} onChange={e => setShowHistory(e.target.checked)} />
            Show completed
          </label>
          <button className={`${btnPrimary} inline-flex items-center gap-1.5`} onClick={() => setEditing('new')}>
            <Plus size={14} /> Add
          </button>
        </div>
      </div>

      {editing && (
        <DeadlineForm
          key={editing === 'new' ? 'new' : editing.id}
          initial={editing === 'new' ? undefined : editing}
          onDone={() => setEditing(null)}
        />
      )}

      {list.isLoading && <div className="text-sm text-gray-400 py-6 text-center">Loading…</div>}
      {list.isError && (
        <div className="text-sm text-red-600 py-6 text-center">Couldn’t load deadlines.</div>
      )}
      {list.data && items.length === 0 && (
        <div className="text-sm text-gray-400 py-8 text-center border border-dashed border-gray-200 rounded-xl">
          {category ? `Nothing in ${CATEGORY_LABELS[category]}.` : 'No open deadlines.'}
        </div>
      )}

      <div className="space-y-2">
        {items.map(item => (
          <DeadlineRow key={item.id} item={item} today={today} onEdit={() => setEditing(item)} />
        ))}
      </div>
    </div>
  )
}
