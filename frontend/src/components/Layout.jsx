import { useEffect, useState } from 'react'
import { ChevronDown, Cpu, Gauge, X } from 'lucide-react'
import toast from 'react-hot-toast'
import { getLlmStatus, updateLlmRuntime } from '../api/client.js'
import Sidebar from './Sidebar.jsx'
import { modelDisplayName } from '../modelDisplay.js'

function LlmStatusChip() {
  const [status, setStatus] = useState(null)
  const [open, setOpen] = useState(false)
  const [model, setModel] = useState('')
  const [profile, setProfile] = useState('balanced')
  const [saving, setSaving] = useState(false)
  const adoptStatus = (next) => {
    setStatus(next)
    setModel(next.runtime?.selected_model || next.active_model || '')
    setProfile(next.runtime?.selected_profile || 'balanced')
  }

  useEffect(() => {
    getLlmStatus().then(adoptStatus).catch(() => setStatus({ available: false }))
  }, [])

  if (!status) return null
  const profiles = status.model_profiles?.[model] || status.runtime?.profiles || []
  const selectedProfile = profiles.find((item) => item.id === profile) || profiles[0]
  const apply = async () => {
    setSaving(true)
    try {
      adoptStatus(await updateLlmRuntime(model, profile))
      setOpen(false)
      toast.success('LLM runtime updated for new jobs')
    } catch (error) {
      toast.error(error?.response?.data?.detail || 'Unable to update LLM runtime')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="relative">
      <button onClick={() => setOpen((value) => !value)} className={`portal-runtime-chip inline-flex items-center gap-2 rounded-full border px-3.5 py-2 text-xs shadow-sm ${status.available ? 'is-online' : 'is-offline'}`}>
        <span className={`h-2 w-2 rounded-full ${status.available ? 'bg-emerald-400' : 'bg-amber-400'}`} />
        {status.available ? (
          <><strong className="font-semibold text-ink">{modelDisplayName(status.active_model || status.recommended)}</strong><span className="text-slate-400">·</span><span className="capitalize text-slate-500">{status.runtime?.selected_profile || 'balanced'}</span><ChevronDown className="h-3.5 w-3.5" /></>
        ) : (
          <><span>LLM offline</span><span className="text-slate-400">·</span><span>Check Ollama</span></>
        )}
      </button>
      {open && status.available && (
        <div className="absolute right-0 top-11 z-50 w-[360px] rounded-lg border border-hairline bg-white p-4 text-slate-800 shadow-lg">
          <div className="mb-4 flex items-start justify-between gap-4">
            <div><p className="font-semibold">Generation runtime</p><p className="mt-1 text-xs text-slate-500">Settings apply to new jobs and stay fixed while generation is active.</p></div>
            <button onClick={() => setOpen(false)} className="rounded-lg p-1 hover:bg-slate-100"><X className="h-4 w-4" /></button>
          </div>
          <label className="mb-1 block text-xs font-semibold text-slate-600">Installed LLM model</label>
          <select value={model} onChange={(event) => { setModel(event.target.value); setProfile('balanced') }} className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm outline-none focus:border-blue-500">
            {(status.models || []).map((item) => <option key={item} value={item}>{modelDisplayName(item)}</option>)}
          </select>
          <label className="mb-1 mt-4 block text-xs font-semibold text-slate-600">Optimization profile</label>
          <select value={profile} onChange={(event) => setProfile(event.target.value)} className="w-full rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-sm outline-none focus:border-blue-500">
            {profiles.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
          </select>
          {selectedProfile && (
            <div className="mt-4 rounded-xl bg-slate-50 p-3">
              <p className="text-xs text-slate-600">{selectedProfile.description}</p>
              <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                <span className="flex items-center gap-1.5"><Gauge className="h-3.5 w-3.5 text-blue-600" />{selectedProfile.inference_lanes} inference lane(s)</span>
                <span className="flex items-center gap-1.5"><Cpu className="h-3.5 w-3.5 text-blue-600" />{selectedProfile.file_workers} file worker(s)</span>
                <span>{selectedProfile.member_workers} member worker(s)</span><span>{selectedProfile.validation_workers} validation worker(s)</span>
              </div>
            </div>
          )}
          <button onClick={apply} disabled={saving || !model} className="mt-4 w-full rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-50">{saving ? 'Applying…' : 'Apply to new jobs'}</button>
        </div>
      )}
    </div>
  )
}

export default function Layout({ children }) {
  return (
    <div className="flex h-screen overflow-hidden bg-bg">
      <Sidebar />
      <div className="relative flex min-w-0 flex-1 flex-col overflow-hidden">
        <header className="portal-blue-header flex h-16 shrink-0 items-center justify-end border-b border-blue-900/15 px-6 lg:px-8"><LlmStatusChip /></header>
        <div className="flex-1 overflow-y-auto">{children}</div>
      </div>
    </div>
  )
}
