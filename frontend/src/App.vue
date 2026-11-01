<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import {
  ArrowLeft, ArrowUpRight, Box, Check, ChevronLeft, ChevronRight, CircleDot, Copy, Cpu, Database, Eraser, Info, LayoutDashboard, Network,
  LogOut, Pencil, Play, Plus, RefreshCw, RotateCw, ScrollText, Server, SquareTerminal, Table2, Trash2, X, Bell, ShieldAlert, MemoryStick,
} from '@lucide/vue'

const apiBase = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000/api'
const token = ref(localStorage.getItem('minicloud_token') || '')
const username = ref(localStorage.getItem('minicloud_user') || '')
const loginForm = ref({ username: '', password: '' })
const servers = ref([])
const templates = ref([])
const notifications = ref([])
const toasts = ref([])
const activeTab = ref('overview')

watch(activeTab, (newTab) => {
  if (newTab === 'notifications') {
    loadNotifications()
  }
})
const selectedServer = ref(null)
const showDetail = ref(false)
const metrics = ref([])
const liveMetric = ref(null)
const loading = ref(false)
const lifecycleAction = ref('')
const error = ref('')
const loginError = ref('')
const showCreate = ref(false)
const terminalHost = ref(null)
const terminalStatus = ref('Offline')
const terminalPath = ref('Resolving path…')
const logs = ref('')
const logsStatus = ref('Connecting...')
const logsHost = ref(null)
let logsPinnedToBottom = true
const createForm = ref({ name: '', image: 'nginx', framework: '', database_size_mb: null, template: '', custom_image: '', env_vars: {}, postgres_database: 'minicloud', postgres_username: 'minicloud', postgres_password: '', ssh_enabled: false, ssh_public_key: '', ssh_allowed_from: '' })
let terminal = null
let terminalSocket = null
let logsSocket = null
let notificationSocket = null
let provisioningPollTimer = null
let terminalResizeObserver = null
let terminalOutputBuffer = ''
let terminalTextDecoder = null
let terminalRetryCount = 0
const terminalPathMarker = '__MINICLOUD_CWD__'

const databaseState = () => ({
  loading: false,
  error: '',
  database: '',
  version: '',
  databases: [],
  tables: [],
  selected: null,
  table: null,
  limit: 50,
  offset: 0,
  tab: 'data',
  sql: 'SELECT * FROM ',
  result: null,
  running: false,
})
const db = ref(databaseState())
const rowEditor = ref({ open: false, mode: 'insert', fields: {}, key: null, error: '', saving: false })
const tableCreator = ref({ open: false, schema: 'public', name: '', columns: [], error: '', saving: false })
const showIncidentModal = ref(false)
const incidentForm = ref({ type: 'down' })
const incidentSubmitting = ref(false)
const incidentError = ref('')
const incidentDialog = ref(null)
const detailPanel = ref('')
const copiedField = ref('')
const dbColumnTypes = [
  'serial', 'bigserial', 'integer', 'bigint', 'smallint', 'numeric(10,2)', 'real', 'double precision',
  'text', 'varchar(255)', 'char(10)', 'boolean', 'date', 'timestamptz', 'timestamp', 'time',
  'uuid', 'json', 'jsonb', 'bytea',
]

const editableColumns = computed(() => (db.value.table?.columns || []).filter(column => !column.identity && !column.generated))

const incidentOptions = [
  {
    value: 'down',
    label: 'Server down',
    icon: ShieldAlert,
    tone: 'error',
    detail: 'Opens a DOWN incident even while the container keeps running. The next health check finds it healthy again and resolves it automatically.',
  },
  {
    value: 'high_cpu',
    label: 'High CPU',
    icon: Cpu,
    tone: 'warning',
    detail: 'Opens a HIGH CPU incident. It stays open until metrics report usage back under the configured threshold.',
  },
  {
    value: 'high_memory',
    label: 'High memory',
    icon: MemoryStick,
    tone: 'warning',
    detail: 'Opens a HIGH MEMORY incident measured against this server’s resource template.',
  },
]

const openIncidentTypes = computed(() => new Set(
  (selectedServer.value?.incidents || []).filter(incident => incident.status === 'open').map(incident => incident.type),
))
const selectedIncidentOption = computed(() => incidentOptions.find(option => option.value === incidentForm.value.type) || incidentOptions[0])

function resetDatabase() {
  db.value = databaseState()
  rowEditor.value.open = false
  tableCreator.value.open = false
}

function formatBytes(bytes) {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = Number(bytes)
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1 }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

function formatCell(value) {
  if (value === null || value === undefined) return 'NULL'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function isBooleanColumn(column) {
  return column.type.includes('bool')
}

function isJsonColumn(column) {
  return column.type.includes('json')
}

function databaseUrl(path, params = {}) {
  const server = selectedServer.value
  if (!server) throw new Error('No server selected.')
  const search = new URLSearchParams()
  if (db.value.database) search.set('database', db.value.database)
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const query = search.toString()
  return `/servers/${server.id}/database${path}${query ? `?${query}` : ''}`
}

async function loadDatabase(databaseName) {
  const server = selectedServer.value
  if (!server || server.image !== 'postgres' || server.status !== 'running') return
  db.value.loading = true
  db.value.error = ''
  try {
    const search = new URLSearchParams()
    const target = databaseName || db.value.database
    if (target) search.set('database', target)
    const data = await api(databaseUrl('/', Object.fromEntries(search)))
    db.value.database = data.database
    db.value.version = data.server_version
    db.value.databases = data.databases
    db.value.tables = data.tables
    const current = db.value.selected
    if (current && data.tables.some(table => table.schema === current.schema && table.name === current.name)) {
      await loadDatabaseTable(current.schema, current.name, db.value.offset)
    } else {
      db.value.selected = null
      db.value.table = null
    }
  } catch (err) {
    db.value.error = err.message
  } finally {
    db.value.loading = false
  }
}

async function loadDatabaseTable(schema, name, offset = 0) {
  if (!selectedServer.value) return
  db.value.loading = true
  db.value.error = ''
  db.value.selected = { schema, name }
  db.value.offset = Math.max(0, offset)
  try {
    db.value.table = await api(databaseUrl(`/tables/${encodeURIComponent(schema)}/${encodeURIComponent(name)}/`, {
      limit: db.value.limit,
      offset: db.value.offset,
    }))
  } catch (err) {
    db.value.error = err.message
    db.value.table = null
  } finally {
    db.value.loading = false
  }
}

function selectDatabaseTable(table) {
  if (db.value.tab === 'sql') db.value.tab = 'data'
  db.value.error = ''
  loadDatabaseTable(table.schema, table.name, 0)
}

function setDatabaseTab(tab) {
  db.value.tab = tab
  db.value.error = ''
}

function databasePage(direction) {
  if (!db.value.selected) return
  const next = db.value.offset + direction * db.value.limit
  if (next < 0) return
  if (next >= (db.value.table?.total_rows || 0) && direction > 0) return
  loadDatabaseTable(db.value.selected.schema, db.value.selected.name, next)
}

async function runDatabaseQuery() {
  if (!selectedServer.value || db.value.running) return
  db.value.running = true
  db.value.error = ''
  try {
    db.value.result = await api(databaseUrl('/query/'), {
      method: 'POST',
      body: JSON.stringify({ sql: db.value.sql, database: db.value.database || null }),
    })
    await loadDatabase()
  } catch (err) {
    db.value.error = err.message
    db.value.result = null
  } finally {
    db.value.running = false
  }
}

function rowKey(row) {
  const key = {}
  for (const name of db.value.table?.row_key || []) key[name] = row[name]
  return key
}

function openRowEditor(row = null) {
  const fields = {}
  for (const column of editableColumns.value) {
    const value = row ? row[column.name] : ''
    fields[column.name] = {
      value: value === null || value === undefined ? '' : (typeof value === 'object' ? JSON.stringify(value) : String(value)),
      nil: row ? value === null || value === undefined : false,
    }
  }
  rowEditor.value = { open: true, mode: row ? 'edit' : 'insert', fields, key: row ? rowKey(row) : null, error: '', saving: false }
}

async function saveRow() {
  const table = db.value.table
  if (!table) return
  const values = {}
  for (const column of editableColumns.value) {
    const field = rowEditor.value.fields[column.name]
    if (!field) continue
    if (field.nil) {
      values[column.name] = null
    } else if (rowEditor.value.mode === 'insert' && field.value === '') {
      continue
    } else if (isBooleanColumn(column)) {
      values[column.name] = field.value === 'true'
    } else {
      values[column.name] = field.value
    }
  }
  rowEditor.value.saving = true
  rowEditor.value.error = ''
  try {
    const path = rowEditor.value.mode === 'insert'
      ? `/tables/${encodeURIComponent(table.schema)}/${encodeURIComponent(table.table)}/rows/`
      : `/tables/${encodeURIComponent(table.schema)}/${encodeURIComponent(table.table)}/rows/update/`
    const body = rowEditor.value.mode === 'insert'
      ? { values, database: db.value.database || null }
      : { key: rowEditor.value.key, values, database: db.value.database || null }
    await api(databaseUrl(path), { method: 'POST', body: JSON.stringify(body) })
    rowEditor.value.open = false
    await loadDatabaseTable(table.schema, table.table, db.value.offset)
  } catch (err) {
    rowEditor.value.error = err.message
  } finally {
    rowEditor.value.saving = false
  }
}

async function deleteDatabaseRow(row) {
  const table = db.value.table
  if (!table) return
  if (!window.confirm('Delete this row? This cannot be undone.')) return
  try {
    await api(databaseUrl(`/tables/${encodeURIComponent(table.schema)}/${encodeURIComponent(table.table)}/rows/delete/`), {
      method: 'POST',
      body: JSON.stringify({ key: rowKey(row), database: db.value.database || null }),
    })
    const nextOffset = (db.value.table?.rows.length || 0) === 1 && db.value.offset > 0 ? db.value.offset - db.value.limit : db.value.offset
    await loadDatabaseTable(table.schema, table.table, nextOffset)
    await loadDatabase()
  } catch (err) {
    db.value.error = err.message
  }
}

function openTableCreator() {
  tableCreator.value = {
    open: true,
    schema: 'public',
    name: '',
    columns: [{ name: 'id', type: 'serial', nullable: false, primary_key: true, default: '' }],
    error: '',
    saving: false,
  }
}

function addCreatorColumn() {
  tableCreator.value.columns.push({ name: '', type: 'text', nullable: true, primary_key: false, default: '' })
}

function removeCreatorColumn(index) {
  tableCreator.value.columns.splice(index, 1)
}

async function createDatabaseTable() {
  const creator = tableCreator.value
  creator.saving = true
  creator.error = ''
  try {
    await api(databaseUrl('/tables/'), {
      method: 'POST',
      body: JSON.stringify({
        schema: creator.schema || 'public',
        name: creator.name,
        columns: creator.columns,
        database: db.value.database || null,
      }),
    })
    creator.open = false
    await loadDatabase()
    await loadDatabaseTable(creator.schema || 'public', creator.name, 0)
    db.value.tab = 'data'
  } catch (err) {
    creator.error = err.message
  } finally {
    creator.saving = false
  }
}

async function dropDatabaseTable() {
  const table = db.value.table
  if (!table) return
  if (!window.confirm(`Drop table ${table.schema}.${table.table}? All of its data will be deleted.`)) return
  try {
    await api(databaseUrl(`/tables/${encodeURIComponent(table.schema)}/${encodeURIComponent(table.table)}/`), { method: 'DELETE' })
    db.value.selected = null
    db.value.table = null
    await loadDatabase()
  } catch (err) {
    db.value.error = err.message
  }
}

async function truncateDatabaseTable() {
  const table = db.value.table
  if (!table) return
  if (!window.confirm(`Remove every row from ${table.schema}.${table.table}?`)) return
  try {
    await api(databaseUrl(`/tables/${encodeURIComponent(table.schema)}/${encodeURIComponent(table.table)}/truncate/`), {
      method: 'POST',
      body: JSON.stringify({ database: db.value.database || null }),
    })
    await loadDatabaseTable(table.schema, table.table, 0)
    await loadDatabase()
  } catch (err) {
    db.value.error = err.message
  }
}


function formatDate(dateString) {
  if (!dateString) return '—'
  const date = new Date(dateString)
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
}

function formatDateTime(dateString) {
  if (!dateString) return '—'
  const date = new Date(dateString)
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}

const notificationFilter = ref('all')
const unreadNotifications = computed(() => notifications.value.filter(note => !note.is_read))
const visibleNotifications = computed(() => (
  notificationFilter.value === 'unread' ? unreadNotifications.value : notifications.value
))

function relativeTime(dateString) {
  const seconds = Math.round((Date.now() - new Date(dateString).getTime()) / 1000)
  if (seconds < 45) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  const days = Math.round(hours / 24)
  return days === 1 ? 'yesterday' : `${days} days ago`
}

function notificationDayLabel(dateString) {
  const date = new Date(dateString)
  const today = new Date()
  if (date.toDateString() === today.toDateString()) return 'Today'
  if (date.toDateString() === new Date(today.getTime() - 86400000).toDateString()) return 'Yesterday'
  return formatDate(dateString)
}

const groupedNotifications = computed(() => {
  const groups = []
  for (const note of visibleNotifications.value) {
    const label = notificationDayLabel(note.created_at)
    const current = groups[groups.length - 1]
    if (current && current.label === label) current.items.push(note)
    else groups.push({ label, items: [note] })
  }
  return groups
})

async function markNotificationRead(note) {
  if (note.is_read) return
  note.is_read = true
  try {
    await api(`/notifications/${note.id}/mark_read/`, { method: 'POST' })
  } catch {
    note.is_read = false
  }
}

function openNotificationServer(note) {
  if (!note.server) return
  activeTab.value = 'overview'
  selectServer(note.server)
}
const terminalTitle = computed(() => {
  if (!selectedServer.value) return ''
  if (selectedServer.value.image === 'postgres') {
    return `${selectedServer.value.name} · PostgreSQL / ${selectedServer.value.env_vars?.POSTGRES_DB || 'postgres'}`
  }
  return `${selectedServer.value.name} · ${terminalPath.value}`
})

const api = async (path, options = {}) => {
  const response = await fetch(`${apiBase}${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(token.value ? { Authorization: `Token ${token.value}` } : {}), ...(options.headers || {}) },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    // field errors come back keyed by field, so flatten them into the one message line each form shows
    const messages = [...new Set(Object.values(body).flat().filter(value => typeof value === 'string'))]
    throw new Error(messages.join(' ') || `Request failed (${response.status})`)
  }
  return response.status === 204 ? null : response.json()
}

const runningCount = computed(() => servers.value.filter(server => server.status === 'running').length)
const totalMemory = computed(() => servers.value.reduce((total, server) => total + (server.effective_memory_mb ?? server.template_memory_mb ?? 0), 0))

const latestMetric = computed(() => liveMetric.value || metrics.value[metrics.value.length - 1] || null)
const cpuUsage = computed(() => Number(latestMetric.value?.cpu_percent ?? 0).toFixed(2))
const memoryUsage = computed(() => {
  const value = latestMetric.value?.memory_mb
  return value === undefined || value === null ? '—' : Number(value).toFixed(1)
})

async function copyText(value, field) {
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    copiedField.value = field
    window.setTimeout(() => { if (copiedField.value === field) copiedField.value = '' }, 1600)
  } catch {
    window.prompt('Copy this value', value)
  }
}

const sshProbe = ref(null)
const sshProbing = ref(false)
const sshAllowlistDraft = ref('')
const sshAllowlistSaving = ref(false)
const sshAllowlistError = ref('')

async function saveSshAllowlist() {
  const server = selectedServer.value
  if (!server || sshAllowlistSaving.value) return
  sshAllowlistSaving.value = true
  sshAllowlistError.value = ''
  try {
    const updated = await api(`/servers/${server.id}/`, { method: 'PATCH', body: JSON.stringify({ ssh_allowed_from: sshAllowlistDraft.value }) })
    const index = servers.value.findIndex(item => item.id === updated.id)
    if (index !== -1) servers.value[index] = updated
    selectedServer.value = updated
    // the serializer normalises every entry, so show what sshd actually enforces
    sshAllowlistDraft.value = updated.ssh_allowed_from || ''
  } catch (err) {
    sshAllowlistError.value = err.message
  } finally {
    sshAllowlistSaving.value = false
  }
}

async function checkSshReachability() {
  const server = selectedServer.value
  if (!server || sshProbing.value) return
  sshProbing.value = true
  sshProbe.value = null
  try {
    sshProbe.value = await api(`/servers/${server.id}/ssh/check/`, { method: 'POST' })
  } catch (err) {
    sshProbe.value = { state: 'error', message: err.message }
  } finally {
    sshProbing.value = false
  }
}

async function openDetailPanel(panel) {
  if (detailPanel.value === panel) {
    closeDetailPanel()
    return
  }
  closeDetailPanel()
  const server = selectedServer.value
  if (!server) return
  if (panel === 'shell' && server.status !== 'running') return
  if (panel === 'database' && !(server.image === 'postgres' && server.status === 'running')) return
  if (panel === 'logs' && !['creating', 'running'].includes(server.status)) return
  detailPanel.value = panel
  await nextTick()
  if (panel === 'shell') openTerminal()
  if (panel === 'logs') openLogs()
}

function closeDetailPanel() {
  if (detailPanel.value === 'shell') closeTerminal()
  if (detailPanel.value === 'logs') closeLogs()
  detailPanel.value = ''
}

function closeDetailPanels() {
  closeDetailPanel()
  copiedField.value = ''
}

const provisioningSteps = [
  { key: 'allocating_port', label: 'Allocating port' },
  { key: 'pulling_image', label: 'Pulling image' },
  { key: 'creating_container', label: 'Creating container' },
  { key: 'downloading_xampp', label: 'Downloading XAMPP', images: ['php'] },
  { key: 'installing_xampp', label: 'Installing XAMPP', images: ['php'] },
  { key: 'starting_apache', label: 'Starting Apache', images: ['php'] },
  { key: 'installing_dependencies', label: 'Installing deps', images: ['python', 'node'] },
  { key: 'configuring', label: 'Configuring', images: ['nginx', 'python', 'node'] },
  { key: 'ready', label: 'Ready' },
]

const visibleProvisioningSteps = computed(() => {
  const image = selectedServer.value?.image
  return provisioningSteps.filter(step => !step.images || step.images.includes(image))
})

function stepIndex(key) {
  return provisioningSteps.findIndex(s => s.key === key)
}

function stepState(key) {
  if (!selectedServer.value?.provisioning_step) return 'pending'
  const currentIndex = stepIndex(selectedServer.value.provisioning_step)
  const targetIndex = stepIndex(key)
  if (targetIndex < currentIndex) return 'completed'
  if (targetIndex === currentIndex) return selectedServer.value.provisioning_step === 'ready' ? 'completed' : 'current'
  return 'pending'
}

function stopProvisioningPoll() {
  if (provisioningPollTimer) window.clearInterval(provisioningPollTimer)
  provisioningPollTimer = null
}

function startProvisioningPoll() {
  if (provisioningPollTimer) return
  provisioningPollTimer = window.setInterval(async () => {
    if (!token.value) { stopProvisioningPoll(); return }
    try {
      const list = await api('/servers/')
      servers.value = list
      const current = selectedServer.value
      if (current) {
        const updated = list.find(item => item.id === current.id)
        if (updated) {
          const wasCreating = current.status === 'creating'
          selectedServer.value = updated
          if (wasCreating && updated.status === 'running') {
            await refreshLiveMetric()
            if (updated.image === 'postgres') loadDatabase()
          }
        }
      }
      if (!list.some(item => item.status === 'creating')) stopProvisioningPoll()
    } catch { /* keep polling through transient errors */ }
  }, 2500)
}

async function login() {
  loginError.value = ''
  try {
    const response = await fetch(`${apiBase}/token/`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(loginForm.value) })
    if (!response.ok) throw new Error('Invalid username or password.')
    const data = await response.json()
    token.value = data.token
    username.value = loginForm.value.username
    localStorage.setItem('minicloud_token', token.value)
    localStorage.setItem('minicloud_user', username.value)
    await loadDashboard()
  } catch (err) { loginError.value = err.message }
}

function logout() {
  closeTerminal()
  stopProvisioningPoll()
  resetDatabase()
  closeDetailPanels()
  token.value = ''
  username.value = ''
  servers.value = []
  selectedServer.value = null
  showDetail.value = false
  localStorage.removeItem('minicloud_token')
  localStorage.removeItem('minicloud_user')
}

async function loadDashboard() {
  if (!token.value) return
  loading.value = true
  error.value = ''
  try {
    if (!templates.value.length) templates.value = await api('/templates/')
    servers.value = await api('/servers/')
    await loadNotifications()
    if (!createForm.value.template) createForm.value.template = templates.value[0]?.id || ''
    const current = selectedServer.value
    if (current) {
      const updated = servers.value.find(server => server.id === current.id)
      if (updated) selectedServer.value = updated
    }
    if (servers.value.some(server => server.status === 'creating')) startProvisioningPoll()
  } catch (err) {
    error.value = err.message
    if (err.message.includes('401')) logout()
  } finally { loading.value = false }
}

async function loadNotifications() {
  if (!token.value) return
  try {
    notifications.value = await api('/notifications/')
  } catch (err) {
    console.error('Failed to load notifications:', err)
  }
}

async function selectServer(id) {
  closeDetailPanels()
  resetDatabase()
  selectedServer.value = servers.value.find(server => server.id === id) || null
  sshProbe.value = null
  sshAllowlistDraft.value = selectedServer.value?.ssh_allowed_from || ''
  if (!selectedServer.value) return
  showDetail.value = true
  metrics.value = await api(`/servers/${id}/metrics/`)
  await refreshLiveMetric()
  if (selectedServer.value?.id !== id) return
  if (selectedServer.value.image === 'postgres' && selectedServer.value.status === 'running') {
    detailPanel.value = 'database'
    loadDatabase()
  }
  if (selectedServer.value.status === 'creating') startProvisioningPoll()
}

async function refreshLiveMetric() {
  if (!selectedServer.value || selectedServer.value.status !== 'running') {
    liveMetric.value = null
    return
  }
  try { liveMetric.value = await api(`/servers/${selectedServer.value.id}/metrics/live/`) } catch { liveMetric.value = null }
}

function closeDetail() {
  closeDetailPanels()
  resetDatabase()
  showDetail.value = false
  selectedServer.value = null
  metrics.value = []
  liveMetric.value = null
}

async function lifecycle(action, server) {
  if (lifecycleAction.value) return
  lifecycleAction.value = action
  try {
    const updatedServer = await api(`/servers/${server.id}/${action}/`, { method: 'POST', body: '{}' })
    servers.value = servers.value.map(item => item.id === updatedServer.id ? updatedServer : item)
    if (selectedServer.value?.id === updatedServer.id) await selectServer(updatedServer.id)
  } catch (err) { error.value = err.message } finally { lifecycleAction.value = '' }
}

async function deleteServer(server) {
  if (!window.confirm(`Delete '${server.name}'? This removes its Docker container and hides it from the active inventory.`)) return
  try {
    await api(`/servers/${server.id}/`, { method: 'DELETE' })
    if (selectedServer.value?.id === server.id) closeDetail()
    await loadDashboard()
  } catch (err) { error.value = err.message }
}

function disposeTerminalView() {
  terminalResizeObserver?.disconnect()
  terminalResizeObserver = null
  terminal?.dispose()
  terminal = null
  if (terminalHost.value) terminalHost.value.innerHTML = ''
}

function closeTerminal() {
  terminalSocket?.close()
  terminalSocket = null
  disposeTerminalView()
  terminalStatus.value = 'Offline'
  terminalPath.value = 'Resolving path…'
  terminalOutputBuffer = ''
  terminalTextDecoder = null
}

async function reconnectTerminal() {
  closeTerminal()
  await nextTick()
  openTerminal()
}

function closeLogs() {
  logsSocket?.close()
  logsSocket = null
  logs.value = ''
  logsStatus.value = 'Disconnected'
  logsPinnedToBottom = true
}

function scrollLogsToBottom(force = false) {
  const host = logsHost.value
  if (!host) return
  if (!force && !logsPinnedToBottom) return
  host.scrollTop = host.scrollHeight
}

function trackLogsScroll() {
  const host = logsHost.value
  if (!host) return
  logsPinnedToBottom = host.scrollHeight - host.scrollTop - host.clientHeight < 48
}

watch(logs, async () => {
  await nextTick()
  scrollLogsToBottom()
})

watch(detailPanel, async panel => {
  if (panel !== 'logs') return
  await nextTick()
  logsPinnedToBottom = true
  scrollLogsToBottom(true)
})

function openLogs() {
  const server = selectedServer.value
  if (!server || detailPanel.value !== 'logs' || !['creating', 'running'].includes(server.status) || !server.container_id || !token.value || logsSocket) return
  logsPinnedToBottom = true
  const socketUrl = new URL(apiBase)
  socketUrl.protocol = socketUrl.protocol === 'https:' ? 'wss:' : 'ws:'
  socketUrl.pathname = `/ws/servers/${server.id}/logs/`
  socketUrl.search = `token=${encodeURIComponent(token.value)}`
  logsStatus.value = 'Connecting...'
  const socket = new WebSocket(socketUrl)
  logsSocket = socket
  socket.onopen = () => { logsStatus.value = 'Connected' }
  socket.onmessage = event => {
    if (logsSocket !== socket) return
    const message = JSON.parse(event.data)
    if (message.type === 'logs') {
      logs.value = message.logs || 'No container output yet.'
    } else if (message.type === 'error') {
      logsStatus.value = 'Stream error'
      logs.value = message.message || 'The log stream is temporarily unavailable.'
    }
  }
  socket.onerror = () => { if (logsSocket === socket) logsStatus.value = 'Connection failed' }
  socket.onclose = () => {
    if (logsSocket !== socket) return
    logsSocket = null
    logsStatus.value = 'Reconnecting...'
    window.setTimeout(openLogs, 3000)
  }
}

function terminalPathMarkerSuffixLength(value) {
  for (let length = Math.min(value.length, terminalPathMarker.length - 1); length > 0; length -= 1) {
    if (terminalPathMarker.startsWith(value.slice(-length))) return length
  }
  return 0
}

function writeTerminalOutput(terminalInstance, data) {
  terminalOutputBuffer += terminalTextDecoder.decode(new Uint8Array(data), { stream: true })
  let output = ''

  while (terminalOutputBuffer) {
    const markerStart = terminalOutputBuffer.indexOf(terminalPathMarker)
    if (markerStart === -1) {
      const suffixLength = terminalPathMarkerSuffixLength(terminalOutputBuffer)
      output += terminalOutputBuffer.slice(0, terminalOutputBuffer.length - suffixLength)
      terminalOutputBuffer = terminalOutputBuffer.slice(terminalOutputBuffer.length - suffixLength)
      break
    }

    output += terminalOutputBuffer.slice(0, markerStart)
    const markerEnd = terminalOutputBuffer.indexOf(terminalPathMarker, markerStart + terminalPathMarker.length)
    if (markerEnd === -1) {
      terminalOutputBuffer = terminalOutputBuffer.slice(markerStart)
      break
    }

    const path = terminalOutputBuffer.slice(markerStart + terminalPathMarker.length, markerEnd)
    if (path.startsWith('/')) terminalPath.value = path
    terminalOutputBuffer = terminalOutputBuffer.slice(markerEnd + terminalPathMarker.length)
  }

  if (output) terminalInstance.write(output)
}

function terminalCloseMessage(code) {
  const messages = {
    4404: 'Server unavailable',
    4409: 'Start the server first',
    3000: 'Docker could not open the shell',
  }
  return messages[code] || (code === 1000 ? 'Shell exited' : 'Shell connection closed')
}

function openTerminal() {
  if (!selectedServer.value || selectedServer.value.status !== 'running' || detailPanel.value !== 'shell' || !terminalHost.value || !token.value || terminalSocket) return
  disposeTerminalView()
  const activeTerminal = new Terminal({ cursorBlink: true, fontFamily: 'DM Mono, monospace', fontSize: 13, theme: { background: '#0f1714', foreground: '#f0fff4', cursor: '#c9f2df' } })
  terminal = activeTerminal
  const serverId = selectedServer.value.id
  const fitAddon = new FitAddon()
  activeTerminal.loadAddon(fitAddon)
  activeTerminal.open(terminalHost.value)
  fitAddon.fit()
  const socketUrl = new URL(apiBase)
  socketUrl.protocol = socketUrl.protocol === 'https:' ? 'wss:' : 'ws:'
  socketUrl.pathname = `/ws/servers/${serverId}/terminal/`
  socketUrl.search = `token=${encodeURIComponent(token.value)}`
  terminalStatus.value = 'Connecting...'
  const socket = new WebSocket(socketUrl)
  terminalSocket = socket
  socket.binaryType = 'arraybuffer'
  terminalPath.value = 'Resolving path…'
  terminalOutputBuffer = ''
  terminalTextDecoder = new TextDecoder()
  const encoder = new TextEncoder()
  const resize = () => {
    fitAddon.fit()
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: 'resize', cols: activeTerminal.cols, rows: activeTerminal.rows }))
  }
  activeTerminal.onData(data => {
    if (socket.readyState === WebSocket.OPEN) socket.send(encoder.encode(data))
  })
  socket.onopen = () => {
    terminalRetryCount = 0
    terminalStatus.value = 'Connected'
    resize()
    activeTerminal.focus()
  }
  socket.onmessage = event => {
    if (terminalSocket !== socket) return
    if (typeof event.data === 'string') {
      const message = JSON.parse(event.data)
      if (message.type === 'terminal_error') {
        terminalStatus.value = message.message
        activeTerminal.write(`\r\n\x1b[31m${message.message}\x1b[0m\r\n`)
      }
      return
    }
    writeTerminalOutput(activeTerminal, event.data)
  }
  socket.onerror = () => { if (terminalSocket === socket) terminalStatus.value = 'Shell connection failed' }
  socket.onclose = event => {
    if (terminalSocket !== socket) return
    terminalSocket = null
    const message = terminalCloseMessage(event.code)
    terminalStatus.value = message
    activeTerminal.write(`\r\n\x1b[31m${message}\x1b[0m\r\n`)
    // A container restart closes the shell unexpectedly; reopen it automatically.
    const retryable = ![1000, 4404].includes(event.code)
    if (retryable && terminalRetryCount < 3 && detailPanel.value === 'shell' && selectedServer.value?.id === serverId && selectedServer.value.status === 'running' && showDetail.value) {
      const delay = [3000, 6000, 10000][terminalRetryCount] || 10000
      terminalRetryCount += 1
      terminalStatus.value = `Reconnecting (${terminalRetryCount}/3)...`
      window.setTimeout(() => {
        if (!terminalSocket && detailPanel.value === 'shell' && selectedServer.value?.id === serverId && selectedServer.value.status === 'running' && showDetail.value) openTerminal()
      }, delay)
    }
  }
  terminalResizeObserver = new ResizeObserver(resize)
  terminalResizeObserver.observe(terminalHost.value)
}

async function createServer() {
  try {
    const payload = { ...createForm.value, env_vars: { ...createForm.value.env_vars } }
    if (payload.image === 'postgres') {
      payload.env_vars.POSTGRES_DB = payload.postgres_database || 'minicloud'
      payload.env_vars.POSTGRES_USER = payload.postgres_username || 'minicloud'
      payload.env_vars.POSTGRES_PASSWORD = payload.postgres_password || crypto.randomUUID().replaceAll('-', '')
    }
    delete payload.postgres_database
    delete payload.postgres_username
    delete payload.postgres_password
    if (!payload.ssh_enabled) {
      payload.ssh_public_key = ''
      payload.ssh_allowed_from = ''
    }
    const created = await api('/servers/', { method: 'POST', body: JSON.stringify(payload) })
    showCreate.value = false
    createForm.value = { name: '', image: 'nginx', framework: '', database_size_mb: null, template: templates.value[0]?.id || '', custom_image: '', env_vars: {}, postgres_database: 'minicloud', postgres_username: 'minicloud', postgres_password: '', ssh_enabled: false, ssh_public_key: '', ssh_allowed_from: '' }
    await loadDashboard()
    if (created?.id) await selectServer(created.id)
  } catch (err) { error.value = err.message }
}

function openIncidentModal() {
  if (!selectedServer.value) return
  const inactive = incidentOptions.find(option => !openIncidentTypes.value.has(option.value))
  incidentForm.value = { type: (inactive || incidentOptions[0]).value }
  incidentError.value = ''
  incidentSubmitting.value = false
  showIncidentModal.value = true
  nextTick(() => incidentDialog.value?.focus())
}

function closeIncidentModal() {
  if (incidentSubmitting.value) return
  showIncidentModal.value = false
  incidentError.value = ''
}

async function triggerIncident() {
  const server = selectedServer.value
  if (!server || incidentSubmitting.value) return
  incidentSubmitting.value = true
  incidentError.value = ''
  try {
    await api(`/servers/${server.id}/trigger_incident/`, {
      method: 'POST',
      body: JSON.stringify(incidentForm.value),
    })
    showIncidentModal.value = false
    await loadDashboard()
    await selectServer(server.id)
  } catch (err) {
    incidentError.value = err.message
  } finally {
    incidentSubmitting.value = false
  }
}

async function markAllNotificationsRead() {
  try {
    await api('/notifications/mark_all_read/', { method: 'POST' })
    notifications.value = notifications.value.map(n => ({ ...n, is_read: true }))
  } catch (err) {
    error.value = err.message
  }
}

async function runHealthCheck() {
  loading.value = true
  try {
    await api('/servers/run_health_check/', { method: 'POST' })
    await loadDashboard()
  } catch (err) {
    error.value = err.message
  } finally {
    loading.value = false
  }
}

function setFrameworkDefault() {
  const frameworkOptions = {
    python: ['fastapi', 'flask', 'django'],
    node: ['express', 'nextjs'],
  }
  const options = frameworkOptions[createForm.value.image] || []
  if (!options.includes(createForm.value.framework)) createForm.value.framework = ''
  if (createForm.value.image !== 'postgres') createForm.value.database_size_mb = null
}

function connectNotifications() {
  if (!token.value || notificationSocket) return
  const socketUrl = new URL(apiBase)
  socketUrl.protocol = socketUrl.protocol === 'https:' ? 'wss:' : 'ws:'
  socketUrl.pathname = '/ws/notifications/'
  socketUrl.search = `token=${encodeURIComponent(token.value)}`
  const socket = new WebSocket(socketUrl)
  notificationSocket = socket
  socket.onmessage = event => {
    let data
    try {
      data = JSON.parse(event.data)
    } catch {
      return
    }
    if (!data || typeof data !== 'object' || data.id === undefined || data.id === null) return
    // Add to persistent list
    notifications.value.unshift(data)
    // Add to transient toasts
    const toastId = Date.now() + Math.random()
    toasts.value.unshift({ ...data, id: toastId })
    if (toasts.value.length > 5) toasts.value.pop()
    setTimeout(() => removeToast(toastId), 10000)
  }
  socket.onclose = () => {
    notificationSocket = null
    setTimeout(connectNotifications, 5000)
  }
}

function removeToast(id) {
  toasts.value = toasts.value.filter(t => t.id !== id)
}

function removeNotification(id) {
  notifications.value = notifications.value.filter(n => n.id !== id)
}

onMounted(async () => {
  await loadDashboard()
  window.setInterval(refreshLiveMetric, 5000)
  connectNotifications()
})

onBeforeUnmount(() => {
  closeTerminal()
  closeLogs()
  stopProvisioningPoll()
  if (notificationSocket) {
    notificationSocket.close()
    notificationSocket = null
  }
})
</script>

<template>
  <main v-if="!token" class="login-shell">
    <section class="login-panel">
      <div class="brand-mark"><span>MC</span><div>MINICLOUD <small>LOCAL INFRASTRUCTURE</small></div></div>
      <h1>Operate your<br><em>private cloud.</em></h1>
      <p>Provision, observe, and control Docker workloads from one calm command center.</p>
      <form @submit.prevent="login">
        <label>Username<input v-model="loginForm.username" autocomplete="username" required></label>
        <label>Password<input v-model="loginForm.password" type="password" autocomplete="current-password" required></label>
        <p v-if="loginError" class="error">{{ loginError }}</p>
        <button class="primary-button">Enter console <ArrowUpRight :size="16" /></button>
      </form>
    </section>
    <aside class="login-art"><div class="orbit orbit-one"></div><div class="orbit orbit-two"></div><div class="art-core"><CircleDot :size="56" stroke-width="1.5" /><strong>RUN<br>LOCAL</strong></div><div class="art-caption">SELF-HOSTED<br>SERVER CONTROL</div></aside>
  </main>

  <main v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand-mark"><span>MC</span><div>MINICLOUD <small>LOCAL INFRASTRUCTURE</small></div></div>
      <nav>
        <a :class="{ active: activeTab === 'overview' }" @click="activeTab = 'overview'"><LayoutDashboard :size="16" /> Overview</a>
        <a :class="{ active: activeTab === 'notifications' }" @click="activeTab = 'notifications'">
          <Bell :size="16" /> Notifications
          <b v-if="notifications.filter(n => !n.is_read).length">{{ notifications.filter(n => !n.is_read).length }}</b>
        </a>
        <a :class="{ active: activeTab === 'templates' }" @click="activeTab = 'templates'"><Server :size="16" /> Templates</a>
      </nav>
      <div class="sidebar-footer"><div class="user-chip"><span>{{ username.slice(0, 1).toUpperCase() }}</span><div>{{ username }}<small>Workspace owner</small></div></div><button class="logout" @click="logout">Sign out <LogOut :size="15" /></button></div>
    </aside>

    <section class="workspace">
      <header v-if="activeTab === 'overview'"><div><button v-if="showDetail" class="back-button" @click="closeDetail"><ArrowLeft :size="15" /> All servers</button><div class="eyebrow">{{ showDetail ? 'Server / details' : 'Workspace / overview' }}</div><h1>{{ showDetail ? selectedServer?.name : `Good to see you, ${username}.` }}</h1><p>{{ showDetail ? 'Inspect telemetry and work directly inside this container.' : 'Here is the pulse of your local infrastructure.' }}</p></div><button v-if="!showDetail" class="primary-button" @click="showCreate = true"><Plus :size="16" /> Provision server</button></header>
      
      <header v-else-if="activeTab === 'notifications'">
        <div>
          <div class="eyebrow">System Alerts</div>
          <h1>Notifications</h1>
          <p>{{ unreadNotifications.length }} unread of {{ notifications.length }} events across your infrastructure.</p>
        </div>
        <div class="notification-controls">
          <div class="segmented">
            <button :class="{ active: notificationFilter === 'all' }" @click="notificationFilter = 'all'">All <b>{{ notifications.length }}</b></button>
            <button :class="{ active: notificationFilter === 'unread' }" @click="notificationFilter = 'unread'">Unread <b>{{ unreadNotifications.length }}</b></button>
          </div>
          <button class="text-button" :disabled="!unreadNotifications.length" @click="markAllNotificationsRead"><Check :size="14" /> Mark all as read</button>
        </div>
      </header>

      <header v-else-if="activeTab === 'templates'">
        <div>
          <div class="eyebrow">Resource Presets</div>
          <h1>Templates</h1>
          <p>Standardized configurations for your Docker workloads.</p>
        </div>
      </header>

      <p v-if="error" class="notice error">{{ error }}</p>

      <template v-if="activeTab === 'overview'">
        <section class="stats-grid"><article><span class="stat-label">Total servers</span><strong>{{ servers.length.toString().padStart(2, '0') }}</strong><small>Provisioned workloads</small></article><article><span class="stat-label">Running now</span><strong>{{ runningCount.toString().padStart(2, '0') }}</strong><small class="green"><CircleDot :size="11" /> Operational</small></article><article><span class="stat-label">Allocated memory</span><strong>{{ totalMemory }}<i> MB</i></strong><small>Across active limits</small></article><article><span class="stat-label">System health</span><strong class="green">Good</strong><small class="green"><CircleDot :size="11" /> All systems nominal</small></article></section>

        <section v-if="!showDetail" class="content-grid"><div class="server-section"><div class="section-heading"><div><div class="eyebrow">Managed resources</div><h2>Your servers</h2></div>
      <div class="db-header-actions">
        <button class="text-button" @click="runHealthCheck"><RotateCw :size="14" /> Run Health Check</button>
        <button class="text-button" @click="loadDashboard"><RefreshCw :size="14" /> Refresh</button>
      </div>
      </div><div v-if="loading" class="empty-state">Syncing with the control plane...</div><div v-else-if="!servers.length" class="empty-state">No servers yet. Provision your first workload.</div><div v-else class="server-list"><article v-for="server in servers" :key="server.id" class="server-row" @click="selectServer(server.id)"><div class="server-icon"><Server :size="19" /></div><div class="server-main"><div class="server-title"><h3>{{ server.name }}</h3><span :class="['status', server.status]">{{ server.status_display }}</span></div><p class="server-meta"><span><Box :size="13" /> {{ server.framework_display || server.image_display }}</span><span><Cpu :size="13" /> {{ server.template_name }}</span><span><Network :size="13" /> {{ server.host_port || 'Not mapped' }}</span></p></div><div class="server-actions"><button v-if="server.status === 'running'" @click.stop="lifecycle('stop', server)">Stop</button><button v-else-if="['stopped', 'failed'].includes(server.status)" @click.stop="lifecycle('start', server)">Start</button><button title="Restart" aria-label="Restart" @click.stop="lifecycle('restart', server)"><RotateCw :size="15" /></button><button class="delete-action" title="Delete server" aria-label="Delete server" @click.stop="deleteServer(server)"><Trash2 :size="15" /></button><ChevronRight :size="17" /></div></article></div></div></section>
      </template>

      <template v-else-if="activeTab === 'notifications'">
        <div v-if="!notifications.length" class="empty-state">No notifications yet. Incidents opened or resolved on your servers appear here.</div>
        <div v-else-if="!visibleNotifications.length" class="empty-state">Nothing unread — you are all caught up.</div>
        <div v-else class="notification-groups">
          <section v-for="group in groupedNotifications" :key="group.label">
            <h3 class="notification-day">{{ group.label }}</h3>
            <div class="server-list">
              <article v-for="note in group.items" :key="note.id" :class="['server-row', 'notification-row', { unread: !note.is_read }]" @click="markNotificationRead(note)">
                <div :class="['server-icon', note.level]">
                  <ShieldAlert v-if="note.kind === 'incident_opened'" :size="19" />
                  <Check v-else-if="note.kind === 'incident_resolved'" :size="19" />
                  <Bell v-else :size="19" />
                </div>
                <div class="notification-main">
                  <div class="server-title">
                    <h3>{{ note.title || note.kind_display }}</h3>
                    <span v-if="note.title" :class="['notification-kind', note.level]">{{ note.kind_display }}</span>
                  </div>
                  <p class="notification-message">{{ note.message }}</p>
                  <p class="server-meta">
                    <span :title="formatDateTime(note.created_at)"><CircleDot v-if="!note.is_read" :size="11" /> {{ relativeTime(note.created_at) }}</span>
                    <span v-if="note.server_name"><Box :size="13" /> {{ note.server_name }}</span>
                    <span v-else><Box :size="13" /> System</span>
                  </p>
                </div>
                <div class="notification-side">
                  <button
                    class="detail-control"
                    :disabled="!note.server || note.server_status === 'deleted'"
                    :title="note.server ? `Open ${note.server_name}` : 'This notification has no linked server'"
                    @click.stop="openNotificationServer(note)"
                  >
                    <ArrowUpRight :size="14" /> {{ note.server_status === 'deleted' ? 'Server removed' : 'View server' }}
                  </button>
                  <span v-if="note.server" :class="['status', note.server_status]">{{ note.server_status }}</span>
                </div>
              </article>
            </div>
          </section>
        </div>
      </template>

      <template v-else-if="activeTab === 'templates'">
        <div v-if="!templates.length" class="empty-state">No templates found.</div>
        <div v-else class="server-list">
          <article v-for="tpl in templates" :key="tpl.id" class="server-row template-row">
            <div class="server-icon"><Cpu :size="19" /></div>
            <div class="server-main">
              <div class="server-title"><h3>{{ tpl.name }}</h3></div>
              <p class="server-meta"><span><Cpu :size="13" /> {{ tpl.cpu_limit }} Cores</span><span><Database :size="13" /> {{ tpl.memory_mb }} MB RAM</span></p>
              <p v-if="tpl.description" class="template-desc">{{ tpl.description }}</p>
            </div>
          </article>
        </div>
      </template>
    </section>

<section v-if="showDetail && selectedServer" class="server-detail-page">
      <div class="detail-toolbar"><button class="back-button" @click="closeDetail"><ArrowLeft :size="15" /> All servers</button><span :class="['status', selectedServer.status]">{{ selectedServer.status_display }}</span></div>
      <div class="detail-heading"><div><div class="eyebrow">{{ selectedServer.image === 'postgres' ? 'Databases / PostgreSQL' : 'Selected resource / container details' }}</div><h2>{{ selectedServer.name }}</h2><p class="detail-meta"><span><Box :size="13" /> {{ selectedServer.framework_display || selectedServer.image_display }}</span><span><Cpu :size="13" /> {{ selectedServer.template_name }}</span></p></div><span v-if="selectedServer.status === 'running'" class="live-dot"><CircleDot :size="11" /> LIVE</span></div>
      <div class="detail-controls" aria-label="Server controls">
        <button v-if="['stopped', 'failed'].includes(selectedServer.status)" class="detail-control primary" :disabled="Boolean(lifecycleAction)" @click="lifecycle('start', selectedServer)">{{ lifecycleAction === 'start' ? 'Starting…' : 'Start server' }}</button>
        <button v-if="selectedServer.status === 'running'" class="detail-control" :disabled="Boolean(lifecycleAction)" @click="lifecycle('stop', selectedServer)">{{ lifecycleAction === 'stop' ? 'Stopping…' : 'Stop server' }}</button>
        <button v-if="selectedServer.status === 'running'" class="detail-control" :disabled="Boolean(lifecycleAction)" @click="lifecycle('restart', selectedServer)"><RotateCw :size="14" /> {{ lifecycleAction === 'restart' ? 'Restarting…' : 'Restart' }}</button>
        <button class="detail-control" :disabled="Boolean(lifecycleAction)" @click="openIncidentModal"><ShieldAlert :size="14" /> Trigger Incident</button>
        <button class="detail-control" :disabled="loading || Boolean(lifecycleAction)" @click="loadDashboard"><RefreshCw :size="14" /> Refresh</button>
        <button class="detail-control danger" :disabled="Boolean(lifecycleAction)" @click="deleteServer(selectedServer)"><Trash2 :size="14" /> Delete</button>
      </div>
      <!-- Provisioning progress indicator -->
      <div v-if="selectedServer.status === 'creating'" class="provisioning-indicator">
        <div class="provisioning-header">
          <h3>Setting up server…</h3>
          <span v-if="selectedServer.provisioning_step_display" class="provisioning-step">{{ selectedServer.provisioning_step_display }}</span>
        </div>
        <div class="provisioning-steps">
          <div v-for="step in visibleProvisioningSteps" :key="step.key" :class="['step', stepState(step.key)]">
            <div class="step-icon">
              <span v-if="stepState(step.key) === 'completed'">✓</span>
              <span v-else-if="stepState(step.key) === 'current'">⟳</span>
              <span v-else>{{ stepIndex(step.key) + 1 }}</span>
            </div>
            <div class="step-label">{{ step.label }}</div>
          </div>
        </div>
        <div class="provisioning-footer">
          <p class="provisioning-hint">Setup runs in the background and can take a few minutes. This page updates automatically.</p>
          <button type="button" class="detail-control" :disabled="!selectedServer.container_id" @click="openDetailPanel('logs')"><ScrollText :size="14" /> View live logs</button>
        </div>
      </div>

      <div v-else-if="selectedServer.status === 'failed'" class="provisioning-failed">
        <div>
          <h3>Setup failed</h3>
          <p>Stopped at step: <strong>{{ selectedServer.provisioning_step_display }}</strong>. Start the server again or check the logs for details.</p>
          <p v-if="selectedServer.provisioning_error" class="provisioning-error-reason">{{ selectedServer.provisioning_error }}</p>
        </div>
        <button type="button" class="detail-control" @click="openDetailPanel('logs')"><ScrollText :size="14" /> View logs</button>
      </div>

      <!-- Unified status + connection + telemetry summary -->
      <div v-else-if="['running', 'stopped'].includes(selectedServer.status)" class="server-summary">
        <div class="summary-main">
          <div class="summary-heading">
            <span :class="['summary-state', { stopped: selectedServer.status !== 'running' }]"><CircleDot :size="12" /> {{ selectedServer.status === 'running' ? 'Ready' : 'Stopped' }}</span>
            <span class="summary-image">{{ selectedServer.image === 'postgres' ? 'PostgreSQL database' : selectedServer.image_display }}</span>
            <a v-if="selectedServer.site_url" :href="selectedServer.site_url" target="_blank" rel="noreferrer" class="summary-link">Open site <ArrowUpRight :size="14" /></a>
          </div>
          <div v-if="selectedServer.postgres_connection_string" class="summary-connection">
            <code>{{ selectedServer.postgres_connection_string }}</code>
            <button type="button" @click="copyText(selectedServer.postgres_connection_string, 'connection')">
              <Check v-if="copiedField === 'connection'" :size="13" /><Copy v-else :size="13" />
              {{ copiedField === 'connection' ? 'Copied' : 'Copy URI' }}
            </button>
          </div>
          <div v-if="selectedServer.ssh_command" class="summary-connection">
            <code>{{ selectedServer.ssh_command }}</code>
            <button type="button" @click="copyText(selectedServer.ssh_command, 'ssh')">
              <Check v-if="copiedField === 'ssh'" :size="13" /><Copy v-else :size="13" />
              {{ copiedField === 'ssh' ? 'Copied' : 'Copy SSH' }}
            </button>
          </div>
          <div v-if="selectedServer.ssh_enabled" class="ssh-access">
            <p v-if="selectedServer.ssh_exposed && !selectedServer.ssh_allowed_from" class="ssh-note warning"><ShieldAlert :size="13" />{{ selectedServer.ssh_host }} is reachable from the internet and every source is accepted until you list the addresses you connect from.</p>
            <p v-else-if="selectedServer.ssh_exposed" class="ssh-note"><Info :size="13" />{{ selectedServer.ssh_host }} is reachable from the internet; your router has to forward to port {{ selectedServer.ssh_port }}.</p>
            <p v-else class="ssh-note"><Info :size="13" />SSH host <code>{{ selectedServer.ssh_host }}</code> is a local address, so it only works where this machine is reachable.</p>
            <div class="ssh-allowlist">
              <label>Allowed from<input v-model="sshAllowlistDraft" spellcheck="false" placeholder="any source"></label>
              <button type="button" :disabled="sshAllowlistSaving || sshAllowlistDraft === (selectedServer.ssh_allowed_from || '')" @click="saveSshAllowlist"><RefreshCw v-if="sshAllowlistSaving" class="spin" :size="13" />{{ sshAllowlistSaving ? 'Saving…' : 'Save allowlist' }}</button>
              <button type="button" :disabled="sshProbing" @click="checkSshReachability"><RefreshCw v-if="sshProbing" class="spin" :size="13" /><Network v-else :size="13" />Check reachability</button>
            </div>
            <p v-if="sshAllowlistError" class="error">{{ sshAllowlistError }}</p>
            <p v-if="sshProbe" :class="['ssh-probe', sshProbe.state]">{{ sshProbe.message }}</p>
          </div>
        </div>
        <dl class="summary-stats">
          <div><dt>CPU</dt><dd>{{ cpuUsage }}<i>%</i></dd><small>of {{ selectedServer.effective_cpu_limit ?? selectedServer.template_cpu_limit }} cores</small></div>
          <div><dt>Memory</dt><dd>{{ memoryUsage }}<i>MB</i></dd><small>of {{ selectedServer.effective_memory_mb ?? selectedServer.template_memory_mb }} MB</small></div>
          <div><dt>Uptime</dt><dd>{{ latestMetric?.uptime_display || '—' }}</dd><small>since last restart</small></div>
          <div><dt>Host port</dt><dd>{{ selectedServer.host_port || '—' }}</dd><small>{{ selectedServer.postgres_connection_string ? 'published on localhost' : 'container mapping' }}</small></div>
          <div v-if="selectedServer.ssh_enabled"><dt>SSH port</dt><dd>{{ selectedServer.ssh_port || '—' }}</dd><small>key only · {{ selectedServer.ssh_allowed_from ? 'allowlisted sources' : 'any source' }}</small></div>
          <div v-if="selectedServer.image === 'postgres'"><dt>Storage</dt><dd>{{ selectedServer.database_size_display || 'No limit' }}</dd><small>writable layer</small></div>
        </dl>
      </div>

      <!-- Cohesive tools dock: one panel open at a time -->
      <div class="detail-dock">
        <div class="dock-tabs" role="tablist" aria-label="Server tools">
          <span class="dock-label">Tools</span>
          <button type="button" role="tab" :aria-selected="detailPanel === 'shell'" :class="{ active: detailPanel === 'shell' }" :disabled="selectedServer.status !== 'running'" @click="openDetailPanel('shell')"><SquareTerminal :size="14" /> Shell</button>
          <button v-if="selectedServer.image === 'postgres'" type="button" role="tab" :aria-selected="detailPanel === 'database'" :class="{ active: detailPanel === 'database' }" :disabled="selectedServer.status !== 'running'" @click="openDetailPanel('database')"><Database :size="14" /> Database</button>
          <button type="button" role="tab" :aria-selected="detailPanel === 'logs'" :class="{ active: detailPanel === 'logs' }" :disabled="!['creating', 'running'].includes(selectedServer.status)" @click="openDetailPanel('logs')"><ScrollText :size="14" /> Logs <i v-if="logsStatus === 'Connected'" class="dock-live-dot"></i></button>
          <button type="button" role="tab" :aria-selected="detailPanel === 'details'" :class="{ active: detailPanel === 'details' }" @click="openDetailPanel('details')"><Info :size="14" /> Details <b v-if="selectedServer.open_incidents_count">{{ selectedServer.open_incidents_count }}</b></button>
        </div>

        <div v-if="detailPanel === 'database' && selectedServer.status === 'running'" class="dock-panel database-panel">
          <div class="database-workbench">
            <div class="db-header">
              <div class="db-heading">
                <h3><Database :size="16" /> {{ db.database || 'PostgreSQL' }}</h3>
                <p>{{ db.version ? `PostgreSQL ${db.version}` : 'PostgreSQL database' }} · {{ db.tables.length }} table{{ db.tables.length === 1 ? '' : 's' }}</p>
              </div>
              <div class="db-header-actions">
                <span class="db-database-select">
                  <select :value="db.database" aria-label="Switch database" @change="loadDatabase($event.target.value)">
                    <option v-for="item in db.databases" :key="item.name" :value="item.name">{{ item.name }}</option>
                  </select>
                </span>
                <button type="button" class="detail-control db-icon-button" :disabled="db.loading" title="Refresh" aria-label="Refresh" @click="loadDatabase()"><RefreshCw :size="15" /></button>
                <button type="button" class="detail-control primary" @click="openTableCreator"><Plus :size="14" /> New table</button>
              </div>
            </div>

            <p v-if="db.error" class="notice error">{{ db.error }}</p>

            <div class="db-tabs" role="tablist">
              <button type="button" :class="{ active: db.tab === 'data' }" @click="setDatabaseTab('data')">Data</button>
              <button type="button" :class="{ active: db.tab === 'structure' }" :disabled="!db.table" @click="setDatabaseTab('structure')">Structure</button>
              <button type="button" :class="{ active: db.tab === 'sql' }" @click="setDatabaseTab('sql')">SQL console</button>
            </div>

            <div class="db-body">
              <aside class="db-sidebar">
                <div class="db-sidebar-heading"><span>Tables</span><b>{{ db.tables.length }}</b></div>
                <p v-if="db.loading && !db.tables.length" class="db-empty">Loading tables…</p>
                <p v-else-if="!db.tables.length" class="db-empty">No tables yet. Create your first table.</p>
                <button
                  v-for="table in db.tables"
                  :key="`${table.schema}.${table.name}`"
                  type="button"
                  :class="['db-table-item', { active: db.selected?.schema === table.schema && db.selected?.name === table.name }]"
                  @click="selectDatabaseTable(table)"
                >
                  <span class="db-table-name"><Table2 :size="13" /> {{ table.name }}</span>
                  <span class="db-table-meta">{{ table.schema }} · ~{{ table.estimated_rows }} rows · {{ formatBytes(table.size_bytes) }}</span>
                </button>
              </aside>

              <div class="db-main">
                <!-- SQL console -->
                <div v-if="db.tab === 'sql'" class="db-sql">
                  <div class="db-sql-bar">
                    <span>Run SQL against <strong>{{ db.database }}</strong></span>
                    <button type="button" class="detail-control primary" :disabled="db.running" @click="runDatabaseQuery"><Play :size="13" /> {{ db.running ? 'Running…' : 'Run query' }}</button>
                  </div>
                  <textarea
                    v-model="db.sql"
                    class="db-sql-input"
                    spellcheck="false"
                    placeholder="SELECT * FROM public.my_table LIMIT 100;"
                    @keydown.ctrl.enter.prevent="runDatabaseQuery"
                    @keydown.meta.enter.prevent="runDatabaseQuery"
                  ></textarea>
                  <p class="db-sql-hint">Ctrl + Enter runs the statement. Results are capped at 500 rows.</p>
                  <div v-if="db.result" class="db-sql-result">
                    <div class="db-sql-meta">
                      <span>{{ db.result.command || 'Result' }}</span>
                      <span>{{ db.result.row_count }} row{{ db.result.row_count === 1 ? '' : 's' }}{{ db.result.truncated ? ' (truncated)' : '' }} · {{ db.result.duration_ms }} ms</span>
                    </div>
                    <div v-if="db.result.columns.length" class="db-grid-scroll">
                      <table class="db-grid">
                        <thead><tr><th v-for="column in db.result.columns" :key="column">{{ column }}</th></tr></thead>
                        <tbody>
                          <tr v-for="(row, index) in db.result.rows" :key="index">
                            <td v-for="(cell, cellIndex) in row" :key="cellIndex" :class="{ 'db-null': cell === null }">{{ formatCell(cell) }}</td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                    <p v-else class="db-empty">{{ db.result.command || 'Statement executed.' }}</p>
                  </div>
                </div>

                <!-- Table data / structure -->
                <template v-else-if="db.table">
                  <div class="db-table-header">
                    <div>
                      <h3>{{ db.table.schema }}.{{ db.table.table }}</h3>
                      <p>{{ db.table.total_rows }} row{{ db.table.total_rows === 1 ? '' : 's' }} · {{ db.table.columns.length }} column{{ db.table.columns.length === 1 ? '' : 's' }}</p>
                    </div>
                    <div class="db-table-actions">
                      <button type="button" class="detail-control db-icon-button" title="Add row" aria-label="Add row" @click="openRowEditor()"><Plus :size="15" /></button>
                      <button type="button" class="detail-control db-icon-button" title="Truncate table" aria-label="Truncate table" @click="truncateDatabaseTable"><Eraser :size="15" /></button>
                      <button type="button" class="detail-control danger db-icon-button" title="Drop table" aria-label="Drop table" @click="dropDatabaseTable"><Trash2 :size="15" /></button>
                    </div>
                  </div>

                  <div v-if="db.tab === 'data'" class="db-data">
                    <div class="db-grid-scroll">
                      <table class="db-grid db-grid-editable">
                        <thead>
                          <tr>
                            <th class="db-grid-actions"></th>
                            <th v-for="column in db.table.columns" :key="column.name">
                              {{ column.name }}<small>{{ column.type }}</small>
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr v-for="(row, index) in db.table.rows" :key="index">
                            <td class="db-grid-actions">
                              <button type="button" title="Edit row" aria-label="Edit row" @click="openRowEditor(row)"><Pencil :size="13" /></button>
                              <button type="button" class="db-row-delete" title="Delete row" aria-label="Delete row" @click="deleteDatabaseRow(row)"><Trash2 :size="13" /></button>
                            </td>
                            <td v-for="column in db.table.columns" :key="column.name" :class="{ 'db-null': row[column.name] === null }" :title="formatCell(row[column.name])">{{ formatCell(row[column.name]) }}</td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                    <p v-if="!db.table.rows.length" class="db-empty">This table has no rows yet.</p>
                    <div class="db-pagination">
                      <span>Rows {{ db.table.total_rows ? db.table.offset + 1 : 0 }}–{{ Math.min(db.table.offset + db.table.limit, db.table.total_rows) }} of {{ db.table.total_rows }}</span>
                      <div>
                        <button type="button" class="detail-control" :disabled="db.offset === 0" @click="databasePage(-1)"><ChevronLeft :size="13" /> Previous</button>
                        <button type="button" class="detail-control" :disabled="db.table.offset + db.table.limit >= db.table.total_rows" @click="databasePage(1)">Next <ChevronRight :size="13" /></button>
                      </div>
                    </div>
                  </div>

                  <div v-else class="db-structure">
                    <div v-for="column in db.table.columns" :key="column.name" class="db-column">
                      <div class="db-column-name">
                        <strong>{{ column.name }}</strong>
                        <span v-if="column.primary_key" class="db-badge pk">PK</span>
                        <span v-if="!column.nullable" class="db-badge">NOT NULL</span>
                      </div>
                      <div class="db-column-type">{{ column.type }}</div>
                      <div class="db-column-default">
                        <span>Default</span>
                        <code>{{ column.default_value || '—' }}</code>
                      </div>
                    </div>
                  </div>
                </template>

                <div v-else class="db-empty">
                  <Table2 :size="22" />
                  <p>Select a table to browse its rows, inspect its structure, or edit data.</p>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="detailPanel === 'shell'" class="dock-panel terminal-panel">
          <div class="terminal-bar">
            <span class="terminal-lights"><i></i><i></i><i></i></span>
            <span>{{ terminalTitle }}</span>
            <span class="terminal-state">{{ terminalStatus }}<button v-if="selectedServer.status === 'running' && terminalStatus !== 'Connected'" type="button" class="terminal-reconnect" @click="reconnectTerminal">Reconnect</button></span>
          </div>
          <div ref="terminalHost" class="real-terminal-body" aria-label="Interactive container terminal"></div>
        </div>

        <div v-else-if="detailPanel === 'logs'" class="dock-panel logs-panel">
          <div class="logs-card">
            <div class="logs-header">
              <h3>Live logs</h3>
              <span :class="['logs-status', logsStatus === 'Connected' ? 'connected' : 'connecting']">{{ logsStatus }}</span>
            </div>
            <pre ref="logsHost" class="logs-content" aria-label="Container logs" @scroll="trackLogsScroll">{{ logs || 'Waiting for container output...' }}</pre>
          </div>
        </div>

        <div v-else-if="detailPanel === 'details'" class="dock-panel details-panel">
          <div class="container-details-card">
            <h3>Container</h3>
            <dl class="container-details">
              <div><dt>Template</dt><dd>{{ selectedServer.template_name }} ({{ selectedServer.template_cpu_limit }} CPU / {{ selectedServer.template_memory_mb }} MB)</dd></div>
              <div v-if="selectedServer.effective_memory_mb > selectedServer.template_memory_mb"><dt>Memory limit</dt><dd>{{ selectedServer.effective_memory_mb }} MB (XAMPP minimum)</dd></div>
              <div><dt>Host port</dt><dd>{{ selectedServer.host_port || 'Not assigned' }}</dd></div>
              <div v-if="selectedServer.ssh_enabled"><dt>SSH port</dt><dd>{{ selectedServer.ssh_port || 'Not assigned' }}</dd></div>
              <div v-if="selectedServer.ssh_enabled"><dt>SSH host</dt><dd>{{ selectedServer.ssh_host }}</dd></div>
              <div v-if="selectedServer.ssh_enabled"><dt>SSH allowed from</dt><dd>{{ selectedServer.ssh_allowed_from || 'Any source' }}</dd></div>
              <div v-if="selectedServer.image === 'postgres'"><dt>Storage limit</dt><dd>{{ selectedServer.database_size_display || 'No limit' }}</dd></div>
              <div><dt>Container ID</dt><dd><code>{{ selectedServer.container_id || 'Not created' }}</code></dd></div>
              <div><dt>Created</dt><dd>{{ formatDate(selectedServer.created_at) }}</dd></div>
            </dl>
          </div>
          <div class="incidents-card">
            <h3>Incidents <span class="subtitle">Most recent first</span></h3>
            <div v-if="selectedServer.incidents && selectedServer.incidents.length" class="incidents-list">
              <div v-for="incident in selectedServer.incidents" :key="incident.id" class="incident-item">
                <div class="incident-info">
                  <strong>{{ incident.type_display }}</strong>
                  <div class="incident-time">Opened {{ formatDateTime(incident.opened_at) }}</div>
                </div>
                <span :class="['incident-badge', incident.status]">{{ incident.status_display }}</span>
              </div>
            </div>
            <p v-else class="empty-state">No incidents recorded for this server.</p>
          </div>
        </div>
      </div>

    </section>

    <!-- Database row editor -->
    <div v-if="rowEditor.open && db.table" class="modal-backdrop" @click.self="rowEditor.open = false">
      <form class="create-modal db-modal" @submit.prevent="saveRow">
        <div class="section-heading">
          <div>
            <div class="eyebrow">{{ rowEditor.mode === 'insert' ? 'New row' : 'Edit row' }}</div>
            <h2>{{ db.table.schema }}.{{ db.table.table }}</h2>
          </div>
          <button type="button" class="close-button" aria-label="Close" @click="rowEditor.open = false"><X :size="20" /></button>
        </div>
        <p v-if="rowEditor.error" class="notice error">{{ rowEditor.error }}</p>
        <div v-for="column in editableColumns" :key="column.name" class="db-field">
          <div class="db-field-label"><span>{{ column.name }}</span><small>{{ column.type }}</small></div>
          <div class="db-field-control">
            <select v-if="isBooleanColumn(column)" v-model="rowEditor.fields[column.name].value" :disabled="rowEditor.fields[column.name].nil">
              <option value="true">true</option>
              <option value="false">false</option>
            </select>
            <textarea v-else-if="isJsonColumn(column)" v-model="rowEditor.fields[column.name].value" :disabled="rowEditor.fields[column.name].nil" rows="3" spellcheck="false"></textarea>
            <input v-else v-model="rowEditor.fields[column.name].value" :disabled="rowEditor.fields[column.name].nil" :placeholder="column.default_value || (column.nullable ? 'NULL' : '')">
            <label class="db-null-toggle"><input type="checkbox" v-model="rowEditor.fields[column.name].nil"> NULL</label>
          </div>
        </div>
        <p v-if="rowEditor.mode === 'insert'" class="db-sql-hint">Leave a field empty to use its database default.</p>
        <div class="db-modal-actions">
          <button type="button" class="detail-control" @click="rowEditor.open = false">Cancel</button>
          <button type="submit" class="detail-control primary" :disabled="rowEditor.saving">{{ rowEditor.saving ? 'Saving…' : 'Save row' }}</button>
        </div>
      </form>
    </div>

    <!-- Create table -->
    <div v-if="tableCreator.open" class="modal-backdrop" @click.self="tableCreator.open = false">
      <form class="create-modal db-modal db-create-table" @submit.prevent="createDatabaseTable">
        <div class="section-heading">
          <div><div class="eyebrow">New table</div><h2>Create table</h2></div>
          <button type="button" class="close-button" aria-label="Close" @click="tableCreator.open = false"><X :size="20" /></button>
        </div>
        <div class="db-create-meta">
          <label>Schema<input v-model="tableCreator.schema" required placeholder="public"></label>
          <label>Table name<input v-model="tableCreator.name" required placeholder="customers"></label>
        </div>
        <div class="db-create-columns">
          <div class="db-create-heading"><span>Columns</span><button type="button" class="text-button" @click="addCreatorColumn"><Plus :size="13" /> Add column</button></div>
          <div v-for="(column, index) in tableCreator.columns" :key="index" class="db-create-column">
            <input v-model="column.name" placeholder="column_name" required>
            <select v-model="column.type">
              <option v-for="type in dbColumnTypes" :key="type" :value="type">{{ type }}</option>
            </select>
            <label class="db-check"><input type="checkbox" v-model="column.primary_key"> PK</label>
            <label class="db-check"><input type="checkbox" v-model="column.nullable"> Null</label>
            <input v-model="column.default" placeholder="default expression">
            <button type="button" class="close-button" aria-label="Remove column" @click="removeCreatorColumn(index)"><X :size="15" /></button>
          </div>
        </div>
        <p v-if="tableCreator.error" class="notice error">{{ tableCreator.error }}</p>
        <div class="db-modal-actions">
          <button type="button" class="detail-control" @click="tableCreator.open = false">Cancel</button>
          <button type="submit" class="detail-control primary" :disabled="tableCreator.saving">{{ tableCreator.saving ? 'Creating…' : 'Create table' }}</button>
        </div>
      </form>
    </div>

    <div v-if="showIncidentModal" class="modal-backdrop" @click.self="closeIncidentModal">
      <form class="incident-modal" role="dialog" aria-modal="true" aria-labelledby="incident-title" tabindex="-1"
            @submit.prevent="triggerIncident" @keydown.esc="closeIncidentModal">
        <div class="incident-head section-heading">
          <div><div class="eyebrow">Monitoring sandbox</div><h2 id="incident-title">Trigger an incident</h2></div>
          <button type="button" class="close-button" aria-label="Close" @click="closeIncidentModal"><X :size="20" /></button>
        </div>

        <div class="incident-body">
          <div class="incident-target">
            <div class="incident-target-icon"><Box :size="15" /></div>
            <div class="incident-target-body">
              <strong>{{ selectedServer?.name }}</strong>
              <small>{{ selectedServer?.image_display }} · {{ selectedServer?.template_name }} · {{ selectedServer?.effective_cpu_limit }} CPU / {{ selectedServer?.effective_memory_mb }} MB</small>
            </div>
            <span :class="['status', selectedServer?.status]">{{ selectedServer?.status_display }}</span>
          </div>

          <fieldset class="incident-options">
            <legend>Incident type</legend>
            <label v-for="option in incidentOptions" :key="option.value"
                   :class="['incident-option', option.tone, { selected: incidentForm.type === option.value }]">
              <input type="radio" v-model="incidentForm.type" :value="option.value" />
              <span class="incident-option-icon"><component :is="option.icon" :size="16" /></span>
              <span class="incident-option-body">
                <span class="incident-option-title">
                  {{ option.label }}
                  <em v-if="openIncidentTypes.has(option.value)" class="incident-active-tag">already open</em>
                </span>
                <span v-if="incidentForm.type === option.value" class="incident-option-detail">{{ option.detail }}</span>
              </span>
            </label>
          </fieldset>

          <p class="incident-preview">
            <Bell :size="13" />
            <span>Expect a <strong>{{ selectedIncidentOption.label }}</strong> entry in Notifications, plus a matching row in the incident history below.</span>
          </p>

          <p v-if="incidentError" class="error">{{ incidentError }}</p>
        </div>

        <div class="modal-actions">
          <button type="button" class="text-button" :disabled="incidentSubmitting" @click="closeIncidentModal">Cancel</button>
          <button class="primary-button danger" type="submit" :disabled="incidentSubmitting">
            {{ incidentSubmitting ? 'Triggering…' : 'Trigger incident' }}
            <RefreshCw v-if="incidentSubmitting" class="spin" :size="16" />
            <ShieldAlert v-else :size="16" />
          </button>
        </div>
      </form>
    </div>

    <div v-if="showCreate" class="modal-backdrop" @click.self="showCreate = false"><form class="create-modal" @submit.prevent="createServer"><div class="section-heading"><div><div class="eyebrow">New resource</div><h2>Provision server</h2></div><button type="button" class="close-button" aria-label="Close" @click="showCreate = false"><X :size="20" /></button></div><label>Server name<input v-model="createForm.name" required placeholder="edge-web-01"></label><label>Image<select v-model="createForm.image" @change="setFrameworkDefault"><option value="nginx">Nginx</option><option value="python">Python</option><option value="node">Node.js</option><option value="php">PHP / XAMPP</option><option value="postgres">PostgreSQL</option><option value="custom">Custom</option></select></label><p v-if="createForm.image === 'php'" class="framework-help">XAMPP installs Apache, MariaDB, PHP, Perl, and <code>nano</code>, then starts Apache automatically. Your PHP files live in <code>/opt/lampp/htdocs</code>.</p><label v-if="createForm.image === 'python'">Python framework<select v-model="createForm.framework"><option value="">Blank Python (runtime only)</option><option value="fastapi">FastAPI</option><option value="flask">Flask</option><option value="django">Django</option></select></label><label v-if="createForm.image === 'node'">Node.js framework<select v-model="createForm.framework"><option value="">Blank Node.js (runtime only)</option><option value="express">Express</option><option value="nextjs">Next.js</option></select></label><p v-if="['python', 'node'].includes(createForm.image)" class="framework-help"><template v-if="createForm.framework">A starter project is created in <code>/workspace</code>; framework dependencies install before the app starts.</template><template v-else>The runtime and <code>nano</code> are ready in the interactive shell, and a basic landing page remains available at the published URL.</template></p><label>Template<select v-model="createForm.template" required :disabled="!templates.length"><option value="" disabled>{{ templates.length ? 'Choose a resource preset' : 'No templates available' }}</option><option v-for="template in templates" :key="template.id" :value="template.id">{{ template.name }} - {{ template.cpu_limit }} CPU / {{ template.memory_mb }} MB</option></select></label><p v-if="!templates.length" class="error">No templates are available. Seed the built-in templates before provisioning.</p><label v-if="createForm.image === 'custom'">Custom image<input v-model="createForm.custom_image" placeholder="registry/image:tag"></label><template v-if="createForm.image === 'postgres'"><p class="postgres-help">Credentials are used to initialize this isolated PostgreSQL container. Leave the password blank to generate one; copy its connection URI from the detail page after provisioning.</p><label>Database name<input v-model="createForm.postgres_database" required pattern="[A-Za-z_][A-Za-z0-9_]*"></label><label>Database user<input v-model="createForm.postgres_username" required pattern="[A-Za-z_][A-Za-z0-9_]*"></label><label>Database password<input v-model="createForm.postgres_password" type="password" minlength="12" placeholder="Generated securely if left blank"></label><label>Database storage limit<select v-model="createForm.database_size_mb"><option :value="null">No limit</option><option :value="512">512 MB</option><option :value="1024">1 GB</option><option :value="5120">5 GB</option><option :value="10240">10 GB</option></select></label><p class="postgres-help">A selected limit constrains the PostgreSQL container writable layer, including its database files.</p></template><label class="ssh-toggle"><input v-model="createForm.ssh_enabled" type="checkbox">Enable SSH access</label><label v-if="createForm.ssh_enabled" class="ssh-key">Public key<textarea v-model="createForm.ssh_public_key" rows="3" spellcheck="false" placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1… you@laptop"></textarea></label><label v-if="createForm.ssh_enabled">Allowed from<input v-model="createForm.ssh_allowed_from" spellcheck="false" placeholder="203.0.113.7, 198.51.100.0/24"></label><p v-if="createForm.ssh_enabled" class="framework-help">sshd runs inside the container with password sign-in disabled, so only this key can connect as <code>root</code>. Leave <em>Allowed from</em> blank to accept any source, or list the IP addresses and CIDR ranges you connect from — sshd rejects every other source before the key is offered. The <code>ssh</code> command appears on the server page; it uses <code>MINICLOUD_SSH_HOST</code>, so set it to an address your client can actually reach (a LAN address, or a static/DDNS name once the router forwards to this SSH port).</p><button class="primary-button" :disabled="!templates.length"><Plus :size="16" /> Provision container</button></form></div>

    <div class="toast-container">
      <TransitionGroup name="toast">
        <div v-for="note in toasts" :key="note.id" :class="['toast', note.level]">
          <div class="toast-icon">
            <Check v-if="note.level === 'success'" :size="16" />
            <X v-else-if="note.level === 'error'" :size="16" />
            <Info v-else :size="16" />
          </div>
          <div class="toast-content">{{ note.message }}</div>
          <button type="button" class="toast-close" aria-label="Close notification" @click="removeToast(note.id)"><X :size="14" /></button>
        </div>
      </TransitionGroup>
    </div>
  </main>
</template>


