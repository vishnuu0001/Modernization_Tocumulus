import { Activity, CheckCircle2, Clock3, Download, FileArchive, RefreshCw, Trash2, XCircle } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import toast from 'react-hot-toast'
import { Link } from 'react-router-dom'
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { deleteJob, getDownloadUrl, getJobsReport, getJobsReportExportUrl } from '../api/client.js'
import Layout from '../components/Layout.jsx'

const COLORS = ['#2563eb', '#10b981', '#ef4444', '#f59e0b', '#64748b', '#8b5cf6']
const TERMINAL = new Set(['completed', 'validation_failed', 'failed', 'cancelled'])
const badge = {
  completed: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  failed: 'bg-red-50 text-red-700 border-red-200',
  validation_failed: 'bg-amber-50 text-amber-700 border-amber-200',
  cancelled: 'bg-slate-100 text-slate-600 border-slate-200',
  running: 'bg-blue-50 text-blue-700 border-blue-200',
  queued: 'bg-indigo-50 text-indigo-700 border-indigo-200',
  pending: 'bg-indigo-50 text-indigo-700 border-indigo-200',
}

const formatDuration = (seconds) => {
  if (!seconds) return '0s'
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  return `${(seconds / 3600).toFixed(1)}h`
}

function Metric({ label, value, note, icon: Icon, tone = 'blue' }) {
  const iconTone = { blue: 'text-blue-600', emerald: 'text-emerald-600', red: 'text-red-600', amber: 'text-amber-600' }[tone]
  return (
    <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
      <div className="flex items-center justify-between"><span className="text-sm text-slate-500">{label}</span><Icon className={`h-5 w-5 ${iconTone}`} /></div>
      <p className="mt-2 text-2xl font-semibold text-slate-900">{value}</p><p className="mt-1 text-xs text-slate-500">{note}</p>
    </div>
  )
}

export default function JobsPage() {
  const [report, setReport] = useState(null)
  const [loading, setLoading] = useState(true)
  const [filters, setFilters] = useState({ date_from: '', date_to: '', tech_stack: '', status: '' })
  const params = useMemo(() => Object.fromEntries(Object.entries(filters).filter(([, value]) => value)), [filters])

  const load = async (quiet = false) => {
    if (!quiet) setLoading(true)
    try { setReport(await getJobsReport(params)) } catch { toast.error('Failed to load transformation report') } finally { setLoading(false) }
  }
  useEffect(() => {
    load()
    const timer = setInterval(() => load(true), 15000)
    return () => clearInterval(timer)
  }, [JSON.stringify(params)])

  const remove = async (job) => {
    if (!window.confirm(`Remove job ${job.job_id}?`)) return
    try { await deleteJob(job.job_id); await load(true); toast.success('Job removed') } catch { toast.error('Failed to remove job') }
  }

  const summary = report?.summary || {}
  const jobs = report?.jobs || []
  const stackOptions = report?.filter_options?.tech_stacks || []
  const statusOptions = report?.filter_options?.statuses || []
  return (
    <Layout>
      <main className="mx-auto max-w-[1480px] px-5 py-6 lg:px-8">
        <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
          <div><p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-blue-600">Operations analytics</p><h1 className="mt-1 text-2xl font-semibold text-slate-950">Transformation Jobs</h1><p className="mt-1 text-sm text-slate-500">Modernization throughput, quality, runtime, and downloadable outputs.</p></div>
          <div className="flex gap-2">
            <a href={getJobsReportExportUrl(params, 'csv')} className="inline-flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 shadow-sm hover:bg-slate-50"><Download className="h-4 w-4" />CSV</a>
            <a href={getJobsReportExportUrl(params, 'json')} className="inline-flex items-center gap-2 rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-700"><Download className="h-4 w-4" />JSON</a>
          </div>
        </div>

        <div className="mb-6 grid gap-3 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:grid-cols-2 lg:grid-cols-5">
          <label className="text-xs font-semibold text-slate-600">From<input type="date" value={filters.date_from} onChange={(e) => setFilters({ ...filters, date_from: e.target.value })} className="mt-1 block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm font-normal" /></label>
          <label className="text-xs font-semibold text-slate-600">To<input type="date" value={filters.date_to} onChange={(e) => setFilters({ ...filters, date_to: e.target.value })} className="mt-1 block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm font-normal" /></label>
          <label className="text-xs font-semibold text-slate-600">Tech stack<select value={filters.tech_stack} onChange={(e) => setFilters({ ...filters, tech_stack: e.target.value })} className="mt-1 block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm font-normal"><option value="">All stacks</option>{stackOptions.map((value) => <option key={value}>{value}</option>)}</select></label>
          <label className="text-xs font-semibold text-slate-600">Status<select value={filters.status} onChange={(e) => setFilters({ ...filters, status: e.target.value })} className="mt-1 block w-full rounded-lg border border-slate-200 px-3 py-2 text-sm font-normal"><option value="">All statuses</option>{statusOptions.map((value) => <option key={value}>{value.replaceAll('_', ' ')}</option>)}</select></label>
          <button onClick={() => load()} className="mt-5 inline-flex h-10 items-center justify-center gap-2 rounded-lg border border-slate-200 text-sm font-semibold text-slate-700 hover:bg-slate-50"><RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />Refresh</button>
        </div>

        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <Metric label="Transformations" value={summary.total || 0} note={`${summary.running || 0} active or queued`} icon={Activity} />
          <Metric label="Successful" value={summary.completed || 0} note={`${summary.success_rate || 0}% terminal success rate`} icon={CheckCircle2} tone="emerald" />
          <Metric label="Quality failures" value={(summary.failed || 0) + (summary.validation_failed || 0)} note={`${summary.validation_failed || 0} held by validation`} icon={XCircle} tone="red" />
          <Metric label="Average runtime" value={formatDuration(summary.avg_duration_seconds)} note={`${summary.output_count || 0} downloadable outputs`} icon={Clock3} tone="amber" />
        </div>

        <div className="mt-6 grid gap-6 xl:grid-cols-3">
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm xl:col-span-2"><h2 className="font-semibold text-slate-900">Transformation volume</h2><p className="mb-4 text-xs text-slate-500">Created jobs and terminal outcomes by day</p><div className="h-72"><ResponsiveContainer width="100%" height="100%"><AreaChart data={report?.daily || []}><defs><linearGradient id="volume" x1="0" y1="0" x2="0" y2="1"><stop offset="5%" stopColor="#2563eb" stopOpacity={0.25}/><stop offset="95%" stopColor="#2563eb" stopOpacity={0}/></linearGradient></defs><CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" /><XAxis dataKey="date" tick={{ fontSize: 11 }} /><YAxis allowDecimals={false} tick={{ fontSize: 11 }} /><Tooltip /><Area type="monotone" dataKey="total" stroke="#2563eb" fill="url(#volume)" strokeWidth={2} /><Area type="monotone" dataKey="completed" stroke="#10b981" fill="transparent" strokeWidth={2} /></AreaChart></ResponsiveContainer></div></section>
          <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-semibold text-slate-900">Outcome distribution</h2><p className="mb-4 text-xs text-slate-500">Current result mix</p><div className="h-72"><ResponsiveContainer width="100%" height="100%"><PieChart><Pie data={report?.statuses || []} dataKey="count" nameKey="name" innerRadius={58} outerRadius={92} paddingAngle={3}>{(report?.statuses || []).map((entry, index) => <Cell key={entry.name} fill={COLORS[index % COLORS.length]} />)}</Pie><Tooltip /></PieChart></ResponsiveContainer></div></section>
        </div>

        <section className="mt-6 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm"><h2 className="font-semibold text-slate-900">Modernizations by tech stack</h2><div className="mt-4 h-64"><ResponsiveContainer width="100%" height="100%"><BarChart data={report?.tech_stacks || []} layout="vertical" margin={{ left: 24 }}><CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" /><XAxis type="number" allowDecimals={false} /><YAxis dataKey="name" type="category" width={150} tick={{ fontSize: 11 }} /><Tooltip /><Bar dataKey="count" fill="#2563eb" radius={[0, 5, 5, 0]} /></BarChart></ResponsiveContainer></div></section>

        <section className="mt-6 overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
          <div className="border-b border-slate-200 px-5 py-4"><h2 className="font-semibold text-slate-900">Job reports and outputs</h2><p className="text-xs text-slate-500">Every output link downloads the persisted artifact for that job.</p></div>
          <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-5 py-3">Job</th><th className="px-4 py-3">Stack</th><th className="px-4 py-3">Status</th><th className="px-4 py-3">Runtime</th><th className="px-4 py-3">Model / profile</th><th className="px-4 py-3 text-right">Actions</th></tr></thead><tbody className="divide-y divide-slate-100">
            {jobs.map((job) => <tr key={job.job_id} className="hover:bg-slate-50/70"><td className="max-w-sm px-5 py-4"><Link to={`/jobs/${job.job_id}`} className="font-semibold text-blue-700 hover:underline">{job.job_id}</Link><p className="mt-1 truncate text-xs text-slate-500">{job.folder_path || job.prompt || 'Prompt-based generation'}</p><p className="mt-1 text-[11px] text-slate-400">{job.created_at ? new Date(job.created_at).toLocaleString() : 'Unknown date'} · {job.generated_file_count} files</p></td><td className="px-4 py-4 text-slate-600">{job.target_stack || 'Unspecified'}</td><td className="px-4 py-4"><span className={`rounded-full border px-2.5 py-1 text-xs font-semibold capitalize ${badge[job.status] || badge.running}`}>{job.status.replaceAll('_', ' ')}</span></td><td className="px-4 py-4 text-slate-600">{formatDuration(job.duration_seconds)}</td><td className="px-4 py-4"><p className="max-w-[180px] truncate text-xs font-medium text-slate-700">{job.llm_model || 'Legacy job'}</p><p className="text-xs capitalize text-slate-400">{job.optimization_profile || 'not captured'}</p></td><td className="px-4 py-4"><div className="flex justify-end gap-2">{job.output_available && <a href={getDownloadUrl(job.job_id)} title="Download output" className="rounded-lg border border-slate-200 p-2 text-blue-700 hover:bg-blue-50"><FileArchive className="h-4 w-4" /></a>}<Link to={`/jobs/${job.job_id}`} className="rounded-lg border border-slate-200 px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50">Report</Link><button disabled={!TERMINAL.has(job.status)} onClick={() => remove(job)} className="rounded-lg border border-slate-200 p-2 text-slate-400 hover:bg-red-50 hover:text-red-600 disabled:opacity-30"><Trash2 className="h-4 w-4" /></button></div></td></tr>)}
            {!loading && jobs.length === 0 && <tr><td colSpan="6" className="px-6 py-16 text-center text-slate-500">No jobs match the selected filters.</td></tr>}
          </tbody></table></div>
        </section>
      </main>
    </Layout>
  )
}
