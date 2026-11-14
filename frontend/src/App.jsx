// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: Modernization — frontend/src (App.jsx)
// Date: 2025-12-17
// ---------------------------------------------------------------------------
import { Orbit } from 'lucide-react'
import { useEffect, useState, createContext } from 'react'
import { Toaster } from 'react-hot-toast'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { getAuthToken, logout, validateSession } from './api/client.js'
import AnalysisPage from './pages/AnalysisPage.jsx'
import JobsPage from './pages/JobsPage.jsx'
import JobDetailPage from './pages/JobDetailPage.jsx'
import ProjectsPage from './pages/ProjectsPage.jsx'
import RequirementsDocumentationPage from './pages/RequirementsDocumentationPage.jsx'
import ModernizationHomePage from './pages/ModernizationHomePage.jsx'
import LoginPage from './pages/LoginPage.jsx'

export const AppContext = createContext(null)

// Function: App
export default function App() {
  const [authReady, setAuthReady] = useState(false)
  const [authUser, setAuthUser] = useState(null)
  const [authError, setAuthError] = useState('')

  useEffect(() => {
    let active = true
    // Function: bootstrap
    const bootstrap = async () => {
      if (!getAuthToken()) {
        if (active) setAuthReady(true)
        return
      }
      try {
        const session = await validateSession()
        if (active) setAuthUser(session.user)
      } catch (err) {
        if (active && err?.response?.status === 401) setAuthUser(null)
        else if (active) setAuthError(
          err?.response?.data?.error ||
            'The Modernization backend is unavailable. Start this module with Start-Modernization.ps1.',
        )
      } finally {
        if (active) setAuthReady(true)
      }
    }
    bootstrap()
    return () => {
      active = false
    }
  }, [])

  const signOut = async () => {
    try {
      await logout({ force: true })
    } finally {
      setAuthUser(null)
    }
  }

  useEffect(() => {
    if (!authUser) return undefined
    const expired = () => setAuthUser(null)
    window.addEventListener('modernization-session-expired', expired)
    return () => {
      window.removeEventListener('modernization-session-expired', expired)
    }
  }, [authUser])

  if (!authReady) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg px-6">
        <div className="w-full max-w-sm rounded-3xl border border-hairline bg-surface p-8 text-center shadow-sm">
          <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-gold to-gold-dim text-white shadow-[0_10px_30px_rgba(0,120,212,0.18)]">
            <Orbit className="h-6 w-6 animate-spin" />
          </div>
          <h2 className="font-display text-xl font-medium text-ink">Connecting to your workspace</h2>
          <p className="mt-2 text-sm leading-6 text-ink-muted">
            Connecting to the standalone Modernization service.
          </p>
        </div>
      </div>
    )
  }

  if (authError) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-bg px-6">
        <div className="w-full max-w-sm rounded-3xl border border-hairline bg-surface p-8 text-center shadow-sm">
          <h2 className="mb-2 font-display text-xl font-medium text-ink">Service unavailable</h2>
          <p className="mb-6 text-sm leading-6 text-ink-muted">{authError}</p>
          <button type="button" onClick={() => window.location.reload()} className="inline-flex items-center justify-center rounded-xl bg-gold px-6 py-3 text-sm font-semibold text-white transition hover:bg-gold-soft">Retry</button>
        </div>
      </div>
    )
  }

  if (!authUser) return <LoginPage onAuthenticated={setAuthUser} />

  return (
    <AppContext.Provider value={{ authUser, signOut }}>
      <BrowserRouter basename={import.meta.env.BASE_URL.replace(/\/$/, '')}>
        <Toaster
          position="top-right"
          toastOptions={{
            style: {
              background: '#ffffff',
              color: '#242424',
              border: '1px solid #d2d0ce',
              borderRadius: '14px',
              fontSize: '13px',
              fontFamily: "'Segoe UI Variable Text', 'Segoe UI', system-ui, sans-serif",
            },
          }}
        />
        <Routes>
          <Route path="/" element={<ModernizationHomePage />} />
          <Route path="/analyze" element={<AnalysisPage />} />
          <Route path="/jobs" element={<JobsPage />} />
          <Route path="/jobs/:jobId" element={<JobDetailPage />} />
          <Route path="/projects" element={<ProjectsPage />} />
          <Route path="/requirements/upload" element={<RequirementsDocumentationPage />} />
          <Route path="/requirements/brd" element={<RequirementsDocumentationPage />} />
          <Route path="/requirements/fsd" element={<RequirementsDocumentationPage />} />
          <Route path="/requirements/frd" element={<Navigate to="/requirements/fsd" replace />} />
          <Route path="/requirements/knowledge-graph" element={<RequirementsDocumentationPage />} />
          <Route path="/requirements/architecture-review" element={<RequirementsDocumentationPage />} />
          <Route path="/requirements/generated-assets" element={<RequirementsDocumentationPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AppContext.Provider>
  )
}
