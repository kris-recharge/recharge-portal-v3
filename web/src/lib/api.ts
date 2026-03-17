/**
 * Thin API client — wraps fetch with auth cookie forwarding.
 * All requests are credentialed so the Supabase cookie is included.
 */

const BASE = import.meta.env.VITE_API_BASE ?? ''

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    ...init,
  })
  if (!res.ok) {
    if (res.status === 401) {
      window.location.href = '/login'
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
