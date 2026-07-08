/** Site-grouped EVSE filter pills — shared by Charging Sessions, Data Export,
 * and Connectivity history. EVSEs cluster under a clickable site label;
 * clicking the label toggles every EVSE at that site. Empty selection = all.
 */

export interface EvseFilterOption {
  id: string
  name: string
  location: string
}

interface EvseFilterGroupsProps {
  options: EvseFilterOption[]
  selected: string[] // [] = all EVSEs
  onChange: (ids: string[]) => void
}

const pillCls = (selected: boolean) =>
  `px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
    selected
      ? 'bg-blue-600 text-white border-blue-600'
      : 'bg-white text-gray-600 border-gray-200 hover:border-blue-400'
  }`

export function EvseFilterGroups({ options, selected, onChange }: EvseFilterGroupsProps) {
  const groups = new Map<string, EvseFilterOption[]>()
  for (const opt of options) {
    const site = opt.location || 'Other'
    const list = groups.get(site)
    if (list) list.push(opt)
    else groups.set(site, [opt])
  }
  const sites = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]))

  function toggleOne(id: string) {
    onChange(selected.includes(id) ? selected.filter(s => s !== id) : [...selected, id])
  }

  function toggleSite(evses: EvseFilterOption[]) {
    const ids = evses.map(e => e.id)
    const allOn = ids.every(id => selected.includes(id))
    onChange(
      allOn
        ? selected.filter(s => !ids.includes(s))
        : [...new Set([...selected, ...ids])],
    )
  }

  return (
    <div className="flex flex-wrap items-end gap-x-4 gap-y-2">
      <button onClick={() => onChange([])} className={pillCls(selected.length === 0)}>
        All
      </button>
      {sites.map(([site, evses]) => {
        const allOn = evses.every(e => selected.includes(e.id))
        return (
          <div key={site} className="flex flex-col items-center gap-1">
            <button
              onClick={() => toggleSite(evses)}
              title={allOn ? `Deselect all ${site} EVSEs` : `Select all ${site} EVSEs`}
              className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide transition-colors ${
                allOn ? 'text-blue-700 bg-blue-50' : 'text-gray-400 hover:text-blue-600 hover:bg-blue-50'
              }`}
            >
              {site}
            </button>
            <div className="flex flex-wrap justify-center gap-1.5">
              {evses.map(ev => (
                <button
                  key={ev.id}
                  onClick={() => toggleOne(ev.id)}
                  className={pillCls(selected.includes(ev.id))}
                >
                  {ev.name}
                </button>
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}
