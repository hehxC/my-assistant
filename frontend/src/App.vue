<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

const LEGACY_STORAGE_KEY = 'warm-chat-messages'
const LEGACY_THREAD_KEY = 'warm-chat-thread-id'
const LEGACY_STUDY_SESSION_KEY = 'warm-chat-study-session'
const STORAGE_KEY = 'assistant-chat-messages'
const THREAD_KEY = 'assistant-chat-thread-id'
const STUDY_SESSION_KEY = 'assistant-study-session'

const currentUser = ref(null)
const authChecking = ref(true)
const authMode = ref('login')
const authUsername = ref('')
const authPassword = ref('')
const authLoading = ref(false)
const authError = ref('')
const threadId = ref('')

// 以下响应式变量分别管理消息、输入框和请求状态。
const messages = ref([])
const draft = ref('')
const isLoading = ref(false)
const error = ref('')
const conversation = ref(null)
const activeStudySession = ref(null)
const elapsedSeconds = ref(0)
const studyLoading = ref(false)
const studyMessage = ref('')
let studyTimer = null
const activeView = ref('chat')
const studyStats = ref(null)
const statsLoading = ref(false)
const statsError = ref('')
const selectedStudyDay = ref(null)

const today = new Date()
const halfMonthAgo = new Date()
halfMonthAgo.setDate(today.getDate() - 14)
const statsStartDate = ref(formatDateInput(halfMonthAgo))
const statsEndDate = ref(formatDateInput(today))

// 输入为空或模型正在回复时，不允许重复发送。
const canSend = computed(() => draft.value.trim() && !isLoading.value)
const isStudying = computed(() => activeStudySession.value !== null)
const formattedStudyTime = computed(() => formatDuration(elapsedSeconds.value))
const maxDailySeconds = computed(() => {
  if (!studyStats.value?.days.length) return 0
  return Math.max(...studyStats.value.days.map((item) => item.duration_seconds))
})

function userStorageKey(key) {
  return `${key}:${currentUser.value.id}`
}

function readStoredJson(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key) || JSON.stringify(fallback))
  } catch {
    localStorage.removeItem(key)
    return fallback
  }
}

function formatDateInput(value) {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

onMounted(restoreLogin)

onBeforeUnmount(() => clearInterval(studyTimer))

async function restoreLogin() {
  try {
    const response = await fetch('/api/auth/me', { credentials: 'include' })
    if (!response.ok) return
    currentUser.value = await response.json()
    await initializeWorkspace()
  } catch {
    authError.value = '暂时无法连接服务器。'
  } finally {
    authChecking.value = false
  }
}

async function submitAuth() {
  if (authLoading.value) return
  authLoading.value = true
  authError.value = ''

  const isRegistering = authMode.value === 'register'
  const payload = {
    username: authUsername.value,
    password: authPassword.value,
  }
  if (isRegistering) {
    const legacyThreadId = localStorage.getItem(LEGACY_THREAD_KEY)
    if (legacyThreadId) payload.legacy_thread_id = legacyThreadId
  }

  try {
    const response = await fetch(`/api/auth/${authMode.value}`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '暂时无法完成操作')

    currentUser.value = isRegistering ? data.user : data
    await initializeWorkspace(isRegistering && data.claimed_legacy_data)
    authPassword.value = ''
  } catch (requestError) {
    const message = requestError.message || '暂时无法连接服务器。'
    if (currentUser.value) error.value = message
    else authError.value = message
  } finally {
    authLoading.value = false
  }
}

async function initializeWorkspace(claimLegacyData = false) {
  if (claimLegacyData) migrateLegacyBrowserData()

  const savedMessages = readStoredJson(userStorageKey(STORAGE_KEY), [])
  messages.value = Array.isArray(savedMessages) ? savedMessages : []

  const savedSession = readStoredJson(userStorageKey(STUDY_SESSION_KEY), null)
  if (savedSession?.session_id && savedSession?.started_at) {
    activeStudySession.value = savedSession
    startStudyTimer()
  }

  threadId.value = localStorage.getItem(userStorageKey(THREAD_KEY)) || ''
  if (!threadId.value) {
    await createNewThread()
    return
  }
  await loadChatHistory()
}

function migrateLegacyBrowserData() {
  const legacyMessages = localStorage.getItem(LEGACY_STORAGE_KEY)
  const legacyThreadId = localStorage.getItem(LEGACY_THREAD_KEY)
  const legacyStudySession = localStorage.getItem(LEGACY_STUDY_SESSION_KEY)

  if (legacyMessages) localStorage.setItem(userStorageKey(STORAGE_KEY), legacyMessages)
  if (legacyThreadId) localStorage.setItem(userStorageKey(THREAD_KEY), legacyThreadId)
  if (legacyStudySession) {
    localStorage.setItem(userStorageKey(STUDY_SESSION_KEY), legacyStudySession)
  }

  localStorage.removeItem(LEGACY_STORAGE_KEY)
  localStorage.removeItem(LEGACY_THREAD_KEY)
  localStorage.removeItem(LEGACY_STUDY_SESSION_KEY)
}

async function createNewThread() {
  const response = await fetch('/api/chat/threads', {
    method: 'POST',
    credentials: 'include',
  })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '暂时无法创建对话')

  threadId.value = data.thread_id
  localStorage.setItem(userStorageKey(THREAD_KEY), threadId.value)
}

async function loadChatHistory() {
  const response = await fetch(`/api/chat/threads/${encodeURIComponent(threadId.value)}/messages`, {
    credentials: 'include',
  })
  if (response.status === 404) {
    messages.value = []
    await createNewThread()
    saveMessages()
    return
  }

  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || '暂时无法读取聊天记录')
  if (data.messages.length) {
    messages.value = data.messages
    saveMessages()
  }
  await scrollToLatest()
}

function formatDuration(totalSeconds) {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const parts = [minutes, seconds]

  if (hours > 0) parts.unshift(hours)
  return parts.map((part) => String(part).padStart(2, '0')).join(':')
}

function getStartedAtTimestamp(startedAt) {
  // 后端以 UTC 写入 MySQL；没有时区后缀时，前端补上 Z 再解析。
  const normalized = /(?:Z|[+-]\d{2}:\d{2})$/.test(startedAt) ? startedAt : `${startedAt}Z`
  return Date.parse(normalized)
}

function updateStudyTime() {
  const startedAt = getStartedAtTimestamp(activeStudySession.value.started_at)
  elapsedSeconds.value = Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
}

function startStudyTimer() {
  clearInterval(studyTimer)
  updateStudyTime()
  studyTimer = setInterval(updateStudyTime, 1000)
}

function formatStatDuration(totalSeconds) {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)

  if (hours > 0) return `${hours} 小时 ${minutes} 分钟`
  return `${minutes} 分钟`
}

function formatChartDate(value) {
  return value.slice(5).replace('-', '/')
}

function formatBarValue(totalSeconds) {
  if (totalSeconds === 0) return ''
  if (totalSeconds >= 3600) return `${(totalSeconds / 3600).toFixed(1)}h`
  return `${Math.ceil(totalSeconds / 60)}分`
}

function getBarHeight(durationSeconds) {
  if (durationSeconds === 0 || maxDailySeconds.value === 0) return '0%'
  return `${Math.max(4, (durationSeconds / maxDailySeconds.value) * 100)}%`
}

async function loadStudyStats() {
  if (!statsStartDate.value || !statsEndDate.value) return

  statsLoading.value = true
  statsError.value = ''
  selectedStudyDay.value = null

  try {
    const params = new URLSearchParams({
      start_date: statsStartDate.value,
      end_date: statsEndDate.value,
    })
    const response = await fetch(`/api/study/stats?${params}`, { credentials: 'include' })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '查询学习记录失败')

    studyStats.value = data
  } catch (requestError) {
    statsError.value = requestError.message || '暂时无法读取学习统计。'
  } finally {
    statsLoading.value = false
  }
}

function openStudyStats() {
  activeView.value = 'stats'
  loadStudyStats()
}

function openStudyDay(item) {
  selectedStudyDay.value = item
}

async function startStudy() {
  if (isStudying.value || studyLoading.value) return

  studyLoading.value = true
  studyMessage.value = ''

  try {
    const response = await fetch('/api/study/start', {
      method: 'POST',
      credentials: 'include',
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '开始学习失败')

    // 保存服务端生成的 session_id，页面刷新后仍能停止同一条记录。
    activeStudySession.value = data
    localStorage.setItem(userStorageKey(STUDY_SESSION_KEY), JSON.stringify(data))
    startStudyTimer()
  } catch (requestError) {
    studyMessage.value = requestError.message || '暂时无法开始计时。'
  } finally {
    studyLoading.value = false
  }
}

async function stopStudy() {
  if (!isStudying.value || studyLoading.value) return

  studyLoading.value = true
  studyMessage.value = ''

  try {
    const response = await fetch('/api/study/stop', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: activeStudySession.value.session_id }),
    })
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '停止学习失败')

    // 只有数据库确认保存成功后，才结束前端计时并清除活动会话。
    clearInterval(studyTimer)
    elapsedSeconds.value = data.duration_seconds
    activeStudySession.value = null
    localStorage.removeItem(userStorageKey(STUDY_SESSION_KEY))
    studyMessage.value = `已记录 ${formatDuration(data.duration_seconds)}`
    if (activeView.value === 'stats') await loadStudyStats()
  } catch (requestError) {
    studyMessage.value = requestError.message || '暂时无法保存学习时间。'
  } finally {
    studyLoading.value = false
  }
}

// 每次消息变化后，把界面消息同步到 localStorage。
function saveMessages() {
  localStorage.setItem(userStorageKey(STORAGE_KEY), JSON.stringify(messages.value))
}

// 等待 Vue 更新 DOM 后，将聊天区域滚动到最新消息。
async function scrollToLatest() {
  await nextTick()
  conversation.value?.scrollTo({ top: conversation.value.scrollHeight, behavior: 'smooth' })
}

// 提交当前输入，并等待后端返回模型回复。
async function sendMessage() {
  const content = draft.value.trim()

  // 忽略空消息，并防止模型回复期间重复提交。
  if (!content || isLoading.value) return

  // 先把用户消息显示在界面上，减少等待时的迟滞感。
  messages.value.push({ role: 'user', content })
  draft.value = ''
  error.value = ''
  isLoading.value = true
  saveMessages()
  await scrollToLatest()

  try {
    // 后端通过 thread_id 找回历史，因此这里只发送最新一条消息。
    const response = await fetch('/api/chat', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        thread_id: threadId.value,
        message: content,
      }),
    })

    // FastAPI 的成功和错误响应都使用 JSON。
    const data = await response.json()
    if (!response.ok) throw new Error(data.detail || '消息发送失败')

    // 把模型回复追加到界面，并保存展示记录。
    messages.value.push({ role: 'assistant', content: data.content })
    saveMessages()
  } catch (requestError) {
    // 请求失败时保留用户输入记录，并显示可理解的错误信息。
    error.value = requestError.message || '连接出现问题，请稍后再试。'
  } finally {
    // 无论成功或失败都恢复输入状态，并滚动到最新位置。
    isLoading.value = false
    await scrollToLatest()
  }
}

// Enter 发送消息，Shift + Enter 保留 textarea 的换行行为。
function handleKeydown(event) {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    sendMessage()
  }
}

async function clearConversation() {
  error.value = ''

  try {
    await createNewThread()
    messages.value = []
    localStorage.removeItem(userStorageKey(STORAGE_KEY))
  } catch (requestError) {
    error.value = requestError.message || '暂时无法创建新对话。'
  }
}

function switchAuthMode() {
  authMode.value = authMode.value === 'login' ? 'register' : 'login'
  authError.value = ''
  authPassword.value = ''
}

async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' })
  } finally {
    clearInterval(studyTimer)
    currentUser.value = null
    threadId.value = ''
    messages.value = []
    activeStudySession.value = null
    elapsedSeconds.value = 0
    activeView.value = 'chat'
  }
}
</script>

<template>
  <main v-if="authChecking" class="auth-loading">正在打开个人工作助手...</main>

  <main v-else-if="!currentUser" class="auth-page">
    <section class="auth-intro">
      <div class="brand" aria-hidden="true">助</div>
      <p class="eyebrow">个人工作助手</p>
      <h1>把每天的事情，放在一个地方。</h1>
      <p>登录后，你的对话、学习记录和计划只属于当前账号。</p>
    </section>

    <section class="auth-card" aria-labelledby="auth-title">
      <p class="auth-kicker">{{ authMode === 'login' ? '欢迎回来' : '创建账号' }}</p>
      <h2 id="auth-title">{{ authMode === 'login' ? '登录' : '注册' }}</h2>

      <form @submit.prevent="submitAuth">
        <label for="auth-username">用户名</label>
        <input
          id="auth-username"
          v-model="authUsername"
          name="username"
          autocomplete="username"
          minlength="3"
          maxlength="32"
          required
        />

        <label for="auth-password">密码</label>
        <input
          id="auth-password"
          v-model="authPassword"
          name="password"
          type="password"
          :autocomplete="authMode === 'login' ? 'current-password' : 'new-password'"
          :minlength="authMode === 'register' ? 8 : 1"
          maxlength="128"
          required
        />

        <p v-if="authError" class="auth-error" role="alert">{{ authError }}</p>
        <button class="auth-submit" type="submit" :disabled="authLoading">
          {{ authLoading ? '请稍候' : authMode === 'login' ? '登录' : '注册并登录' }}
        </button>
      </form>

      <button class="auth-switch" type="button" @click="switchAuthMode">
        {{ authMode === 'login' ? '没有账号？去注册' : '已有账号？去登录' }}
      </button>
    </section>
  </main>

  <div v-else class="app-layout">
    <aside class="sidebar" aria-label="主导航">
      <p class="sidebar-brand">个人工作助手</p>
      <nav class="sidebar-nav">
        <button
          type="button"
          :class="{ selected: activeView === 'chat' }"
          @click="activeView = 'chat'"
        >
          助手对话
        </button>
        <button
          type="button"
          :class="{ selected: activeView === 'stats' }"
          @click="openStudyStats"
        >
          当人了吗
        </button>
      </nav>
      <div class="sidebar-account">
        <span>{{ currentUser.username }}</span>
        <button type="button" @click="logout">退出登录</button>
      </div>
    </aside>

    <!-- 页面主体分为介绍区和聊天区。 -->
    <main v-if="activeView === 'chat'" class="shell">
    <!-- 桌面端左侧的产品介绍区域。 -->
    <section class="intro" aria-labelledby="page-title">
      <div class="brand" aria-hidden="true">助</div>
      <div>
        <p class="eyebrow">你的个人工作助手</p>
        <h1 id="page-title">今天要推进什么？</h1>
        <p class="intro-copy">梳理思路、拆解任务，或者解决一个具体问题。</p>
      </div>

      <button v-if="messages.length" class="clear-button" type="button" @click="clearConversation">
        清空对话
      </button>
    </section>

    <!-- aria-label 让辅助技术可以识别聊天区域。 -->
    <section class="chat" aria-label="聊天区域">
      <!-- aria-live 会在出现新消息时通知屏幕阅读器。 -->
      <div ref="conversation" class="conversation" aria-live="polite">
        <!-- 没有消息时展示空状态。 -->
        <div v-if="!messages.length" class="empty-state">
          <p>准备好了。</p>
          <span>告诉我你的目标、手头的问题，或者下一步想完成的工作。</span>
        </div>

        <!-- 根据 role 为用户和模型消息应用不同样式。 -->
        <div
          v-for="(message, index) in messages"
          :key="`${message.role}-${index}`"
          class="message-row"
          :class="message.role"
        >
          <p class="message">{{ message.content }}</p>
        </div>

        <!-- 请求期间显示回复中的加载反馈。 -->
        <div v-if="isLoading" class="message-row assistant" aria-label="正在回复">
          <div class="message typing"><i></i><i></i><i></i></div>
        </div>
      </div>

      <!-- 接口错误显示在输入区上方。 -->
      <p v-if="error" class="error" role="alert">{{ error }}</p>

      <!-- submit 事件统一支持按钮点击和 Enter 发送。 -->
      <form class="composer" @submit.prevent="sendMessage">
        <label for="message-input">向助手描述你的任务</label>
        <div class="input-row">
          <textarea
            id="message-input"
            v-model="draft"
            rows="1"
            maxlength="8000"
            placeholder="例如：帮我梳理今天的工作重点..."
            :disabled="isLoading"
            @keydown="handleKeydown"
          ></textarea>
          <button type="submit" :disabled="!canSend">发送</button>
        </div>
        <p class="hint">Enter 发送，Shift + Enter 换行</p>
      </form>
    </section>
    </main>

    <main v-else class="stats-view">
      <header class="stats-header">
        <div>
          <p class="eyebrow">学习记录</p>
          <h1>当人了吗</h1>
          <p>看看最近有没有把时间花在自己身上。</p>
        </div>

        <div class="stats-study-panel">
          <p class="study-label">本次当人</p>
          <strong class="study-time">{{ formattedStudyTime }}</strong>
          <button
            class="study-button"
            :class="{ active: isStudying }"
            type="button"
            :disabled="studyLoading"
            @click="isStudying ? stopStudy() : startStudy()"
          >
            {{ studyLoading ? '请稍候' : isStudying ? '停止当人' : '开始当人' }}
          </button>
          <p v-if="studyMessage" class="study-message" role="status">{{ studyMessage }}</p>
        </div>
      </header>

      <form class="stats-filter" @submit.prevent="loadStudyStats">
        <label>
          <span>开始日期</span>
          <input v-model="statsStartDate" type="date" :max="statsEndDate" />
        </label>
        <label>
          <span>结束日期</span>
          <input v-model="statsEndDate" type="date" :min="statsStartDate" />
        </label>
        <button type="submit" :disabled="statsLoading">
          {{ statsLoading ? '查询中' : '查询' }}
        </button>
      </form>

      <p v-if="statsError" class="stats-error" role="alert">{{ statsError }}</p>

      <section class="stats-card" aria-label="每日学习时间柱状图">
        <div class="stats-summary">
          <div>
            <span>累计学习</span>
            <strong>{{ formatStatDuration(studyStats?.total_seconds || 0) }}</strong>
          </div>
          <p v-if="studyStats">
            {{ studyStats.start_date }} 至 {{ studyStats.end_date }}
          </p>
        </div>

        <div v-if="statsLoading && !studyStats" class="chart-loading">正在读取学习记录...</div>

        <div v-else-if="studyStats" class="chart-scroll">
          <div
            class="bar-chart"
            :style="{
              gridTemplateColumns: `repeat(${studyStats.days.length}, minmax(34px, 1fr))`,
              minWidth: `${Math.max(720, studyStats.days.length * 52)}px`,
            }"
          >
            <button
              v-for="item in studyStats.days"
              :key="item.date"
              class="bar-column"
              type="button"
              :aria-label="`查看 ${item.date} 的学习时间段`"
              @click="openStudyDay(item)"
            >
              <div class="bar-value">{{ formatBarValue(item.duration_seconds) }}</div>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :style="{ height: getBarHeight(item.duration_seconds) }"
                ></div>
              </div>
              <span class="bar-date">{{ formatChartDate(item.date) }}</span>
            </button>
          </div>
        </div>

        <p v-if="studyStats && studyStats.total_seconds === 0" class="stats-empty">
          这段时间还没有完成的学习记录。
        </p>
      </section>

      <div
        v-if="selectedStudyDay"
        class="study-detail-backdrop"
        @click.self="selectedStudyDay = null"
      >
        <section class="study-detail" role="dialog" aria-modal="true" aria-labelledby="study-detail-title">
          <header>
            <div>
              <p>学习明细</p>
              <h2 id="study-detail-title">{{ selectedStudyDay.date }}</h2>
            </div>
            <button type="button" aria-label="关闭学习明细" @click="selectedStudyDay = null">×</button>
          </header>

          <strong class="study-detail-total">
            共 {{ formatStatDuration(selectedStudyDay.duration_seconds) }}
          </strong>

          <div v-if="selectedStudyDay.periods.length" class="study-periods">
            <div v-for="(period, index) in selectedStudyDay.periods" :key="`${period.start_time}-${index}`">
              <span>{{ period.start_time }}–{{ period.end_time }}</span>
              <small>{{ formatStatDuration(period.duration_seconds) }}</small>
            </div>
          </div>
          <p v-else class="study-detail-empty">当天没有完成的学习记录。</p>
        </section>
      </div>
    </main>
  </div>
</template>
