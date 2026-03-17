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
import { LogOut, Zap } from 'lucide-react'

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
      if (!data.session) {
        window.location.href = '/login'
      }
    })

    const { data: listener } = supabase.auth.onAuthStateChange((_event, session) => {
      setUserEmail(session?.user?.email ?? null)
      if (!session) window.location.href = '/login'
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

