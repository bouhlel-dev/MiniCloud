<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { Terminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import {
  ArrowLeft, ArrowUpRight, Box, ChevronRight, CircleDot, Cpu, LayoutDashboard, Network,
  LogOut, Plus, RefreshCw, RotateCw, Server, Trash2, X,
} from '@lucide/vue'

const apiBase = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000/api'
const token = ref(localStorage.getItem('minicloud_token') || '')
const username = ref(localStorage.getItem('minicloud_user') || '')
const loginForm = ref({ username: '', password: '' })
const servers = ref([])
const templates = ref([])
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
const createForm = ref({ name: '', image: 'nginx', framework: '', database_size_mb: null, template: '', custom_image: '', env_vars: {}, postgres_database: 'minicloud', postgres_username: 'minicloud', postgres_password: '' })
let terminal = null
let terminalSocket = null
let logsSocket = null
let provisioningPollTimer = null
let terminalResizeObserver = null
let terminalOutputBuffer = ''
let terminalTextDecoder = null
const terminalPathMarker = '__MINICLOUD_CWD__'

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
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Request failed (${response.status})`)
  return response.status === 204 ? null : response.json()
}

const runningCount = computed(() => servers.value.filter(server => server.status === 'running').length)
const totalMemory = computed(() => servers.value.reduce((total, server) => total + (server.template_memory_mb || 0), 0))

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
          if (updated.container_id) openLogs()
          if (wasCreating && updated.status === 'running') {
            await refreshLiveMetric()
            openTerminal()
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
    if (!createForm.value.template) createForm.value.template = templates.value[0]?.id || ''
    if (selectedServer.value) {
      const updated = servers.value.find(server => server.id === selectedServer.value.id)
      if (updated) selectedServer.value = updated
    }
    if (servers.value.some(server => server.status === 'creating')) startProvisioningPoll()
  } catch (err) {
    error.value = err.message
    if (err.message.includes('401')) logout()
  } finally { loading.value = false }
}

async function selectServer(id) {
  closeTerminal()
  closeLogs()
  selectedServer.value = servers.value.find(server => server.id === id) || null
  if (!selectedServer.value) return
  showDetail.value = true
  metrics.value = await api(`/servers/${id}/metrics/`)
  await refreshLiveMetric()
  await nextTick()
  openTerminal()
  openLogs()
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
  closeTerminal()
  closeLogs()
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

function closeTerminal() {
  terminalResizeObserver?.disconnect()
  terminalResizeObserver = null
  terminalSocket?.close()
  terminalSocket = null
  terminal?.dispose()
  terminal = null
  terminalStatus.value = 'Offline'
  terminalPath.value = 'Resolving path…'
  terminalOutputBuffer = ''
  terminalTextDecoder = null
}

function closeLogs() {
  logsSocket?.close()
  logsSocket = null
  logs.value = ''
  logsStatus.value = 'Disconnected'
}

function openLogs() {
  const server = selectedServer.value
  if (!server || !['creating', 'running'].includes(server.status) || !server.container_id || !token.value || logsSocket) return
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
  if (!selectedServer.value || selectedServer.value.status !== 'running' || !terminalHost.value || !token.value || terminalSocket) return
  const activeTerminal = new Terminal({ cursorBlink: true, fontFamily: 'DM Mono, monospace', fontSize: 13, theme: { background: '#0f1714', foreground: '#f0fff4', cursor: '#c9f2df' } })
  terminal = activeTerminal
  const fitAddon = new FitAddon()
  activeTerminal.loadAddon(fitAddon)
  activeTerminal.open(terminalHost.value)
  fitAddon.fit()
  const socketUrl = new URL(apiBase)
  socketUrl.protocol = socketUrl.protocol === 'https:' ? 'wss:' : 'ws:'
  socketUrl.pathname = `/ws/servers/${selectedServer.value.id}/terminal/`
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
  socket.onopen = () => { terminalStatus.value = 'Connected'; resize(); activeTerminal.focus() }
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
    const message = terminalCloseMessage(event.code)
    if (terminalStatus.value === 'Connected' || terminalStatus.value === 'Shell connection failed') {
      terminalStatus.value = message
      activeTerminal.write(`\r\n\x1b[31m${message}\x1b[0m\r\n`)
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
    const created = await api('/servers/', { method: 'POST', body: JSON.stringify(payload) })
    showCreate.value = false
    createForm.value = { name: '', image: 'nginx', framework: '', database_size_mb: null, template: templates.value[0]?.id || '', custom_image: '', env_vars: {}, postgres_database: 'minicloud', postgres_username: 'minicloud', postgres_password: '' }
    await loadDashboard()
    if (created?.id) await selectServer(created.id)
  } catch (err) { error.value = err.message }
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

onMounted(async () => {
  await loadDashboard()
  window.setInterval(refreshLiveMetric, 5000)
})

onBeforeUnmount(() => { closeTerminal(); closeLogs(); stopProvisioningPoll() })
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
      <nav><a class="active"><LayoutDashboard :size="16" /> Overview</a><a><RefreshCw :size="16" /> Activity <b>0</b></a><a><Server :size="16" /> Templates</a></nav>
      <div class="sidebar-footer"><div class="user-chip"><span>{{ username.slice(0, 1).toUpperCase() }}</span><div>{{ username }}<small>Workspace owner</small></div></div><button class="logout" @click="logout">Sign out <LogOut :size="15" /></button></div>
    </aside>

    <section class="workspace">
      <header><div><button v-if="showDetail" class="back-button" @click="closeDetail"><ArrowLeft :size="15" /> All servers</button><div class="eyebrow">{{ showDetail ? 'Server / details' : 'Workspace / overview' }}</div><h1>{{ showDetail ? selectedServer?.name : `Good to see you, ${username}.` }}</h1><p>{{ showDetail ? 'Inspect telemetry and work directly inside this container.' : 'Here is the pulse of your local infrastructure.' }}</p></div><button v-if="!showDetail" class="primary-button" @click="showCreate = true"><Plus :size="16" /> Provision server</button></header>
      <p v-if="error" class="notice error">{{ error }}</p>
      <section class="stats-grid"><article><span class="stat-label">Total servers</span><strong>{{ servers.length.toString().padStart(2, '0') }}</strong><small>Provisioned workloads</small></article><article><span class="stat-label">Running now</span><strong>{{ runningCount.toString().padStart(2, '0') }}</strong><small class="green"><CircleDot :size="11" /> Operational</small></article><article><span class="stat-label">Allocated memory</span><strong>{{ totalMemory }}<i> MB</i></strong><small>Across active limits</small></article><article><span class="stat-label">System health</span><strong class="green">Good</strong><small class="green"><CircleDot :size="11" /> All systems nominal</small></article></section>

      <section v-if="!showDetail" class="content-grid"><div class="server-section"><div class="section-heading"><div><div class="eyebrow">Managed resources</div><h2>Your servers</h2></div><button class="text-button" @click="loadDashboard"><RefreshCw :size="14" /> Refresh</button></div><div v-if="loading" class="empty-state">Syncing with the control plane...</div><div v-else-if="!servers.length" class="empty-state">No servers yet. Provision your first workload.</div><div v-else class="server-list"><article v-for="server in servers" :key="server.id" class="server-row" @click="selectServer(server.id)"><div class="server-icon"><Server :size="19" /></div><div class="server-main"><div class="server-title"><h3>{{ server.name }}</h3><span :class="['status', server.status]">{{ server.status_display }}</span></div><p class="server-meta"><span><Box :size="13" /> {{ server.framework_display || server.image_display }}</span><span><Cpu :size="13" /> {{ server.template_name }}</span><span><Network :size="13" /> {{ server.host_port || 'Not mapped' }}</span></p></div><div class="server-actions"><button v-if="server.status === 'running'" @click.stop="lifecycle('stop', server)">Stop</button><button v-else-if="['stopped', 'failed'].includes(server.status)" @click.stop="lifecycle('start', server)">Start</button><button title="Restart" aria-label="Restart" @click.stop="lifecycle('restart', server)"><RotateCw :size="15" /></button><button class="delete-action" title="Delete server" aria-label="Delete server" @click.stop="deleteServer(server)"><Trash2 :size="15" /></button><ChevronRight :size="17" /></div></article></div></div></section>
    </section>

<section v-if="showDetail && selectedServer" class="server-detail-page">
      <div class="detail-toolbar"><button class="back-button" @click="closeDetail"><ArrowLeft :size="15" /> All servers</button><span :class="['status', selectedServer.status]">{{ selectedServer.status_display }}</span></div>
      <div class="detail-heading"><div><div class="eyebrow">Selected resource / container details</div><h2>{{ selectedServer.name }}</h2><p class="detail-meta"><span><Box :size="13" /> {{ selectedServer.framework_display || selectedServer.image_display }}</span><span><Cpu :size="13" /> {{ selectedServer.template_name }}</span><span><Network :size="13" /> {{ selectedServer.host_port || 'Not mapped' }}</span></p></div><span class="live-dot"><CircleDot :size="11" /> LIVE</span></div>
      <div class="detail-controls" aria-label="Server controls">
        <button v-if="['stopped', 'failed'].includes(selectedServer.status)" class="detail-control primary" :disabled="Boolean(lifecycleAction)" @click="lifecycle('start', selectedServer)">{{ lifecycleAction === 'start' ? 'Starting…' : 'Start server' }}</button>
        <button v-if="selectedServer.status === 'running'" class="detail-control" :disabled="Boolean(lifecycleAction)" @click="lifecycle('stop', selectedServer)">{{ lifecycleAction === 'stop' ? 'Stopping…' : 'Stop server' }}</button>
        <button v-if="selectedServer.status === 'running'" class="detail-control" :disabled="Boolean(lifecycleAction)" @click="lifecycle('restart', selectedServer)"><RotateCw :size="14" /> {{ lifecycleAction === 'restart' ? 'Restarting…' : 'Restart' }}</button>
        <button class="detail-control" :disabled="loading || Boolean(lifecycleAction)" @click="loadDashboard"><RefreshCw :size="14" /> Refresh</button>
        <button class="detail-control danger" :disabled="Boolean(lifecycleAction)" @click="deleteServer(selectedServer)"><Trash2 :size="14" /> Delete</button>
      </div>
      <a v-if="selectedServer.site_url" class="site-url-card" :href="selectedServer.site_url" target="_blank" rel="noreferrer">Published URL: {{ selectedServer.site_url }} <ArrowUpRight :size="15" /></a>
      <div v-if="selectedServer.postgres_connection_string" class="database-connection"><div><span>POSTGRESQL CONNECTION URI</span><code>{{ selectedServer.postgres_connection_string }}</code></div><button type="button" @click="navigator.clipboard.writeText(selectedServer.postgres_connection_string)">Copy</button></div>
      <div v-if="selectedServer.image === 'postgres'" class="database-storage"><span>POSTGRESQL STORAGE LIMIT</span><strong>{{ selectedServer.database_size_display }}</strong><small>Applies to the container writable layer.</small></div>

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
        <p class="provisioning-hint">Setup runs in the background and can take a few minutes. This page updates automatically.</p>
      </div>

      <div v-else-if="selectedServer.status === 'running'" class="provisioning-complete">
        <CircleDot :size="20" class="ready-icon" />
        <span>Server is ready for use!</span>
        <a v-if="selectedServer.site_url" :href="selectedServer.site_url" target="_blank" rel="noreferrer" class="visit-link">Visit site →</a>
      </div>

      <div v-else-if="selectedServer.status === 'failed'" class="provisioning-failed">
        <h3>Setup failed</h3>
        <p>Stopped at step: <strong>{{ selectedServer.provisioning_step_display }}</strong>. Start the server again or check the logs below for details.</p>
      </div>

      <!-- Live telemetry (original) -->
      <div class="detail-cards">
        <article><span>CPU limit</span><strong>{{ selectedServer.template_cpu_limit }} <i>cores</i></strong></article>
        <article><span>Latest memory</span><strong v-if="liveMetric?.memory_mb !== undefined || metrics.at(-1)?.memory_mb !== undefined">{{ liveMetric?.memory_mb ?? metrics.at(-1)?.memory_mb }} <i>MB</i></strong><strong v-else>Not available</strong></article>
        <article><span>CPU activity</span><strong>{{ liveMetric?.cpu_percent ?? metrics.at(-1)?.cpu_percent ?? '0.00' }}%</strong></article>
        <article><span>Uptime</span><strong>{{ liveMetric?.uptime_display || 'Not available' }}</strong></article>
      </div>

      <!-- Resource limits and container details -->
      <div class="detail-grid">
        <div class="detail-cards resource-limits">
          <article><span>CPU limit</span><strong>{{ selectedServer.template_cpu_limit }} <i>cores</i></strong></article>
          <article><span>Memory limit</span><strong>{{ selectedServer.template_memory_mb }} <i>MB</i></strong></article>
          <article><span>Host port</span><strong>{{ selectedServer.host_port || 'Not assigned' }}</strong></article>
          <article><span>Open incidents</span><strong>{{ selectedServer.open_incidents_count }}</strong></article>
        </div>
        <div class="container-details-card">
          <h3>Container details</h3>
          <dl class="container-details">
            <div><dt>Template</dt><dd>{{ selectedServer.template_name }} ({{ selectedServer.template_cpu_limit }} CPU / {{ selectedServer.template_memory_mb }} MB)</dd></div>
            <div><dt>Host port</dt><dd>{{ selectedServer.host_port || 'Not assigned' }}</dd></div>
            <div><dt>Container ID</dt><dd><code>{{ selectedServer.container_id || 'Not created' }}</code></dd></div>
            <div><dt>Created</dt><dd>{{ formatDate(selectedServer.created_at) }}</dd></div>
          </dl>
        </div>
      </div>

      <!-- Incidents -->
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

      <!-- Live logs -->
      <div class="logs-card">
        <div class="logs-header">
          <h3>Live logs</h3>
          <span :class="['logs-status', logsStatus === 'Connected' ? 'connected' : 'connecting']">{{ logsStatus }}</span>
        </div>
        <pre class="logs-content" aria-label="Container logs">{{ logs || 'Waiting for container output...' }}</pre>
      </div>

      <!-- Interactive terminal -->
      <div class="real-terminal"><div class="terminal-bar"><span class="terminal-lights"><i></i><i></i><i></i></span><span>{{ terminalTitle }}</span><span>{{ terminalStatus }}</span></div><div ref="terminalHost" class="real-terminal-body" aria-label="Interactive container terminal"></div></div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" @click.self="showCreate = false"><form class="create-modal" @submit.prevent="createServer"><div class="section-heading"><div><div class="eyebrow">New resource</div><h2>Provision server</h2></div><button type="button" class="close-button" aria-label="Close" @click="showCreate = false"><X :size="20" /></button></div><label>Server name<input v-model="createForm.name" required placeholder="edge-web-01"></label><label>Image<select v-model="createForm.image" @change="setFrameworkDefault"><option value="nginx">Nginx</option><option value="python">Python</option><option value="node">Node.js</option><option value="php">PHP / XAMPP</option><option value="postgres">PostgreSQL</option><option value="custom">Custom</option></select></label><p v-if="createForm.image === 'php'" class="framework-help">XAMPP installs Apache, MariaDB, PHP, Perl, and <code>nano</code>, then starts Apache automatically. Your PHP files live in <code>/opt/lampp/htdocs</code>.</p><label v-if="createForm.image === 'python'">Python framework<select v-model="createForm.framework"><option value="">Blank Python (runtime only)</option><option value="fastapi">FastAPI</option><option value="flask">Flask</option><option value="django">Django</option></select></label><label v-if="createForm.image === 'node'">Node.js framework<select v-model="createForm.framework"><option value="">Blank Node.js (runtime only)</option><option value="express">Express</option><option value="nextjs">Next.js</option></select></label><p v-if="['python', 'node'].includes(createForm.image)" class="framework-help"><template v-if="createForm.framework">A starter project is created in <code>/workspace</code>; framework dependencies install before the app starts.</template><template v-else>The runtime and <code>nano</code> are ready in the interactive shell, and a basic landing page remains available at the published URL.</template></p><label>Template<select v-model="createForm.template" required :disabled="!templates.length"><option value="" disabled>{{ templates.length ? 'Choose a resource preset' : 'No templates available' }}</option><option v-for="template in templates" :key="template.id" :value="template.id">{{ template.name }} - {{ template.cpu_limit }} CPU / {{ template.memory_mb }} MB</option></select></label><p v-if="!templates.length" class="error">No templates are available. Seed the built-in templates before provisioning.</p><label v-if="createForm.image === 'custom'">Custom image<input v-model="createForm.custom_image" placeholder="registry/image:tag"></label><template v-if="createForm.image === 'postgres'"><p class="postgres-help">Credentials are used to initialize this isolated PostgreSQL container. Leave the password blank to generate one; copy its connection URI from the detail page after provisioning.</p><label>Database name<input v-model="createForm.postgres_database" required pattern="[A-Za-z_][A-Za-z0-9_]*"></label><label>Database user<input v-model="createForm.postgres_username" required pattern="[A-Za-z_][A-Za-z0-9_]*"></label><label>Database password<input v-model="createForm.postgres_password" type="password" minlength="12" placeholder="Generated securely if left blank"></label><label>Database storage limit<select v-model="createForm.database_size_mb"><option :value="null">No limit</option><option :value="512">512 MB</option><option :value="1024">1 GB</option><option :value="5120">5 GB</option><option :value="10240">10 GB</option></select></label><p class="postgres-help">A selected limit constrains the PostgreSQL container writable layer, including its database files.</p></template><button class="primary-button" :disabled="!templates.length"><Plus :size="16" /> Provision container</button></form></div>
  </main>
</template>


