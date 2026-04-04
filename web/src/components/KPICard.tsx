/** KPI metric card — Tremor Blocks inspired. */

import { ReactNode } from 'react'

interface KPICardProps {
  title: string
  value: string | number
  subtitle?: string
  icon?: ReactNode
  trend?: { value: string; positive?: boolean }
  color?: 'blue' | 'green' | 'amber' | 'red' | 'gray'
}

const colorMap = {
  blue:  'text-blue-600 bg-blue-50',
  green: 'text-emerald-600 bg-emerald-50',
  amber: 'text-amber-600 bg-amber-50',
  red:   'text-red-600 bg-red-50',
  gray:  'text-gray-600 bg-gray-100',
}

export function KPICard({ title, value, subtitle, icon, trend, color = 'blue' }: KPICardProps) {
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5 flex flex-col gap-2 shadow-sm">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium text-gray-500">{title}</span>
        {icon && (
          <span className={`p-2 rounded-lg ${colorMap[color]}`}>
            {icon}
          </span>
        )}
      </div>
      <div className="text-2xl font-bold text-gray-900 tabular-nums">{value}</div>
      {subtitle && <div className="text-xs text-gray-400">{subtitle}</div>}
      {trend && (
        <div className={`text-xs font-medium ${trend.positive ? 'text-emerald-600' : 'text-red-600'}`}>
          {trend.value}
        </div>
      )}
    </div>
  )
}
