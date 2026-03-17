/**
 * Main app shell — tab navigation + auth guard.
 * Tabs: Charging Sessions | Status History | Connectivity | Data Export | Alerts | Admin*
 * * Admin tab only visible to kris.hall@rechargealaska.net
 */

import { useState, useEffect } from 'react'
import { supabase } from './lib/supabase'
import { SessionsTab }     from './pages/SessionsTab'
import { StatusTab }       from './pages/StatusTab'
import { ConnectivityTab } from './pages/ConnectivityTab'
import { ExportTab }       from './pages/ExportTab'
import { AlertsTab }       from './pages/AlertsTab'
import { AdminTab }        from './pages/AdminTab'
import { AlertBanner }     from './components/AlertBanner'
import { LogOut, Zap, Mail } from 'lucide-react'

// DEV: bypass auth so the dashboard is visible during local review
const DEV_BYPASS_AUTH = import.meta.env.DEV

const ADMIN_EMAIL = 'kris.hall@rechargealaska.net'

type Tab = 'sessions' | 'status' | 'connectivity' | 'export' | 'alerts' | 'admin'

const BASE_TABS: { id: Tab; label: string }[] = [
  { id: 'sessions',     label: 'Charging Sessions' },
  { id: 'status',       label: 'Status History' },
  { id: 'connectivity', label: 'Connectivity' },
  { id: 'export',       label: 'Data Export' },
  { id: 'alerts',       label: 'Alerts' },
]

const ADMIN_TAB: { id: Tab; label: string } = { id: 'admin', label: 'Admin' }

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>('sessions')
  const [userEmail, setUserEmail] = useState<string | null>(null)
  const [loading,   setLoading]   = useState(true)

  useEffect(() => {
    if (DEV_BYPASS_AUTH) {
      setUserEmail(ADMIN_EMAIL)   // dev mode: show all tabs including Admin
      setLoading(false)
      return
    }

    supabase.auth.getSession().then(({ data }) => {
      setUserEmail(data.session?.user?.email ?? null)
      setLoading(false)
    })

    const { data: listener } = supabase.auth.onAuthStateChange((_event, session) => {
      setUserEmail(session?.user?.email ?? null)
    })

    return () => listener.subscription.unsubscribe()
  }, [])

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gray-50">
        <div className="text-sm text-gray-400">Loading…</div>
      </div>
    )
  }

  if (!userEmail) {
    return <LoginScreen />
  }

  const isAdmin = userEmail === ADMIN_EMAIL
  const visibleTabs = isAdmin ? [...BASE_TABS, ADMIN_TAB] : BASE_TABS

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b border-gray-200 sticky top-0 z-40">
        <div className="max-w-screen-2xl mx-auto px-6 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 rounded-lg bg-blue-600 flex items-center justify-center">
              <Zap size={14} className="text-white" />
            </div>
            <span className="font-semibold text-gray-900 text-sm">ReCharge Alaska</span>
            <span className="text-gray-300 text-xs ml-1">v3</span>
          </div>

          <div className="flex items-center gap-4">
            {userEmail && (
              <span className="text-xs text-gray-500 hidden sm:block">{userEmail}</span>
            )}
            <button
              onClick={() => supabase.auth.signOut()}
              className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-gray-800 transition-colors"
            >
              <LogOut size={13} />
              Sign out
            </button>
          </div>
        </div>

        {/* Tab bar */}
        <div className="max-w-screen-2xl mx-auto px-6">
          <nav className="flex gap-0 -mb-px overflow-x-auto">
            {visibleTabs.map(tab => (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`px-4 py-3 text-sm font-medium transition-colors whitespace-nowrap ${
                  activeTab === tab.id ? 'tab-active' : 'tab-inactive'
                }`}
              >
                {tab.label}
              </button>
            ))}
          </nav>
        </div>
      </header>

      {/* Main content */}
      <main className="max-w-screen-2xl mx-auto px-6 py-6">
        {activeTab === 'sessions'     && <SessionsTab />}
        {activeTab === 'status'       && <StatusTab />}
        {activeTab === 'connectivity' && <ConnectivityTab />}
        {activeTab === 'export'       && <ExportTab />}
        {activeTab === 'alerts'       && <AlertsTab />}
        {activeTab === 'admin'        && isAdmin && <AdminTab />}
      </main>

      {/* Alert toast overlay */}
      <AlertBanner />
    </div>
  )
}

// ── Login screen (magic link) ──────────────────────────────────────────────────
function LoginScreen() {
  const [email,   setEmail]   = useState('')
  const [sent,    setSent]    = useState(false)
  const [loading, setLoading] = useState(false)
  const [error,   setError]   = useState<string | null>(null)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setLoading(true)
    setError(null)
    const { error } = await supabase.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: window.location.href },
    })
    setLoading(false)
    if (error) setError(error.message)
    else setSent(true)
  }

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
      <div className="bg-white rounded-2xl border border-gray-200 shadow-sm p-8 w-full max-w-sm">
        {/* Logo */}
        <div className="flex items-center gap-2.5 mb-8">
          <div className="w-8 h-8 rounded-lg bg-blue-600 flex items-center justify-center">
            <Zap size={16} className="text-white" />
          </div>
          <span className="font-semibold text-gray-900">ReCharge Alaska</span>
          <span className="text-gray-300 text-xs">v3</span>
        </div>

        {sent ? (
          <div className="text-center">
            <div className="w-12 h-12 rounded-full bg-emerald-50 flex items-center justify-center mx-auto mb-4">
              <Mail size={20} className="text-emerald-600" />
            </div>
            <h2 className="text-sm font-semibold text-gray-900 mb-1">Check your email</h2>
            <p className="text-xs text-gray-500">
              We sent a magic link to <strong>{email}</strong>.<br />
              Click the link to sign in.
            </p>
          </div>
        ) : (
          <>
            <h2 className="text-sm font-semibold text-gray-900 mb-1">Sign in</h2>
            <p className="text-xs text-gray-500 mb-6">
              Enter your email and we'll send you a magic link.
            </p>
            <form onSubmit={handleSubmit} className="space-y-3">
              <input
                type="email"
                required
                placeholder="you@rechargealaska.net"
                value={email}
                onChange={e => setEmail(e.target.value)}
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm text-gray-800 focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              {error && (
                <p className="text-xs text-red-500">{error}</p>
              )}
              <button
                type="submit"
                disabled={loading}
                className="w-full bg-blue-600 text-white rounded-lg px-4 py-2 text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-50"
              >
                {loading ? 'Sending…' : 'Send magic link'}
              </button>
            </form>
          </>
        )}
      </div>
    </div>
  )
}

