/**
 * SessionDensityHeatmap
 * Day-of-week (rows) × Hour-of-day (columns) heatmap showing session-start density.
 * White = 0 starts, deep blue = most starts in any cell.
 * Responds to applied date/EVSE filter.
 */

import { DensityPoint } from '../lib/api'

interface Props {
  data: DensityPoint[]
  isLoading?: boolean
}

const DOW_LABELS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const HOURS      = Array.from({ length: 24 }, (_, i) => i)

function cellColor(count: number, max: number): string {
  if (count === 0 || max === 0) return '#ffffff'
  // Exponential-ish scale so sparse cells are still visible
  const ratio = Math.max(0.08, count / max)
  const r = Math.round(37  + (37  - 37)  * (1 - ratio))   // stays at 37
  const g = Math.round(99  + (99  - 99)  * (1 - ratio))   // stays at 99
  const b = Math.round(235 * ratio + 255 * (1 - ratio))    // 255 → 235

  // Simpler: interpolate white → brand blue
  const white = 255
  const br = Math.round(white + (37  - white) * ratio)
  const bg = Math.round(white + (99  - white) * ratio)
  const bb = Math.round(white + (235 - white) * ratio)
  return `rgb(${br},${bg},${bb})`
}

function textColor(count: number, max: number): string {
  if (count === 0 || max === 0) return 'transparent'
  const ratio = count / max
  return ratio > 0.55 ? '#ffffff' : '#1e3a8a'
}

export function SessionDensityHeatmap({ data, isLoading }: Props) {
  if (isLoading) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
        <div className="skeleton h-4 w-56 rounded mb-4" />
        <div className="skeleton h-48 w-full rounded" />
      </div>
    )
  }

  // Build lookup: dow → hour → count
  const lookup: Record<number, Record<number, number>> = {}
  for (const p of data) {
    if (!lookup[p.dow]) lookup[p.dow] = {}
    lookup[p.dow][p.hour] = p.count
  }

  const maxCount = data.reduce((m, p) => Math.max(m, p.count), 0)

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
      <h3 className="text-sm font-semibold text-gray-700 mb-4">
        Session Start Density (by Day &amp; Hour)
      </h3>

      <div className="overflow-x-auto">
        <table className="border-collapse text-xs select-none" style={{ minWidth: 660 }}>
          <thead>
            <tr>
              {/* Day label column header (empty) */}
              <th className="w-10" />
              {HOURS.map(h => (
                <th
                  key={h}
                  className="text-center text-gray-400 font-normal pb-1"
                  style={{ width: 32, minWidth: 26 }}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {DOW_LABELS.map((day, dow) => (
              <tr key={dow}>
                {/* Day label */}
                <td className="pr-2 text-gray-500 font-medium text-right whitespace-nowrap py-0.5">
                  {day}
                </td>

                {HOURS.map(hour => {
                  const count = lookup[dow]?.[hour] ?? 0
                  const bg    = cellColor(count, maxCount)
                  const fg    = textColor(count, maxCount)
                  return (
                    <td
                      key={hour}
                      title={count > 0 ? `${day} ${hour}:00 — ${count} session${count !== 1 ? 's' : ''}` : undefined}
                      style={{ backgroundColor: bg, border: '1px solid #e5e7eb', width: 32, minWidth: 26, height: 28 }}
                      className="text-center align-middle leading-none cursor-default transition-all duration-150 hover:opacity-80"
                    >
                      <span style={{ color: fg, fontSize: 10, fontWeight: 600 }}>
                        {count > 0 ? count : ''}
                      </span>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>

          {/* Hour axis label */}
          <tfoot>
            <tr>
              <td />
              <td colSpan={24} className="pt-2 text-center text-gray-400 font-normal">
                Hour (0–23, Alaska Local Time)
              </td>
            </tr>
          </tfoot>
        </table>
      </div>

      {/* Legend */}
      {maxCount > 0 && (
        <div className="mt-3 flex items-center gap-2 text-xs text-gray-500">
          <span>Starts</span>
          <div className="flex">
            {[0, 0.15, 0.3, 0.5, 0.7, 0.85, 1].map((r, i) => (
              <div
                key={i}
                style={{
                  width: 18,
                  height: 12,
                  backgroundColor: cellColor(Math.round(r * maxCount), maxCount),
                  border: '1px solid #e5e7eb',
                }}
              />
            ))}
          </div>
          <span>{maxCount}</span>
        </div>
      )}
    </div>
  )
}
