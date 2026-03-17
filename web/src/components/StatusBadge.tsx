/** Online/offline/fault status badge. */

interface StatusBadgeProps {
  status: 'online' | 'offline' | 'fault' | string
}

const variants: Record<string, string> = {
  online:      'bg-emerald-100 text-emerald-700',
  offline:     'bg-red-100 text-red-700',
  fault:       'bg-amber-100 text-amber-700',
  Available:   'bg-emerald-100 text-emerald-700',
  Charging:    'bg-blue-100 text-blue-700',
  Preparing:   'bg-sky-100 text-sky-700',
  Finishing:   'bg-indigo-100 text-indigo-700',
  Unavailable: 'bg-gray-100 text-gray-500',
  Faulted:     'bg-red-100 text-red-700',
}

const dots: Record<string, string> = {
  online:    'bg-emerald-500',
  Available: 'bg-emerald-500',
  Charging:  'bg-blue-500',
  offline:   'bg-red-500',
  Faulted:   'bg-red-500',
}

export function StatusBadge({ status }: StatusBadgeProps) {
  const cls = variants[status] ?? 'bg-gray-100 text-gray-600'
  const dot = dots[status]

  return (
    <span className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}>
      {dot && <span className={`w-1.5 h-1.5 rounded-full ${dot} animate-pulse`} />}
      {status}
    </span>
  )
}
