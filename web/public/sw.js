/**
 * ReCharge Alaska Portal — service worker.
 *
 * PUSH ONLY, ON PURPOSE. This worker deliberately does not cache anything.
 * The deploy flow copies a fresh dist/ into the container (see DEPLOY.md), and
 * a cache-first shell would keep serving the previous build to installed
 * devices until the cache was manually versioned and busted. A push-only worker
 * has no such failure mode: every navigation still goes to the network.
 *
 * Scope: served from /app/sw.js in production, so its scope is /app/ — the
 * whole SPA. In dev (vite, base "/") it is served from /sw.js.
 */

// Take over immediately so a redeployed worker doesn't sit in "waiting" while
// the user wonders why notifications stopped.
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()))

// ── Incoming push ─────────────────────────────────────────────────────────────

self.addEventListener('push', event => {
  let data = {}
  try {
    data = event.data ? event.data.json() : {}
  } catch {
    data = {}
  }

  const title = data.title || 'ReCharge Alaska Alert'
  const bodyLines = [data.evse_name, data.message].filter(Boolean)

  event.waitUntil(
    self.registration.showNotification(title, {
      body: bodyLines.join('\n'),
      icon: './icon-192.png',
      badge: './icon-192.png',
      // Same tag ⇒ a repeat alert for the same charger replaces the previous
      // notification instead of stacking another one on the lock screen.
      tag: data.tag || 'rca-alert',
      renotify: true,
      timestamp: Date.now(),
      data: { url: data.url || './', alert_type: data.alert_type || '' },
    }),
  )
})

// ── Tapping the notification ──────────────────────────────────────────────────

self.addEventListener('notificationclick', event => {
  event.notification.close()

  const target = new URL(
    (event.notification.data && event.notification.data.url) || './',
    self.registration.scope,
  ).href

  event.waitUntil(
    self.clients
      .matchAll({ type: 'window', includeUncontrolled: true })
      .then(clientList => {
        // Reuse an already-open window if we have one — on iOS, opening a second
        // window of an installed PWA is not possible anyway.
        for (const client of clientList) {
          if (client.url.startsWith(self.registration.scope) && 'focus' in client) {
            return client.focus()
          }
        }
        return self.clients.openWindow ? self.clients.openWindow(target) : undefined
      }),
  )
})

// ── Subscription rotation ─────────────────────────────────────────────────────
// Push services occasionally rotate a subscription. The worker cannot re-register
// with the API on its own (it has no Supabase session to authenticate with), so
// recovery is handled app-side: src/lib/push.ts re-sends the current subscription
// to /api/alerts/push/subscribe on every launch, and the endpoint upserts.
self.addEventListener('pushsubscriptionchange', () => {
  // Intentionally empty — see above.
})
