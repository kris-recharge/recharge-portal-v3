/**
 * Web Push registration for the installed PWA.
 *
 * iOS/iPadOS specifics that shape this file:
 *  - Push only works when the site is installed to the Home Screen. In a plain
 *    Safari tab `window.PushManager` is undefined, so `pushSupported()` is false
 *    and the UI explains the install step instead of showing a dead toggle.
 *  - `Notification.requestPermission()` must be called from a user gesture, so
 *    `enablePush()` is only ever wired to a button's onClick.
 *  - iOS drops the push subscription when the Home Screen icon is deleted. We
 *    therefore re-send the current subscription on every launch (`syncPush`),
 *    which the API upserts by endpoint.
 */

import { subscribePush, unsubscribePush } from './api'

const SW_URL = `${import.meta.env.BASE_URL}sw.js`
const SW_SCOPE = import.meta.env.BASE_URL

/** True when this browser can register a push subscription at all. */
export function pushSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    'serviceWorker' in navigator &&
    'PushManager' in window &&
    'Notification' in window
  )
}

/**
 * True when the app is running as an installed PWA rather than a browser tab.
 * On iOS this is the difference between push being available and not.
 */
export function isStandalone(): boolean {
  if (typeof window === 'undefined') return false
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    // Safari's non-standard flag, still the only reliable signal on iOS
    (window.navigator as unknown as { standalone?: boolean }).standalone === true
  )
}

export function permissionState(): NotificationPermission | 'unsupported' {
  if (!pushSupported()) return 'unsupported'
  return Notification.permission
}

async function getRegistration(): Promise<ServiceWorkerRegistration> {
  const existing = await navigator.serviceWorker.getRegistration(SW_SCOPE)
  if (existing) return existing
  return navigator.serviceWorker.register(SW_URL, { scope: SW_SCOPE })
}

/** VAPID public keys travel as base64url; PushManager wants a Uint8Array. */
function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4)
  const normalized = (base64 + padding).replace(/-/g, '+').replace(/_/g, '/')
  const raw = window.atob(normalized)
  const output = new Uint8Array(raw.length)
  for (let i = 0; i < raw.length; i++) output[i] = raw.charCodeAt(i)
  return output
}

function serialize(sub: PushSubscription) {
  const json = sub.toJSON() as { keys?: { p256dh?: string; auth?: string } }
  return {
    endpoint: sub.endpoint,
    p256dh: json.keys?.p256dh ?? '',
    auth: json.keys?.auth ?? '',
    user_agent: navigator.userAgent,
  }
}

/**
 * Ask for permission and register this device. Must be called from a click.
 * Returns a human-readable reason on failure so the UI can say what to do.
 */
export async function enablePush(vapidPublicKey: string): Promise<{ ok: boolean; reason?: string }> {
  if (!pushSupported()) {
    return {
      ok: false,
      reason: isStandalone()
        ? 'This browser does not support push notifications.'
        : 'Add the dashboard to your Home Screen first, then open it from there.',
    }
  }
  if (!vapidPublicKey) {
    return { ok: false, reason: 'Push is not configured on the server yet.' }
  }

  const permission = await Notification.requestPermission()
  if (permission !== 'granted') {
    return {
      ok: false,
      reason:
        permission === 'denied'
          ? 'Notifications are blocked for this app. Enable them in iOS Settings → Notifications → ReCharge.'
          : 'Notification permission was dismissed.',
    }
  }

  const reg = await getRegistration()
  await navigator.serviceWorker.ready

  // Reuse the existing subscription when there is one — calling subscribe()
  // twice with the same key is fine, but this avoids a needless round-trip.
  let sub = await reg.pushManager.getSubscription()
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidPublicKey),
    })
  }

  await subscribePush(serialize(sub))
  return { ok: true }
}

/** Unregister this device (server row + browser subscription). */
export async function disablePush(): Promise<void> {
  if (!pushSupported()) return
  const reg = await navigator.serviceWorker.getRegistration(SW_SCOPE)
  const sub = await reg?.pushManager.getSubscription()
  if (!sub) return
  await unsubscribePush({ endpoint: sub.endpoint, p256dh: '', auth: '', user_agent: '' })
  await sub.unsubscribe()
}

/** True when this specific device currently holds a push subscription. */
export async function hasLocalSubscription(): Promise<boolean> {
  if (!pushSupported() || Notification.permission !== 'granted') return false
  const reg = await navigator.serviceWorker.getRegistration(SW_SCOPE)
  return !!(await reg?.pushManager.getSubscription())
}

/**
 * Called once at app start. Registers the service worker, and re-sends an
 * existing subscription so a rotated endpoint (or a row lost to a reinstall)
 * heals itself without the user touching anything. Never prompts.
 */
export async function syncPush(): Promise<void> {
  if (!pushSupported()) return
  try {
    await getRegistration()
    if (Notification.permission !== 'granted') return
    const reg = await navigator.serviceWorker.getRegistration(SW_SCOPE)
    const sub = await reg?.pushManager.getSubscription()
    if (sub) await subscribePush(serialize(sub))
  } catch {
    // Push is a convenience channel — a failure here must never break app boot.
  }
}
