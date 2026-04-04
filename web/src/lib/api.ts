/**
 * Thin API client — wraps fetch with Supabase Bearer token auth.
 * Gets the current session token from the Supabase client and sends it
 * as Authorization: Bearer so the backend can validate it regardless of
 * whether the browser has a Supabase cookie set.
 */

import { supabase } from './supabase'

const BASE = import.meta.env.VITE_API_BASE ?? ''

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // Get the current session token from Supabase (stored in localStorage)
  const { data: { session } } = await supabase.auth.getSession()

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string> ?? {}),
  }
  if (session?.access_token) {
    headers['Authorization'] = `Bearer ${session.access_token}`
  }

  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers,
    ...init,
  })
  if (!res.ok) {
    if (res.status === 401) {
      // Session is invalid — sign out so onAuthStateChange fires and
      // App.tsx shows the LoginScreen inline (no /login redirect needed)
      await supabase.auth.signOut()
    }
    throw new Error(`API error ${res.status}: ${path}`)
  }
  return res.json() as Promise<T>
}

// ── Sessions ──────────────────────────────────────────────────────────────────

export interface ChargingSession {
  transaction_id: string
  station_id: string
  evse_name: string
  location: string
  connector_id: number | null
  connector_type: string
  start_dt: string
  end_dt: string | null
  duration_min: number | null
  max_power_kw: number | null
  energy_kwh: number | null
  soc_start: number | null
  soc_end: number | null
  id_tag: string | null
  est_revenue_usd: number | null
}

export interface SessionsResponse {
  sessions: ChargingSession[]
  total: number
  page: number
  page_size: number
  total_energy_kwh: number
  total_revenue_usd: number
  avg_duration_min: number | null
}

export function fetchSessions(params?: {
  page?: number
  page_size?: number
  start_date?: string
  end_date?: string
  station_id?: string[]
}): Promise<SessionsResponse> {
  const qs = new URLSearchParams()
  if (params?.page)       qs.set('page', String(params.page))
  if (params?.page_size)  qs.set('page_size', String(params.page_size))
  if (params?.start_date) qs.set('start_date', params.start_date)
  if (params?.end_date)   qs.set('end_date', params.end_date)
  params?.station_id?.forEach(s => qs.append('station_id', s))
  return apiFetch<SessionsResponse>(`/api/sessions?${qs}`)
}

// ── Status ────────────────────────────────────────────────────────────────────

export interface StatusEvent {
  id: number
  station_id: string
  evse_name: string
  connector_id: number | null
  status: string
  error_code: string | null
  vendor_error_code: string | null
  vendor_error_description: string | null
  received_at: string
  received_at_ak: string
}

export interface StatusHistoryResponse {
  events: StatusEvent[]
  total: number
}

export function fetchStatusHistory(params?: {
  limit?: number
  station_id?: string[]
  include_no_error?: boolean
}): Promise<StatusHistoryResponse> {
  const qs = new URLSearchParams()
  if (params?.limit)            qs.set('limit', String(params.limit))
  if (params?.include_no_error) qs.set('include_no_error', 'true')
  params?.station_id?.forEach(s => qs.append('station_id', s))
  return apiFetch<StatusHistoryResponse>(`/api/status?${qs}`)
}

// ── Connectivity ──────────────────────────────────────────────────────────────

export interface ConnectivityRecord {
  station_id: string
  evse_name: string
  location: string
  last_seen_utc: string | null
  last_seen_ak: string | null
  last_action: string | null
  connection_id: string | null
  minutes_since_last_message: number | null
  is_online: boolean
}

export interface ConnectivityResponse {
  chargers: ConnectivityRecord[]
  as_of_utc: string
}

export function fetchConnectivity(): Promise<ConnectivityResponse> {
  return apiFetch<ConnectivityResponse>('/api/connectivity')
}

// ── Session Detail (meter value time-series) ──────────────────────────────────

export interface MeterValuePoint {
  ts_ak: string
  power_kw: number | null
  power_offered_kw: number | null
  current_offered_a: number | null
  soc: number | null
  energy_kwh_delta: number | null
  voltage_v: number | null
}

export interface SessionDetailResponse {
  station_id: string
  evse_name: string
  transaction_id: string
  start_dt: string
  end_dt: string | null
  points: MeterValuePoint[]
}

export function fetchSessionDetail(params: {
  station_id: string
  transaction_id: string
  connector_id?: number | null
}): Promise<SessionDetailResponse> {
  const qs = new URLSearchParams({
    station_id:     params.station_id,
    transaction_id: params.transaction_id,
  })
  if (params.connector_id != null) qs.set('connector_id', String(params.connector_id))
  return apiFetch<SessionDetailResponse>(`/api/sessions/detail?${qs}`)
}

// ── Analytics (daily totals + start density) ──────────────────────────────────

export interface DailyTotal {
  date: string        // "YYYY-MM-DD"
  count: number
  energy_kwh: number
}

export interface DensityPoint {
  dow: number         // 0 = Sun … 6 = Sat
  hour: number        // 0–23
  count: number
}

export interface AnalyticsResponse {
  daily_totals: DailyTotal[]
  density: DensityPoint[]
}

export function fetchAnalytics(params?: {
  start_date?: string
  end_date?: string
  station_id?: string[]
}): Promise<AnalyticsResponse> {
  const qs = new URLSearchParams()
  if (params?.start_date) qs.set('start_date', params.start_date)
  if (params?.end_date)   qs.set('end_date', params.end_date)
  params?.station_id?.forEach(s => qs.append('station_id', s))
  return apiFetch<AnalyticsResponse>(`/api/analytics?${qs}`)
}

// ── EVSE options (matches constants.py — shared between SessionsTab & ExportTab) ─

export const EVSE_OPTIONS = [
  { id: 'as_c8rCuPHDd7sV1ynHBVBiq', name: 'ARG - Right',   location: 'ARG' },
  { id: 'as_cnIGqQ0DoWdFCo7zSrN01', name: 'ARG - Left',    location: 'ARG' },
  { id: 'as_oXoa7HXphUu5riXsSW253', name: 'Delta - Right', location: 'Delta Jct' },
  { id: 'as_xTUHfTKoOvKSfYZhhdlhT', name: 'Delta - Left',  location: 'Delta Jct' },
  { id: 'as_LYHe6mZTRKiFfziSNJFvJ', name: 'Glennallen',    location: 'Glennallen' },
]

// ── Export ────────────────────────────────────────────────────────────────────

export function buildExportUrl(params: {
  start_date: string
  end_date: string
  format?: 'csv' | 'xlsx'
  station_id?: string[]
}): string {
  const qs = new URLSearchParams({
    start_date: params.start_date,
    end_date:   params.end_date,
    format:     params.format ?? 'csv',
  })
  params.station_id?.forEach(s => qs.append('station_id', s))
  return `${BASE}/api/export?${qs}`
}

// ── Alerts Config & History ────────────────────────────────────────────────

export type AlertType = 'offline_idle' | 'offline_mid_session' | 'fault' | 'suspicious_vid'

export interface AlertSubscription {
  alert_type: AlertType
  enabled: boolean
}

export interface AlertSubscriptionsResponse {
  email: string
  subscriptions: AlertSubscription[]
}

export interface FiredAlert {
  id: string
  fired_at_ak: string
  alert_type: string
  evse_name: string
  message: string
}

export interface AlertHistoryResponse {
  alerts: FiredAlert[]
}

export function fetchAlertSubscriptions(): Promise<AlertSubscriptionsResponse> {
  return apiFetch<AlertSubscriptionsResponse>('/api/alerts/subscriptions')
}

export function saveAlertSubscriptions(
  subscriptions: AlertSubscription[],
): Promise<AlertSubscriptionsResponse> {
  return apiFetch<AlertSubscriptionsResponse>('/api/alerts/subscriptions', {
    method:  'POST',
    body:    JSON.stringify(subscriptions),
  })
}

export function fetchAlertHistory(): Promise<AlertHistoryResponse> {
  return apiFetch<AlertHistoryResponse>('/api/alerts/history')
}

// ── Admin ──────────────────────────────────────────────────────────────────────

export interface AdminUser {
  id: string
  email: string
  name: string | null
  allowed_evse_ids: string[] | null
  active: boolean
  created_at: string
}

export interface AdminPricing {
  id: string
  station_id: string
  connection_fee: number | null
  price_per_kwh: number | null
  price_per_min: number | null
  idle_fee_per_min: number | null
  effective_start: string | null
  effective_end: string | null
}

export interface AdminEvse {
  station_id: string
  display_name: string
  location: string
  platform: string
  archived: boolean
}

export interface AdminUnidentifiedEvse {
  station_id: string
  last_seen_ak: string
}

// Users
export const fetchAdminUsers = (): Promise<AdminUser[]> =>
  apiFetch<AdminUser[]>('/api/admin/users')

export const createAdminUser = (body: {
  email: string; name?: string; allowed_evse_ids?: string[] | null; active?: boolean
}): Promise<AdminUser> =>
  apiFetch<AdminUser>('/api/admin/users', { method: 'POST', body: JSON.stringify(body) })

export const updateAdminUser = (
  id: string,
  body: Partial<{ email: string; name: string; allowed_evse_ids: string[] | null; active: boolean }>,
): Promise<AdminUser> =>
  apiFetch<AdminUser>(`/api/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

// Pricing
export const fetchAdminPricing = (): Promise<AdminPricing[]> =>
  apiFetch<AdminPricing[]>('/api/admin/pricing')

export const createAdminPricing = (body: {
  station_id: string; connection_fee?: number | null; price_per_kwh?: number | null
  price_per_min?: number | null; idle_fee_per_min?: number | null
  effective_start: string; effective_end?: string | null
}): Promise<AdminPricing> =>
  apiFetch<AdminPricing>('/api/admin/pricing', { method: 'POST', body: JSON.stringify(body) })

export const updateAdminPricing = (
  id: string,
  body: Partial<{ connection_fee: number | null; price_per_kwh: number | null
    price_per_min: number | null; idle_fee_per_min: number | null
    effective_start: string; effective_end: string | null }>,
): Promise<AdminPricing> =>
  apiFetch<AdminPricing>(`/api/admin/pricing/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

// EVSEs
export const fetchAdminEvse = (): Promise<AdminEvse[]> =>
  apiFetch<AdminEvse[]>('/api/admin/evse')

export const fetchAdminUnidentifiedEvse = (): Promise<AdminUnidentifiedEvse[]> =>
  apiFetch<AdminUnidentifiedEvse[]>('/api/admin/evse/unidentified')

export const upsertAdminEvse = (body: {
  station_id: string; display_name?: string; location?: string
  platform?: string; archived?: boolean
}): Promise<{ ok: boolean; station_id: string }> =>
  apiFetch('/api/admin/evse', { method: 'PUT', body: JSON.stringify(body) })

// ── Connectivity History ───────────────────────────────────────────────────────

export interface ConnectivityEvent {
  station_id: string
  evse_name: string
  location: string
  connector_id: number | null
  connection_id: string | null
  received_at_ak: string
  event: string
}

export interface ConnectivityHistoryResponse {
  events: ConnectivityEvent[]
  total: number
}

export function fetchConnectivityHistory(params: {
  start_date: string
  end_date: string
  station_id?: string[]
}): Promise<ConnectivityHistoryResponse> {
  const q = new URLSearchParams()
  q.set('start_date', params.start_date)
  q.set('end_date',   params.end_date)
  params.station_id?.forEach(s => q.append('station_id', s))
  return apiFetch<ConnectivityHistoryResponse>(`/api/connectivity/history?${q}`)
}

// ── Utility Accounts & Credentials ────────────────────────────────────────────

export type UtilityName = 'gvea' | 'cvea' | 'cea'

export const UTILITY_LABELS: Record<UtilityName, string> = {
  gvea: 'GVEA (SmartHub)',
  cvea: 'CVEA (SmartHub)',
  cea:  'CEA (mymeterQ)',
}

export interface UtilityAccount {
  id: number
  utility: UtilityName
  account_number: string
  display_name: string
  service_location_number: string | null
  customer_number: string | null
  system_of_record: string
  meter_group_id: string | null
  enabled: boolean
  last_collected: string | null
  last_error: string | null
  created_at: string
}

export interface UtilityCredential {
  utility: UtilityName
  username: string
  updated_at: string
}

export interface UtilityUsageRow {
  utility: string
  account_number: string
  meter_id: string | null
  interval_start: string
  interval_end: string
  kwh: number | null
  is_estimated: boolean
  granularity_min: number
  collected_at: string
}

// Accounts
export const fetchUtilityAccounts = (): Promise<UtilityAccount[]> =>
  apiFetch<UtilityAccount[]>('/api/utility/accounts')

export const createUtilityAccount = (body: {
  utility: string
  account_number: string
  display_name?: string
  service_location_number?: string | null
  customer_number?: string | null
  meter_group_id?: string | null
  enabled?: boolean
}): Promise<UtilityAccount> =>
  apiFetch<UtilityAccount>('/api/utility/accounts', {
    method: 'POST', body: JSON.stringify(body),
  })

export const updateUtilityAccount = (
  id: number,
  body: Partial<{
    display_name: string
    service_location_number: string | null
    customer_number: string | null
    meter_group_id: string | null
    enabled: boolean
  }>,
): Promise<UtilityAccount> =>
  apiFetch<UtilityAccount>(`/api/utility/accounts/${id}`, {
    method: 'PATCH', body: JSON.stringify(body),
  })

export const deleteUtilityAccount = (id: number): Promise<void> =>
  apiFetch<void>(`/api/utility/accounts/${id}`, { method: 'DELETE' })

// Credentials
export const fetchUtilityCredentials = (): Promise<UtilityCredential[]> =>
  apiFetch<UtilityCredential[]>('/api/utility/credentials')

export const upsertUtilityCredentials = (
  utility: string,
  body: { username: string; password: string },
): Promise<UtilityCredential> =>
  apiFetch<UtilityCredential>(`/api/utility/credentials/${utility}`, {
    method: 'PUT', body: JSON.stringify(body),
  })

// Manual collection trigger
export const triggerUtilityCollect = (days_back = 2): Promise<{ ok: boolean; message: string }> =>
  apiFetch('/api/utility/collect', {
    method: 'POST', body: JSON.stringify({ days_back }),
  })

// ── Maintenance Tracker ────────────────────────────────────────────────────────

export interface WarrantyStatus {
  status: 'in_warranty' | 'expiring_soon' | 'out_of_warranty' | 'warranty_unknown' | 'no_warranty'
  label: string
  color: 'green' | 'amber' | 'gray'
  days_remaining?: number
  asterisk?: boolean
}

export interface MaintenanceUnit {
  id: string
  external_id: string | null
  name: string
  serial_number: string | null
  status: 'active' | 'retired'
  site_name: string | null
  site_id: string | null
  unit_type_id: string | null
  unit_type_name: string | null
  manufacturer: string | null
  hyperdoc_required: boolean
  maintenance_responsibility: string
  network_platform: string | null
  owner_name: string | null
  warranty_end: string | null
  warranty_start: string | null
  warranty_notes: string | null
  warranty: WarrantyStatus
  commission_date: string | null
  retired_at: string | null
  retired_reason: string | null
  // PM status
  last_pm_quarterly: string | null
  last_pm_semi_annual: string | null
  last_pm_annual: string | null
  next_pm_quarterly_due: string | null
  next_pm_semi_annual_due: string | null
  next_pm_annual_due: string | null
  next_pm_due_date: string | null
  next_pm_due_type: 'quarterly' | 'semi_annual' | 'annual' | null
  pm_overdue: boolean
  // Alert badges
  service_needed: boolean
  parts_on_order: boolean
  hyperdoc_pending: boolean
  pm_template_pending: boolean
}

export interface MaintenanceOverviewResponse {
  chargers: MaintenanceUnit[]
}

export interface PartReplaced {
  part_name: string
  part_number: string | null
  action_taken: 'replaced' | 'repaired' | 'cleaned' | 'adjusted'
  notes: string | null
}

export interface MaintenanceRecord {
  id: string
  record_timestamp: string
  record_type: string
  overall_result: 'pass' | 'conditional' | 'fail' | null
  firmware_version: string | null
  technician_name: string
  work_description: string | null
  onsite_hours: number | null
  mobilized_hours: number | null
  additional_work_needed: boolean
  planned_future_work: string | null
  hyperdoc_required: boolean
  hyperdoc_submitted: boolean
  hyperdoc_submitted_at: string | null
  pm_template_version: string | null
  site_name: string | null
  parts: PartReplaced[]
}

export interface MaintenanceUnitDetail {
  charger: MaintenanceUnit & {
    interval_quarterly_months: number | null
    interval_semiannual_months: number | null
    interval_annual_months: number | null
    network_platform_notes: string | null
    port_count: number | null
  }
  maintenance_records: MaintenanceRecord[]
  location_history: { id: string; assigned_at: string; site_name: string | null; notes: string | null }[]
}

export interface PMTemplateTask {
  id: string
  task_code: string
  task_order: number
  task_category: string
  task_name: string
  task_description: string
  input_type: 'pass_fail' | 'completed' | 'measured_value' | 'text' | 'pass_fail_action'
  unit_of_measure: string | null
  is_required: boolean
  is_conditional: boolean
  conditional_label: string | null
  critical_fail: boolean
  fail_guidance: string | null
}

export interface PMTemplateResponse {
  template: { id: string; template_name: string; pm_interval: string; template_version: string } | null
  tasks: PMTemplateTask[]
  fallback: 'general_inspection' | null
}

export interface TaskResult {
  task_id: string
  result_pass_fail?: 'pass' | 'fail' | 'na' | null
  result_completed?: boolean | null
  result_measured_value?: string | null
  result_text?: string | null
  task_notes?: string | null
}

export interface SubmitRecordBody {
  charger_id: string
  record_type: string
  pm_template_id?: string | null
  pm_template_version?: string | null
  overall_result?: 'pass' | 'conditional' | 'fail' | null
  firmware_version?: string | null
  technician_name: string
  work_description?: string | null
  onsite_hours?: number | null
  mobilized_hours?: number | null
  additional_work_needed: boolean
  planned_future_work?: string | null
  task_results: TaskResult[]
  parts: { part_name: string; part_number?: string | null; action_taken: string; notes?: string | null }[]
}

export interface FleetUnitType {
  id: string
  type_name: string
  manufacturer: string
  mirror_type_id: string | null
  default_pm_template_id: string | null
  interval_quarterly_months: number | null
  interval_semiannual_months: number | null
  interval_annual_months: number
  hyperdoc_required: boolean
  notes: string | null
  is_active: boolean
  annual_template_name: string | null
  effective_template_name: string | null
  unit_count: number
}

export interface Site {
  id: string
  name: string
}

// Maintenance overview
export const fetchMaintenanceOverview = (showRetired = false): Promise<MaintenanceOverviewResponse> =>
  apiFetch<MaintenanceOverviewResponse>(`/api/maintenance/overview?show_retired=${showRetired}`)

// Unit detail
export const fetchMaintenanceUnit = (chargerId: string): Promise<MaintenanceUnitDetail> =>
  apiFetch<MaintenanceUnitDetail>(`/api/maintenance/units/${chargerId}`)

// PM template tasks
export const fetchPMTemplate = (chargerId: string, pmType: string): Promise<PMTemplateResponse> =>
  apiFetch<PMTemplateResponse>(`/api/maintenance/templates/${chargerId}/${pmType}`)

// Submit maintenance record
export const submitMaintenanceRecord = (body: SubmitRecordBody): Promise<{ id: string; record_timestamp: string }> =>
  apiFetch('/api/maintenance/records', { method: 'POST', body: JSON.stringify(body) })

// Update Hyperdoc submission
export const updateHyperdocSubmission = (recordId: string, submittedAt: string): Promise<{ ok: boolean }> =>
  apiFetch(`/api/maintenance/records/${recordId}/hyperdoc`, {
    method: 'PATCH', body: JSON.stringify({ submitted_at: submittedAt }),
  })

// Sites list
export const fetchSites = (): Promise<Site[]> =>
  apiFetch<Site[]>('/api/maintenance/sites')

// Admin fleet — unit types
export const fetchFleetUnitTypes = (): Promise<FleetUnitType[]> =>
  apiFetch<FleetUnitType[]>('/api/admin/fleet/unit-types')

export const createFleetUnitType = (body: {
  type_name: string; manufacturer: string; mirror_type_id?: string | null
  interval_quarterly_months?: number | null; interval_semiannual_months?: number | null
  interval_annual_months: number; hyperdoc_required?: boolean
  notes?: string | null; is_active?: boolean
}): Promise<{ id: string; type_name: string }> =>
  apiFetch('/api/admin/fleet/unit-types', { method: 'POST', body: JSON.stringify(body) })

export const updateFleetUnitType = (
  id: string,
  body: Partial<{ type_name: string; manufacturer: string; mirror_type_id: string | null
    interval_quarterly_months: number | null; interval_semiannual_months: number | null
    interval_annual_months: number; hyperdoc_required: boolean
    notes: string | null; is_active: boolean }>,
): Promise<{ id: string; type_name: string }> =>
  apiFetch(`/api/admin/fleet/unit-types/${id}`, { method: 'PATCH', body: JSON.stringify(body) })

// Admin fleet — onboard unit
export const onboardFleetUnit = (body: {
  serial_number: string; name: string; unit_type_id: string; site_id: string
  commission_date?: string | null; warranty_start?: string | null; warranty_end?: string | null
  warranty_notes?: string | null; owner_name?: string | null
  maintenance_responsibility?: string; network_platform?: string | null
  network_platform_notes?: string | null; port_count?: number | null
}): Promise<{ id: string; name: string; serial_number: string }> =>
  apiFetch('/api/admin/fleet/onboard', { method: 'POST', body: JSON.stringify(body) })

// Admin fleet — move / retire
export const moveFleetUnit = (chargerId: string, body: { site_id: string; notes?: string | null }) =>
  apiFetch(`/api/admin/fleet/units/${chargerId}/move`, { method: 'POST', body: JSON.stringify(body) })

export const retireFleetUnit = (chargerId: string, reason: string) =>
  apiFetch(`/api/admin/fleet/units/${chargerId}/retire`, {
    method: 'POST', body: JSON.stringify({ retired_reason: reason }),
  })

export const patchFleetUnit = (
  chargerId: string,
  body: Partial<{ parts_on_order: boolean; warranty_notes: string | null; network_platform_notes: string | null }>,
) =>
  apiFetch(`/api/admin/fleet/units/${chargerId}`, { method: 'PATCH', body: JSON.stringify(body) })
