// ---------------------------------------------------------------------------
// Author: Vishnuu A
// Scope: Modernization — frontend/src/api (client.js)
// Date: 2025-08-07
// ---------------------------------------------------------------------------
import axios from 'axios'

const AUTH_TOKEN_KEY = 'modernization_auth_token'
// Development uses Vite's /api proxy; production shares the backend origin.
const API_BASE = import.meta.env.VITE_MODERNIZATION_API_URL || '/api'

// Function: isTokenCurrent
// `bufferSeconds` lets callers ask "is this still good with N seconds of
// margin" — a small buffer (default) for "usable right now", a larger one
// for "due for a proactive refresh soon".
const isTokenCurrent = (token, bufferSeconds = 15) => {
  try {
    const parts = String(token || '').split('.')
    if (parts.length !== 3 || parts[0] !== 'v1') return false
    const encoded = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const payload = JSON.parse(atob(encoded.padEnd(Math.ceil(encoded.length / 4) * 4, '=')))
    return Number(payload.exp || 0) > Math.floor(Date.now() / 1000) + bufferSeconds
  } catch {
    return false
  }
}

// Function: getRawAuthToken
// Returns whatever token is on hand regardless of expiry — the only thing
// checked is that it's shaped like one of ours. Used by the refresh flow,
// which needs to send an *expired* token to the server to renew it.
const getRawAuthToken = () => {
  const token = sessionStorage.getItem(AUTH_TOKEN_KEY)
  return token || null
}

// Function: getAuthToken
// A token usable right now, or null — never mutates storage. Expiry alone is
// not grounds to wipe the session; it might still be renewable (see
// refreshAuthToken), which only clearAuthToken()'s callers decide.
export const getAuthToken = () => {
  return getRawAuthToken()
}

// Function: refreshAuthToken
// Exchanges whatever raw token is stored for a fresh one via /api/auth/refresh.
// The backend accepts a signature-valid token even past its `exp` (within a
// grace window), so a session that expired while a long conversion job was
// still running can renew itself instead of leaving status polling stuck on
// 401s until someone manually logs back in. Concurrent callers share one
// in-flight request instead of each firing their own.
let _refreshPromise = null
export const refreshAuthToken = () => {
  if (_refreshPromise) return _refreshPromise
  const raw = getRawAuthToken()
  if (!raw) return Promise.resolve(null)
  _refreshPromise = axios
    .post(`${API_BASE}/auth/refresh`, {}, { headers: { Authorization: `Bearer ${raw}` } })
    .then(({ data }) => {
      if (data?.token) {
        setAuthToken(data.token)
        return data.token
      }
      clearAuthToken()
      return null
    })
    .catch(() => {
      clearAuthToken()
      return null
    })
    .finally(() => {
      _refreshPromise = null
    })
  return _refreshPromise
}

// Function: getValidAuthToken
// Async token getter that self-heals: returns the current token if usable,
// otherwise tries a refresh before giving up. This is what request paths
// should use instead of the synchronous getAuthToken().
export const getValidAuthToken = async () => {
  const current = getAuthToken()
  if (current) return current
  return refreshAuthToken()
}

// Function: setAuthToken
export const setAuthToken = (token) => {
  if (!token) return
  sessionStorage.setItem(AUTH_TOKEN_KEY, token)
}

// Function: clearAuthToken
export const clearAuthToken = () => {
  sessionStorage.removeItem(AUTH_TOKEN_KEY)
}

// ─── Axios instance ────────────────────────────────────────────────────────
const api = axios.create({ baseURL: API_BASE })

api.interceptors.request.use(async (config) => {
  const token = await getValidAuthToken()
  if (token) config.headers['Authorization'] = `Bearer ${token}`
  return config
})

// Belt-and-suspenders: if a request still comes back 401 (clock skew, or the
// token expired in the gap between getValidAuthToken() and the server
// receiving it), try one refresh-and-retry before surfacing the error. This
// is what used to just retry the same dead token every 10s forever on the
// job status poll, showing a frozen progress bar.
api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401 && getAuthToken()) {
      clearAuthToken()
      window.dispatchEvent(new CustomEvent('modernization-session-expired'))
    }
    return Promise.reject(error)
  },
)

// Proactive refresh: keep an open tab's token from ever actually reaching
// expiry during a long-running job, rather than relying solely on the
// reactive paths above. Checks every minute; only acts when the current
// token is within 5 minutes of expiring.
const PROACTIVE_REFRESH_WINDOW_SECONDS = 5 * 60
setInterval(() => {
  const raw = getRawAuthToken()
  if (raw && isTokenCurrent(raw) && !isTokenCurrent(raw, PROACTIVE_REFRESH_WINDOW_SECONDS)) {
    refreshAuthToken()
  }
}, 60_000)

// ─── Auth ──────────────────────────────────────────────────────────────────
// Function: validateSession
export const validateSession = async () => {
  const { data } = await api.get('/auth/session')
  return data
}

export const beginLogin = async (username, password) =>
  (await axios.post(`${API_BASE}/auth/login`, { username, password })).data

export const completeLogin = async (challenge, credential) => {
  const body = new FormData()
  body.append('challenge', challenge)
  body.append('credential', credential)
  const { data } = await axios.post(`${API_BASE}/auth/credential`, body)
  setAuthToken(data.token)
  return data
}

export const logout = async ({ force = true } = {}) => {
  if (!getAuthToken()) return { logged_out: true, deferred: false }
  const { data } = await api.post('/auth/logout', { force })
  if (data?.logged_out) {
    clearAuthToken()
  }
  return data
}

// ─── Jobs ──────────────────────────────────────────────────────────────────

// Function: startAnalysis
export const startAnalysis = async (folderPath, targetStack = 'aveva_mes', customStackDesc = '', guideFiles = [], outputMode = 'project') => {
  const validGuides = (guideFiles || []).filter(item => item && item.file)
  if (validGuides.length > 0) {
    const fd = new FormData()
    fd.append('folder_path', folderPath)
    fd.append('target_stack', targetStack)
    if (customStackDesc) fd.append('custom_stack_desc', customStackDesc)
    fd.append('output_mode', outputMode)
    validGuides.forEach(({ file }) => fd.append('files', file))
    const { data } = await api.post('/modernize/analyze-with-guides', fd)
    return data
  }
  const { data } = await api.post('/modernize/analyze', {
    folder_path:       folderPath,
    target_stack:      targetStack,
    custom_stack_desc: customStackDesc || undefined,
    output_mode:       outputMode,
  })
  return data
}

// Function: startPromptAnalysis
export const startPromptAnalysis = async (prompt, files = [], targetStack = 'prompt_inferred', customStackDesc = '', outputMode = 'project') => {
  const validFiles = (files || []).filter(({ file }) => file)
  if (validFiles.length === 0) {
    const body = new URLSearchParams()
    body.set('prompt', prompt)
    body.set('target_stack', targetStack)
    if (customStackDesc) body.set('custom_stack_desc', customStackDesc)
    body.set('output_mode', outputMode)
    const { data } = await api.post('/modernize/analyze-prompt', body, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    })
    return data
  }
  const fd = new FormData()
  fd.append('prompt', prompt)
  fd.append('target_stack', targetStack)
  if (customStackDesc) fd.append('custom_stack_desc', customStackDesc)
  fd.append('output_mode', outputMode)
  validFiles.forEach(({ file }) => fd.append('files', file))
  const { data } = await api.post('/modernize/analyze-prompt', fd)
  return data
}

// Function: getLlmStatus
export const getLlmStatus = async () => {
  const { data } = await api.get('/llm/status')
  return data
}

export const updateLlmRuntime = async (model, profile) =>
  (await api.put('/llm/runtime', { model, profile })).data

// Function: detectFolder
export const detectFolder = async (path) => {
  const params = new URLSearchParams({ path })
  const { data } = await api.get(`/fs/detect?${params}`)
  return data
}

// Function: listJobs
export const listJobs = async () => {
  const { data } = await api.get('/modernize/jobs')
  return data
}

export const getJobsReport = async (params = {}) =>
  (await api.get('/modernize/jobs/report', { params })).data

export const getJobsReportExportUrl = (params = {}, format = 'csv') => {
  const query = new URLSearchParams({ ...params, format })
  query.set('token', getAuthToken() || '')
  return `${API_BASE}/modernize/jobs/report/export?${query}`
}

// Function: getJob
export const getJob = async (jobId) => {
  const { data } = await api.get(`/modernize/jobs/${jobId}`)
  return data
}

// Function: getJobStatus
export const getJobStatus = async (jobId) => {
  const { data } = await api.get(`/modernize/jobs/${jobId}/status`)
  return data
}

// Function: deleteJob
export const deleteJob = async (jobId) => {
  const { data } = await api.delete(`/modernize/jobs/${jobId}`)
  return data
}

// Function: getDownloadUrl
export const getDownloadUrl = (jobId) =>
  `${API_BASE}/modernize/jobs/${jobId}/output?token=${encodeURIComponent(getAuthToken() || '')}`

// Function: getStreamUrl
export const getStreamUrl = (jobId) =>
  `${API_BASE}/modernize/jobs/${jobId}/stream?token=${encodeURIComponent(getAuthToken() || '')}`

// ─── Filesystem browser ────────────────────────────────────────────────────
// Function: getFsLs
export const getFsLs = async (path = '') => {
  const token = getAuthToken() || ''
  const params = new URLSearchParams({ token })
  if (path) params.set('path', path)
  const { data } = await api.get(`/fs/ls?${params}`)
  return data
}

// ─── Upload-from-browser folder intake ─────────────────────────────────────
// Mirrors the backend's _UPLOAD_SKIP_DIRS in api/server.py — filtered out
// client-side too so we don't waste bandwidth uploading node_modules/.git/
// build output that would just be dropped server-side anyway.
const UPLOAD_SKIP_DIRS = new Set([
  '.git', '.vs', '.vscode', 'bin', 'obj', 'node_modules',
  '__pycache__', '.venv', 'venv', 'env', 'dist', 'build',
  'target', 'out', 'packages', '.nuget', 'TestResults',
  '.gradle', '.idea', 'coverage', '.next', '.nuxt',
  '.mvn', '.svn', '.hg',
])

// `fileList` is a FileList/array from <input type="file" webkitdirectory>,
// where each File's `.webkitRelativePath` looks like "MyProject/src/Foo.cs".
// Function: filterUploadFiles
export const filterUploadFiles = (fileList) =>
  Array.from(fileList).filter((file) => {
    const parts = (file.webkitRelativePath || file.name).split('/')
    return !parts.slice(0, -1).some((seg) => UPLOAD_SKIP_DIRS.has(seg))
  })

// Function: uploadFolder
export const uploadFolder = async (files, onProgress) => {
  const fd = new FormData()
  files.forEach((file) => {
    fd.append('files', file, file.webkitRelativePath || file.name)
  })
  const { data } = await api.post('/fs/upload-folder', fd, {
    onUploadProgress: onProgress
      ? (evt) => onProgress(evt.total ? evt.loaded / evt.total : 0)
      : undefined,
  })
  return data
}

export const extractGenerationGuidelines = async (files) => {
  const body = new FormData()
  files.forEach((file) => body.append('files', file))
  return (await api.post('/generation-guidelines/extract', body)).data
}

// ── Requirements documentation ────────────────────────────────────────────
export const getRequirementDocument = async (projectId, documentType) =>
  (await api.get(`/projects/${projectId}/requirements/${documentType}`)).data
export const generateRequirementDocument = async (projectId, documentType) =>
  (await api.post(`/projects/${projectId}/requirements/${documentType}/generate`, {})).data
export const getRequirementGenerationJob = async jobId =>
  (await api.get(`/requirements/jobs/${jobId}`)).data
export const downloadRequirementDocument = async (projectId, documentType) =>
  (await api.get(`/projects/${projectId}/requirements/${documentType}/export`, { responseType: 'blob' })).data
export const getGeneratedAssets = async projectId =>
  (await api.get(`/projects/${projectId}/generated-assets`)).data
export const downloadGeneratedAssets = async projectId =>
  (await api.get(`/projects/${projectId}/generated-assets/export`, { responseType: 'blob' })).data

// ── Governed projects ──────────────────────────────────────────────────────
// Function: listProjects
export const listProjects = async () => (await api.get('/projects')).data
// Function: createProject
export const createProject = async (payload) => (await api.post('/projects', payload)).data
// Function: getProject
export const getProject = async (id) => (await api.get(`/projects/${id}`)).data
// Function: deleteProject
export const deleteProject = async (id) => (await api.post(`/projects/${id}/delete`, {})).data
// Function: getProjectJobs
export const getProjectJobs = async (id, sourcePath = '') => {
  const result = await listJobs()
  return { jobs: (result.jobs || []).filter(job => job.project_id === id || (sourcePath && job.folder_path === sourcePath)) }
}
// Function: analyzeProject
export const analyzeProject = async (id, target_stack, custom_stack_desc = '') =>
  (await api.post(`/projects/${id}/analyze`, { target_stack, custom_stack_desc })).data
// Function: generateProjectPlan
export const generateProjectPlan = async (id, target_stack, custom_stack_desc = '') =>
  (await api.post(`/projects/${id}/plans`, { target_stack, custom_stack_desc })).data
// Function: getTargetStacks
export const getTargetStacks = async () => (await api.get('/modernize/target-stacks')).data
// Function: getToolchainStatus
export const getToolchainStatus = async () => (await api.get('/modernize/toolchains')).data
// Function: installToolchain
export const installToolchain = async (tool_id) => (await api.post('/modernize/toolchains/install', { tool_id })).data
// Function: getToolchainInstallStatus
export const getToolchainInstallStatus = async (jobId) => (await api.get(`/modernize/toolchains/install/${jobId}`)).data
// Function: decideProjectSnapshot
export const decideProjectSnapshot = async (id, snapshotId, decision) =>
  (await api.post(`/projects/${id}/snapshots/${snapshotId}/decision`, { decision })).data
// Function: transformProject
export const transformProject = async (id) => (await api.post(`/projects/${id}/transform`, {})).data
// Function: compareSnapshots
export const compareSnapshots = async (id, left, right) =>
  (await api.get(`/projects/${id}/compare`, { params: { left_snapshot_id: left, right_snapshot_id: right } })).data
// Function: getSnapshotArtifact
export const getSnapshotArtifact = async (id, snapshotId) =>
  (await api.get(`/projects/${id}/snapshots/${snapshotId}/artifact`)).data
// Function: reviseProjectPlan
export const reviseProjectPlan = async (id, snapshotId, payload) =>
  (await api.patch(`/projects/${id}/plans/${snapshotId}`, payload)).data
// Function: restoreProjectSnapshot
export const restoreProjectSnapshot = async (id, snapshotId) =>
  (await api.post(`/projects/${id}/snapshots/${snapshotId}/restore`, {})).data
// Function: validateProjectContracts
export const validateProjectContracts = async (id) => (await api.get(`/projects/${id}/contracts/validate`)).data
// Function: getProjectQualityGate
export const getProjectQualityGate = async (id, outputSnapshotId) =>
  (await api.get(`/projects/${id}/quality-gate`, { params: { output_snapshot_id: outputSnapshotId } })).data
// Function: approveProjectRelease
export const approveProjectRelease = async (id, outputSnapshotId, comment = '') =>
  (await api.post(`/projects/${id}/releases`, { output_snapshot_id: outputSnapshotId, comment })).data
// Function: submitProjectReview
export const submitProjectReview = async (id, outputSnapshotId, decision, feedback, fileFeedback = []) =>
  (await api.post(`/projects/${id}/reviews`, { output_snapshot_id: outputSnapshotId, decision, feedback, file_feedback: fileFeedback })).data
// Function: purgeProjectSnapshots
export const purgeProjectSnapshots = async (id) => (await api.post(`/projects/${id}/retention/purge`)).data
// Function: getComparisonExportUrl
export const getComparisonExportUrl = (id, left, right, format) =>
  `${API_BASE}/projects/${id}/compare/export?left_snapshot_id=${encodeURIComponent(left)}&right_snapshot_id=${encodeURIComponent(right)}&format=${format}&token=${encodeURIComponent(getAuthToken() || '')}`
// Function: getReleaseExportUrl
export const getReleaseExportUrl = (id, snapshotId) =>
  `${API_BASE}/projects/${id}/releases/${snapshotId}/export?token=${encodeURIComponent(getAuthToken() || '')}`

