import { FileKey2, LoaderCircle, LockKeyhole, Orbit, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { beginLogin, completeLogin } from '../api/client.js'

export default function LoginPage({ onAuthenticated }) {
  const [step, setStep] = useState('credentials')
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [challenge, setChallenge] = useState('')
  const [credential, setCredential] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const message = err => err?.response?.data?.error || err?.response?.data?.detail || 'Unable to sign in.'
  const verifyPassword = async event => {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const result = await beginLogin(username, password)
      setChallenge(result.challenge); setPassword(''); setStep('credential')
    } catch (err) { setError(message(err)) } finally { setBusy(false) }
  }
  const verifyFile = async event => {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const result = await completeLogin(challenge, credential)
      onAuthenticated(result.user)
    } catch (err) { setError(message(err)) } finally { setBusy(false) }
  }

  return <main className="flex min-h-screen items-center justify-center bg-slate-50 px-5 py-10">
    <section className="w-full max-w-md overflow-hidden rounded-lg border border-hairline bg-white shadow-sm">
      <div className="bg-gradient-to-br from-blue-700 to-blue-500 px-8 py-8 text-white">
        <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-white/15"><Orbit className="h-6 w-6" /></div>
        <h1 className="mt-4 text-xl font-semibold">Modernization Studio</h1>
        <p className="mt-2 text-sm leading-6 text-blue-50">Secure standalone access with a signed credential file.</p>
      </div>
      <div className="p-8">
        <div className="mb-6 flex items-center gap-3 text-xs font-semibold text-slate-500">
          <span className={`flex h-7 w-7 items-center justify-center rounded-full ${step === 'credentials' ? 'bg-blue-600 text-white' : 'bg-emerald-100 text-emerald-700'}`}>{step === 'credentials' ? '1' : '✓'}</span>
          <span>Account</span><span className="h-px flex-1 bg-slate-200" />
          <span className={`flex h-7 w-7 items-center justify-center rounded-full ${step === 'credential' ? 'bg-blue-600 text-white' : 'bg-slate-100'}`}>2</span><span>Credential file</span>
        </div>
        {step === 'credentials' ? <form onSubmit={verifyPassword} className="space-y-4">
          <div><label className="text-xs font-semibold text-slate-600">Username</label><input autoFocus autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} className="mt-1.5 w-full rounded-xl border border-slate-300 px-4 py-3 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100" required /></div>
          <div><label className="text-xs font-semibold text-slate-600">Password</label><input type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} className="mt-1.5 w-full rounded-xl border border-slate-300 px-4 py-3 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100" required /></div>
          <button disabled={busy} className="flex w-full items-center justify-center rounded-xl bg-blue-600 px-4 py-3 text-sm font-bold text-white hover:bg-blue-700 disabled:opacity-50">{busy ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : <LockKeyhole className="mr-2 h-4 w-4" />}Continue securely</button>
        </form> : <form onSubmit={verifyFile} className="space-y-5">
          <div className="rounded-2xl border border-blue-100 bg-blue-50 p-4 text-sm leading-6 text-blue-900"><ShieldCheck className="mb-2 h-5 w-5 text-blue-600" />Password verified. Upload the signed <strong>.mcred</strong> file. This is required for every new browser session.</div>
          <label className="block cursor-pointer rounded-2xl border-2 border-dashed border-slate-300 px-5 py-8 text-center hover:border-blue-400 hover:bg-blue-50/40"><FileKey2 className="mx-auto h-7 w-7 text-slate-400" /><span className="mt-3 block text-sm font-semibold text-slate-700">{credential?.name || 'Choose credential file'}</span><input type="file" accept=".mcred,application/json" className="sr-only" onChange={e => setCredential(e.target.files?.[0] || null)} required /></label>
          <button disabled={busy || !credential} className="flex w-full items-center justify-center rounded-xl bg-blue-600 px-4 py-3 text-sm font-bold text-white hover:bg-blue-700 disabled:opacity-50">{busy ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : <ShieldCheck className="mr-2 h-4 w-4" />}Verify and open Modernization</button>
          <button type="button" onClick={() => { setStep('credentials'); setChallenge(''); setCredential(null); setError('') }} className="w-full text-xs font-semibold text-slate-500 hover:text-slate-800">Use another account</button>
        </form>}
        {error && <p role="alert" className="mt-5 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</p>}
        <p className="mt-6 text-center text-[11px] leading-5 text-slate-400">Sessions remain open until explicit sign out, browser closure, or credential expiry.</p>
      </div>
    </section>
  </main>
}
