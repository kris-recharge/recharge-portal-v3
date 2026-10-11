/**
 * Parts / materials / services lines with costs, shared by the PM form, the
 * Log Work modal and the admin "Edit costs" panel on a submitted record.
 *
 * Cost rule (mirrored by the Year-End P&L): a line costs quantity × unit cost,
 * and nothing when it's warranty-covered. Labor is labor rate × (onsite +
 * mobilized hours).
 */

import { Plus, RotateCcw, Trash2 } from 'lucide-react'
import type { MaintenanceRecord, PartReplaced } from '../lib/api'

export type PartLine = Partial<PartReplaced> & { delete?: boolean }

export const PART_ACTIONS: { value: PartReplaced['action_taken']; label: string }[] = [
  { value: 'replaced', label: 'Replaced' },
  { value: 'repaired', label: 'Repaired' },
  { value: 'cleaned',  label: 'Cleaned' },
  { value: 'adjusted', label: 'Adjusted' },
  { value: 'material', label: 'Material / consumable' },
  { value: 'service',  label: 'Service / contract labor' },
]

export const usd = (v: number) =>
  v.toLocaleString('en-US', { style: 'currency', currency: 'USD' })

/** What the line costs ReCharge: zero when warranty-covered or not yet priced. */
export function lineCost(p: PartLine): number {
  if (p.delete || p.warranty || p.unit_cost == null) return 0
  return (p.quantity ?? 1) * p.unit_cost
}

export function laborCost(r: Pick<MaintenanceRecord, 'labor_rate' | 'onsite_hours' | 'mobilized_hours'>): number {
  return (r.labor_rate ?? 0) * ((r.onsite_hours ?? 0) + (r.mobilized_hours ?? 0))
}

/** Lines ready for the API: named lines only, quantity defaulted to 1. */
export function toPayload(lines: PartLine[]): PartReplaced[] {
  return lines
    .filter(p => p.delete || p.part_name?.trim())
    .map(p => ({
      ...p,
      part_name: (p.part_name ?? '').trim(),
      part_number: p.part_number?.trim() || null,
      action_taken: p.action_taken ?? 'replaced',
      notes: p.notes ?? null,
      quantity: p.quantity && p.quantity > 0 ? p.quantity : 1,
      unit_cost: p.unit_cost ?? null,
      warranty: !!p.warranty,
      vendor: p.vendor?.trim() || null,
      invoice_number: p.invoice_number?.trim() || null,
    })) as PartReplaced[]
}

const num = (s: string): number | null => (s === '' ? null : parseFloat(s))

export function PartLinesEditor({ lines, onChange, label = 'Parts / Materials / Services' }: {
  lines: PartLine[]
  onChange: (lines: PartLine[]) => void
  label?: string
}) {
  const update = (i: number, u: Partial<PartLine>) =>
    onChange(lines.map((x, idx) => (idx === i ? { ...x, ...u } : x)))
  // Saved lines are marked for deletion (undoable); unsaved ones just go.
  const remove = (i: number) =>
    lines[i].id ? update(i, { delete: !lines[i].delete }) : onChange(lines.filter((_, idx) => idx !== i))
  const total = lines.reduce((s, p) => s + lineCost(p), 0)

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <label className="text-xs font-medium text-gray-500">{label}</label>
        <button
          onClick={() => onChange([...lines, { action_taken: 'replaced', quantity: 1 }])}
          className="flex items-center gap-1 text-xs text-blue-600 hover:text-blue-800"
        >
          <Plus size={12} /> Add line
        </button>
      </div>
      {lines.map((p, i) => (
        <div key={p.id ?? `new-${i}`}
             className={`flex items-start gap-2 mb-3 pb-3 border-b border-gray-100 ${p.delete ? 'opacity-40 line-through' : ''}`}>
          <div className="flex-1 space-y-2">
            <div className="grid grid-cols-3 gap-2">
              <input placeholder="Part / material *" value={p.part_name ?? ''}
                     onChange={e => update(i, { part_name: e.target.value })} className="input-base" />
              <input placeholder="Part # (optional)" value={p.part_number ?? ''}
                     onChange={e => update(i, { part_number: e.target.value })} className="input-base" />
              <select value={p.action_taken ?? 'replaced'}
                      onChange={e => update(i, { action_taken: e.target.value as PartReplaced['action_taken'] })}
                      className="input-base">
                {PART_ACTIONS.map(a => <option key={a.value} value={a.value}>{a.label}</option>)}
              </select>
            </div>
            <div className="grid grid-cols-4 gap-2 items-center">
              <input type="number" min="0" step="1" placeholder="Qty" value={p.quantity ?? ''}
                     onChange={e => update(i, { quantity: num(e.target.value) ?? undefined })} className="input-base" />
              <input type="number" min="0" step="0.01" placeholder="Unit cost $" value={p.unit_cost ?? ''}
                     onChange={e => update(i, { unit_cost: num(e.target.value) })} className="input-base" />
              <input placeholder="Vendor" value={p.vendor ?? ''}
                     onChange={e => update(i, { vendor: e.target.value })} className="input-base" />
              <input placeholder="Invoice #" value={p.invoice_number ?? ''}
                     onChange={e => update(i, { invoice_number: e.target.value })} className="input-base" />
            </div>
            <div className="flex items-center justify-between text-xs text-gray-500">
              <label className="flex items-center gap-1.5 cursor-pointer">
                <input type="checkbox" checked={!!p.warranty}
                       onChange={e => update(i, { warranty: e.target.checked })} className="rounded" />
                Warranty-covered (no cost to us)
              </label>
              <span>{p.warranty ? 'Warranty' : p.unit_cost != null ? usd(lineCost(p)) : 'not priced'}</span>
            </div>
          </div>
          <button onClick={() => remove(i)} title={p.delete ? 'Keep this line' : 'Remove line'}
                  className="text-gray-300 hover:text-red-400 mt-2">
            {p.delete ? <RotateCcw size={14} /> : <Trash2 size={14} />}
          </button>
        </div>
      ))}
      {lines.length > 0 && (
        <div className="text-xs text-right text-gray-600">
          Material & service cost: <span className="font-semibold">{usd(total)}</span>
        </div>
      )}
    </div>
  )
}
