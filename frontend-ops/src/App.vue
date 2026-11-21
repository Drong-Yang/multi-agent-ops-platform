<script setup>
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import * as auth from './api.js'
import LegacyCreator from './LegacyCreator.vue'
import SupportWikiEditor from './SupportWikiEditor.vue'
import { createFusionClient } from './fusion-api.js'
import { normalizeTask, mergeTaskSnapshot, approvedAssetForTask, applyTaskEvent, draftInsightFromTurn, canCreateInsight, canPublishAsset } from './fusion-state.js'
import { lineDiff, versionView } from './version-diff.js'
import { assetReuseView, faqReuseView, createFaqVerification } from './faq-reuse-view.js'

const client = createFusionClient()
const user = ref(null), page = ref('consultation'), error = ref(''), busy = ref(false)
const signingUp = ref(false), credentials = ref({ username: '', password: '' })
const knowledge = ref(null), session = ref(null), question = ref(''), sending = ref(false), liveAnswer = ref('')
const tasks = ref([]), assets = ref([]), current = ref(null), feedback = ref(''), selectedVersion = ref('')
const insightDraft = ref(null), format = ref('faq'), productScope = ref(''), audience = ref('准备选购商品的用户')
const sessions = ref([]), wikiOption = ref(false)
const verificationTarget = ref(null), highlightedAsset = ref(''), highlightedTurn = ref('')
const transcript = ref(null), composer = ref(null), insightSummary = ref(null), pendingQuestion = ref('')
const examples = ['SC-PB27 多重？', '耳机拆封后能退吗？', 'SC-PB20 和 SC-PB27 有什么区别？']
const consultationTurns = computed(() => (session.value?.turns || []).map(turn => ({ ...turn, reuseView: faqReuseView(turn) })))
const assetRows = computed(() => assets.value.map(asset => ({ ...asset, reuseView: assetReuseView(asset, knowledge.value?.knowledge_snapshot_id) })))
let eventsAbort = null, refreshTimer = null
const titles = { consultation: '客服试聊', tasks: '内容任务', assets: '内容资产', free: '自由创作' }
const phaseNames = { queued: '等待执行', running: '正在生成', waiting_human: '等待人工决策', text_approved: '文本已批准', failed: '执行失败', interrupted: '已中断', completed: '已完成' }
const assetNames = { draft: '待发布', published: '已发布', stale: '知识已更新', revoked: '已撤回', superseded: '已有更新版本' }
const typeNames = { faq: 'FAQ', article: '图文', wiki: '售后 Wiki' }
const answerRoutes = { support_wiki: '直接复用售后 Wiki', catalog_quote: '引用商品原文', rag: '原始资料检索',
  business_boundary: '固定业务答复', no_evidence: '待核实 · 资料不足' }
const relatedWikiPages = computed(() => assets.value.filter(asset => asset.wiki_eligible && asset.task_id !== current.value?.task_id))
const nodeNames = { plan_topics: '准备选题', write_draft: '撰写正文', quality_check: '检查内容要求', fact_check: '核对商品事实', human_review: '等待审稿' }
const article = computed(() => versionView(current.value, selectedVersion.value))
const differences = computed(() => article.value.previous === null ? [] : lineDiff(article.value.previous, article.value.content))
const taskSource = computed(() => current.value?.source)
const statusText = computed(() => knowledge.value
  ? (knowledge.value.mode === 'mock' ? '模拟生成' : '真实模型') + ' · ' +
    (knowledge.value.retrieval_mode === 'milvus' ? 'Milvus 检索' : '本地检索')
  : '等待连接')

async function scrollToLatest(force = false) {
  const thread = transcript.value
  if (!thread || (!force && thread.scrollHeight - thread.scrollTop - thread.clientHeight > 90)) return
  await nextTick()
  thread.scrollTop = thread.scrollHeight
}
watch(liveAnswer, () => scrollToLatest())

function prefill(text) {
  question.value = text
  composer.value?.focus()
}

function fail(e) { error.value = e.message || '请求失败，请重试'; if (e.status === 401) logout() }
async function run(fn) { error.value = ''; busy.value = true; try { return await fn() } catch (e) { fail(e) } finally { busy.value = false } }
async function lists() {
  const token = auth.getToken()
  const [taskList, assetList, activeKnowledge, sessionList] = await Promise.all([
    client.tasks(), client.assets(), client.knowledge(), client.sessions()
  ])
  if (token !== auth.getToken()) return
  tasks.value = taskList.tasks
  assets.value = assetList.assets
  knowledge.value = activeKnowledge
  sessions.value = sessionList.sessions
}
async function initialize() {
  user.value = await auth.getCurrentUser()
  await lists()
  const remembered = sessionStorage.getItem('fusion_consultation')
  if (remembered) { try { session.value = await client.getSession(remembered) } catch { session.value = null } }
  if (!session.value) await newSession()
  else await scrollToLatest(true)
}
async function signIn() {
  await run(async () => {
    if (signingUp.value) await auth.register(credentials.value.username, credentials.value.password)
    await auth.login(credentials.value.username, credentials.value.password)
    credentials.value.password = ''; await initialize()
  })
}
function logout() {
  auth.logout()
  user.value = null
  session.value = null
  current.value = null
  knowledge.value = null
  tasks.value = []
  assets.value = []
  sessions.value = []
  page.value = 'consultation'
  question.value = ''
  insightDraft.value = null
  verificationTarget.value = null
  highlightedAsset.value = ''
  highlightedTurn.value = ''
  sessionStorage.removeItem('fusion_consultation')
  eventsAbort?.abort()
}
async function newSession() {
  session.value = await client.createSession(); sessionStorage.setItem('fusion_consultation', session.value.session_id)
  insightDraft.value = null; liveAnswer.value = ''; verificationTarget.value = null
  highlightedTurn.value = ''; question.value = ''
  sessions.value = (await client.sessions()).sessions
}

async function loadConsultation(id) {
  session.value = await client.getSession(id)
  sessionStorage.setItem('fusion_consultation', id)
  insightDraft.value = null
  verificationTarget.value = null
  highlightedTurn.value = ''
  question.value = ''
}

async function switchSession(event) {
  const id = event.target.value
  await run(async () => {
    await loadConsultation(id)
    await scrollToLatest(true)
  })
}

async function openSourceConsultation() {
  if (sending.value) return
  await run(async () => {
    const { session_id, turn_id } = taskSource.value
    await loadConsultation(session_id)
    highlightedTurn.value = turn_id
    page.value = 'consultation'
    await nextTick()
    const turn = document.getElementById('turn-' + turn_id)
    if (!turn) throw new Error('已打开来源会话，未找到对应的问答轮次。')
    turn.focus({ preventScroll: true })
    turn.scrollIntoView({ block: 'center' })
  })
}
async function send() {
  if (!session.value || !question.value.trim() || sending.value) return
  error.value = ''; sending.value = true; liveAnswer.value = ''
  const id = session.value.session_id, text = question.value
  pendingQuestion.value = text
  question.value = ''
  highlightedTurn.value = ''
  await scrollToLatest(true)
  let finished = false
  try {
    await client.message(id, text, event => {
      if (event.type === 'answer_delta') liveAnswer.value += event.payload.content
      if (event.type === 'answer_completed') finished = true
      if (event.type === 'run_failed') error.value = event.payload.message
    })
    const updated = await client.getSession(id)
    if (session.value?.session_id !== id) return
    session.value = updated
    if (finished && verificationTarget.value) verificationTarget.value.sent = true
    if (!finished && !error.value) error.value = '连接已结束，请核对保存的回答后重试。'
  } catch (e) {
    fail(e)
  } finally {
    if (!finished && session.value?.session_id === id && !question.value) question.value = text
    sending.value = false; liveAnswer.value = ''; pendingQuestion.value = ''
    await scrollToLatest(true)
  }
}

async function chooseTurn(turn) {
  insightDraft.value = draftInsightFromTurn(turn)
  wikiOption.value = turn.wiki_candidate !== false
  if (!wikiOption.value && format.value === 'wiki') format.value = 'faq'
  productScope.value = [...new Set((turn.sources || []).map(s => s.sku).filter(Boolean))].join(', ')
  await nextTick()
  insightSummary.value?.focus()
}
async function bridge() {
  if (!canCreateInsight(insightDraft.value)) return
  await run(async () => {
    const insight = await client.createInsight({ ...insightDraft.value, session_id: session.value.session_id })
    const task = await client.createTask({ insight_id: insight.insight_id, content_type: format.value,
      product_scope: productScope.value.split(/[,，\s]+/).filter(Boolean),
      content_brief: { audience: audience.value, min_chars: format.value === 'faq' ? 80 : 600, max_chars: format.value === 'faq' ? 500 : 1400 } })
    insightDraft.value = null; page.value = 'tasks'; await openTask(task.task_id); await lists()
  })
}
async function openTask(id) {
  eventsAbort?.abort(); selectedVersion.value = ''; feedback.value = ''
  current.value = normalizeTask(await client.task(id)); page.value = 'tasks'
  if (current.value.content_type !== 'wiki') watchTask(id)
}
function watchTask(id) {
  eventsAbort?.abort(); const controller = new AbortController(); eventsAbort = controller
  client.events(id, event => { if (current.value?.task_id === id) current.value = applyTaskEvent(current.value, event) }, controller.signal)
    .then(() => { if (!controller.signal.aborted) return lists() }).catch(e => { if (!controller.signal.aborted && e.name !== 'AbortError') fail(e) })
}
async function action(name, data = {}) {
  if (name === 'approve_text' && article.value.historical) return
  await run(async () => {
    const updated = await client.actOnTask(current.value.task_id, { action: name, data, expected_state_version: current.value.state_version })
    current.value = normalizeTask(updated); selectedVersion.value = ''; feedback.value = ''; watchTask(updated.task_id)
    await lists()
  })
}
function draftWiki(fields) {
  const id = current.value.task_id
  return run(async () => {
    const result = await client.draftWiki(id, fields)
    if (current.value?.task_id === id) {
      const { state_version, ...draftFields } = result
      current.value = normalizeTask({ ...current.value, state_version, wiki_draft: { fields: draftFields } })
    }
    return result
  })
}
async function approveWiki(draft) {
  const id = current.value.task_id
  await run(async () => {
    const result = await client.approveWiki(id, draft)
    if (current.value?.task_id === id) current.value = normalizeTask(result.task)
    await lists()
  })
}
async function publish(asset) { await run(async () => { await client.publish(asset); await lists() }) }
async function revoke(asset) { await run(async () => { await client.revoke(asset); await lists() }) }
async function verifyAsset(asset) {
  if (sending.value) return
  await run(async () => {
    if (asset.content_type === 'wiki' && !asset.wiki_eligible) return
    const prepared = asset.content_type === 'wiki'
      ? { session: await client.createSession(), question: asset.wiki.question, title: asset.wiki.title, kind: 'wiki' }
      : await createFaqVerification(client, asset, knowledge.value?.knowledge_snapshot_id)
    session.value = prepared.session; question.value = prepared.question
    sessionStorage.setItem('fusion_consultation', session.value.session_id)
    sessions.value = (await client.sessions()).sessions
    insightDraft.value = null; liveAnswer.value = ''; highlightedTurn.value = ''; verificationTarget.value = { ...prepared, sent: false }
    page.value = 'consultation'
    await nextTick(); composer.value?.focus()
  })
}
async function focusAsset(id) {
  highlightedAsset.value = id
  page.value = 'assets'
  await nextTick()
  const card = document.getElementById('asset-' + id)
  card?.focus({ preventScroll: true })
  card?.scrollIntoView({ block: 'start' })
}

async function showAsset(id) {
  await run(async () => {
    await lists()
    if (!assets.value.some(asset => asset.asset_id === id)) throw new Error('这份素材当前不可用；本轮历史引用记录仍保留。')
    await focusAsset(id)
  })
}

async function openApprovedAsset() {
  const task = current.value
  await run(async () => {
    await lists()
    const asset = approvedAssetForTask(assets.value, task)
    if (!asset) throw new Error('当前批准版本的内容资产暂未就绪，请刷新后重试。')
    await focusAsset(asset.asset_id)
  })
}
async function voice() {
  // Open the tab during the click, before awaiting exchange-code creation.
  const tab = window.open('about:blank', '_blank')
  if (!tab) { error.value = '浏览器阻止了新窗口，请允许本站打开语音渠道。'; return }
  await run(async () => {
    try { const handoff = await client.handoff(); tab.location.href = `http://127.0.0.1:5179/#handoff=${encodeURIComponent(handoff.code)}` }
    catch (e) { tab?.close(); throw e }
  })
}
function authExpired() { logout() }
onMounted(async () => {
  window.addEventListener('auth:logout', authExpired)
  if (auth.getToken()) await run(initialize)
  refreshTimer = setInterval(async () => {
    if (!user.value || !current.value || !['queued', 'running'].includes(current.value.status)) return
    const id = current.value.task_id
    try {
      const snapshot = await client.task(id)
      current.value = mergeTaskSnapshot(current.value, snapshot)
    } catch (e) {
      if (current.value?.task_id === id) fail(e)
    }
  }, 1500)
})
onBeforeUnmount(() => { eventsAbort?.abort(); clearInterval(refreshTimer); window.removeEventListener('auth:logout', authExpired) })
</script>

<template>
  <main v-if="!user" class="fusion-login">
    <div class="login-copy">
      <p class="login-brand"><span class="brand-mark" aria-hidden="true">营</span> 多Agent运营协同平台</p>
      <h1>从客服咨询到<br>可复用的内容</h1>
      <p>用商品资料回答咨询，让审定的售后答复进入 Wiki。</p>
    </div>
    <form class="login-panel" @submit.prevent="signIn"><h2>{{ signingUp ? '创建你的工作空间' : '登录运营工作台' }}</h2><p>咨询、任务与内容按账号隔离。</p>
      <label>用户名<input v-model="credentials.username" required minlength="3" maxlength="50" autocomplete="username" placeholder="至少 3 个字符"></label>
      <label>密码<input v-model="credentials.password" required minlength="6" type="password" :autocomplete="signingUp ? 'new-password' : 'current-password'" placeholder="至少 6 个字符"></label>
      <p v-if="error" role="alert" class="error">{{ error }}</p><button class="primary" :disabled="busy">{{ busy ? '处理中…' : signingUp ? '注册并进入' : '登录' }}</button>
      <button type="button" class="text-button" @click="signingUp = !signingUp">{{ signingUp ? '已有账号，返回登录' : '首次使用，创建账号' }}</button>
    </form>
  </main>
  <div v-else class="fusion-shell">
    <aside class="fusion-nav">
      <div class="brand"><span class="brand-mark" aria-hidden="true">营</span><strong>多Agent运营协同平台</strong></div>
      <nav aria-label="工作台">
        <button v-for="(title, key) in titles" :key="key" :class="{ active: page === key }" :aria-current="page === key ? 'page' : undefined" @click="page = key; error = ''">
          {{ title }}<span v-if="key === 'tasks'">{{ tasks.length }}</span><span v-if="key === 'assets'">{{ assets.length }}</span>
        </button>
      </nav>
      <div class="nav-foot"><p>{{ user.username }}</p><button class="text-button" @click="logout">退出登录</button></div>
    </aside>
    <main class="fusion-main">
      <header class="fusion-header">
        <h1>{{ titles[page] }}</h1>
        <span class="mode" :class="{ 'mode-live': knowledge?.mode === 'live' }">{{ statusText }}</span>
      </header>
      <p v-if="error" class="error global-error" role="alert">{{ error }}<button class="text-button" @click="error = ''">关闭</button></p>

      <div v-if="page === 'consultation'" class="consult-grid">
        <section class="surface conversation"><div class="section-head"><div><h2>商品与售后咨询</h2><p>回答可查看来源，有价值的问题可转为内容任务。</p></div><button class="secondary" :disabled="sending || busy" @click="run(newSession)">新会话</button></div>
          <div class="session-toolbar"><select aria-label="咨询会话" :value="session?.session_id" :disabled="sending || busy" @change="switchSession"><option v-for="item in sessions" :key="item.session_id" :value="item.session_id">{{ item.channel === 'voice' ? '语音咨询' : '文字咨询' }} · {{ item.last_question || '尚无提问' }}</option></select><button class="text-button" :disabled="busy" @click="run(lists)">刷新会话</button></div>
          <section v-if="verificationTarget" class="reuse-verification" aria-label="客服素材验证"><div class="section-head"><strong>正在验证：{{ verificationTarget.title }}</strong><button class="text-button" @click="verificationTarget = null">收起</button></div><p>{{ verificationTarget.sent ? '请查看新回答的来源与回答路径。' : '已新建文字会话并预填问题，点击“发送问题”后查看结果。' }}</p><p v-if="verificationTarget.kind === 'wiki'" class="support">已发布 Wiki 通过标准问题和审核过的别名匹配。</p></section>
          <div ref="transcript" class="transcript" aria-label="咨询记录">
          <div v-if="!session?.turns?.length && !sending" class="start-conversation">
            <h3>试问一个商品或售后问题</h3>
            <div class="examples"><button v-for="q in examples" :key="q" @click="prefill(q)">{{ q }} <span aria-hidden="true">↗</span></button></div>
          </div>
          <article v-for="turn in consultationTurns" :id="'turn-' + turn.turn_id" :key="turn.turn_id" class="turn" :class="{ 'linked-turn': highlightedTurn === turn.turn_id }" tabindex="-1"><p class="question"><span>你</span>{{ turn.question }}</p><div class="answer"><span>客服助手 <small v-if="answerRoutes[turn.answer_route]" class="answer-route">{{ answerRoutes[turn.answer_route] }}<template v-if="turn.answer_model_skipped"> · 跳过回答模型</template></small></span><p>{{ turn.answer }}</p>
            <button v-if="turn.wiki_asset_id" class="text-button" :disabled="busy" @click="showAsset(turn.wiki_asset_id)">查看本轮 Wiki 条目</button><small v-if="turn.reuseView.selected.length" class="positive">本次参考了已发布 FAQ 素材</small>
            <details v-if="!turn.answer_route || turn.answer_route === 'rag'" class="reuse-trace" :open="turn.reuseView.selected.length > 0"><summary>FAQ 选用说明 · {{ turn.reuseView.selected.length ? '已参考 ' + turn.reuseView.selected.length + ' 份素材' : turn.reuseView.status === 'unknown' ? '历史记录' : '本轮未选用素材' }}</summary><p>{{ turn.reuseView.message }}</p><div v-for="reference in turn.reuseView.selected" :key="reference.asset_id" class="reuse-reference"><strong>{{ reference.title || 'FAQ 素材' }}</strong><p v-if="reference.excerpt" class="reuse-excerpt">{{ reference.excerpt }}</p><p v-if="reference.match_reason" class="support">{{ reference.match_reason }}</p><button class="text-button" :disabled="busy" @click="showAsset(reference.asset_id)">查看这份素材</button></div><ul v-if="turn.reuseView.candidates.length" class="reuse-reasons"><li v-for="candidate in turn.reuseView.candidates" :key="candidate.asset_id"><strong>{{ candidate.title || 'FAQ 素材' }}</strong>：{{ candidate.reason }}</li></ul><p v-if="turn.reuseView.selected.length" class="support">以上是本轮回答时的参考记录，不代表逐字采用。素材撤回或失效后不会被新的咨询继续选用。</p></details>
            <details v-if="turn.sources?.length"><summary>{{ turn.sources.length }} 条资料依据</summary><div v-for="source in turn.sources" :key="source.chunk_id" class="source"><strong>{{ source.source_section || source.heading }}</strong><p>{{ source.content }}</p><a v-if="source.kind === 'wiki_evidence'" :href="source.source" target="_blank" rel="noopener noreferrer">核对资料出处 ↗</a></div></details>
            <button class="text-button link" @click="chooseTurn(turn)">转为内容任务 →</button></div></article>
          <article v-if="sending" class="turn" role="status">
            <p class="question"><span>你</span>{{ pendingQuestion }}</p>
            <div class="answer"><span>客服助手 · 正在回答</span><p>{{ liveAnswer || '正在查找资料…' }}</p></div>
          </article>
          </div>
          <form class="message-form" @submit.prevent="send"><textarea ref="composer" v-model="question" :disabled="!session" aria-label="咨询问题" placeholder="输入商品或售后问题，例如 SC-PB27 多重？" rows="2" maxlength="3000"></textarea><button class="primary" :disabled="!session || sending || !question.trim()">{{ !session ? '准备会话…' : sending ? '回答中…' : '发送问题' }}</button></form>
        </section>
        <aside class="consult-side"><section v-if="insightDraft" class="surface"><div class="section-head"><h2>转为内容任务</h2><button class="text-button" :disabled="busy" @click="insightDraft = null">取消</button></div><p class="support">核对问题摘要与商品范围，再创建任务。</p><label>问题摘要<textarea ref="insightSummary" v-model="insightDraft.summary" rows="5" maxlength="2000"></textarea></label><label>内容类型<select v-model="format" aria-label="内容类型"><option value="faq">简短 FAQ</option><option value="article">图文介绍</option><option v-if="wikiOption" value="wiki">售后 Wiki</option></select></label><template v-if="format !== 'wiki'"><label>商品型号<input v-model="productScope" placeholder="多个型号以逗号分隔"></label><label>面向谁<input v-model="audience"></label></template><p v-else class="support">资料不足也可建任务。核验手册或公告后，再审核适用范围与处理步骤。</p><label class="check"><input type="checkbox" v-model="insightDraft.confirmed">我已核对摘要并移除不必要的个人信息</label><button class="primary" :disabled="busy || !canCreateInsight(insightDraft)" @click="bridge">{{ busy ? '正在创建…' : '创建内容任务' }}</button></section>
          <section v-else class="surface workflow-panel"><h2>从咨询到知识沉淀</h2>
            <ol class="steps">
              <li><span>确认咨询摘要</span><small>在一轮问答下选择「转为内容任务」。</small></li>
              <li><span>整理内容并核对依据</span><small>创作 FAQ、图文，或整理成售后 Wiki。</small></li>
              <li><span>发布并复问</span><small>查看客服是否引用了原文或直接复用 Wiki。</small></li>
            </ol>
          </section>
          <section class="surface"><h2>实时语音</h2><p class="support">与文字客服共用商品知识。</p><button class="secondary" :disabled="busy" @click="voice">打开语音渠道</button></section>
        </aside>
      </div>

      <div v-else-if="page === 'tasks'" class="task-grid"><aside class="surface task-list"><div class="section-head"><h2>全部任务</h2><button class="text-button" @click="run(lists)">刷新</button></div><p v-if="!tasks.length" class="support">在客服试聊中选择问题，创建第一个任务。</p><button v-for="task in tasks" :key="task.task_id" :class="['task-item', { selected: current?.task_id === task.task_id }]" @click="run(() => openTask(task.task_id))"><strong>{{ task.topic_direction }}</strong><span>{{ typeNames[task.content_type] }} · {{ phaseNames[task.status] || task.status }}</span></button></aside>
        <section v-if="current" class="task-content"><div class="surface"><div class="section-head"><div><h2>{{ current.topic_direction }}</h2><p>{{ current.source.insight_id ? '来自已确认的咨询线索' : '直接创作' }} · {{ typeNames[current.content_type] }}</p></div><span class="badge muted">{{ phaseNames[current.status] || current.status }}</span></div>
          <button v-if="taskSource?.session_id && taskSource?.turn_id" class="text-button source-link" :disabled="busy || sending" @click="openSourceConsultation">查看来源咨询 ↗</button>
          <p v-if="['queued','running'].includes(current.status)" class="progress" role="status">{{ nodeNames[current.node] || '任务执行中，可离开页面后继续查看。' }}</p><p v-if="current.error" class="error">{{ current.error }}</p><button v-if="['failed','interrupted'].includes(current.status)" class="secondary" :disabled="busy" @click="action('retry')">核对后重试</button>
          <div v-if="current.interrupt_info?.action_required === 'select_topic' && current.status === 'waiting_human'" class="topics"><h3>请选择选题</h3><button v-for="topic in current.generated_topics" :key="topic" :disabled="busy" @click="action('select_topic', { selected_topic: topic })">{{ topic }} <span>选择 →</span></button></div></div>
          <section v-if="current.content_type === 'wiki'" class="surface">
            <button v-if="current.wiki_asset_id" class="secondary" :disabled="busy" @click="showAsset(current.wiki_asset_id)">查看最近审核版本</button>
            <SupportWikiEditor :key="current.task_id + ':' + current.state_version" :task="current" :busy="busy"
              :pages="relatedWikiPages" :active-snapshot="knowledge?.knowledge_snapshot_id" :suggest="draftWiki" @approve="approveWiki" />
          </section>
          <section v-if="current.article_content" class="surface"><div class="section-head"><h2>正文与版本</h2><select v-model="selectedVersion" aria-label="文章版本"><option value="">当前版本</option><option v-for="(v, i) in current.article_versions" :key="v.version_id" :value="v.version_id">版本 {{ i + 1 }} · {{ v.source === 'human' ? '人工反馈' : v.source === 'auto' ? '自动修订' : '首稿' }}</option></select></div><pre class="prose">{{ article.content }}</pre><details v-if="differences.length"><summary>与上一版比较</summary><pre class="diff"><span v-for="(line, index) in differences" :key="index" :class="line.type">{{ line.type === 'added' ? '+ ' : line.type === 'removed' ? '- ' : '  ' }}{{ line.text }}
</span></pre></details></section>
          <section v-if="current.article_content" class="surface"><div class="section-head"><h2>检查与审稿</h2><span :class="['badge', current.fact_check_report.passed ? 'good' : 'bad']">{{ current.fact_check_report.passed ? '关键事实检查通过' : '事实需要修改或核对' }}</span></div><p class="support">检查覆盖资料中的型号、数值、单位和明确政策条件；人工审核仍需核对表达。</p><ul class="issues" v-if="current.fact_check_report.issues?.length"><li v-for="(issue, i) in current.fact_check_report.issues" :key="i">{{ issue.sku }} {{ issue.reason }}</li></ul><details><summary>内容要求检查</summary><ul class="checks"><li v-for="(check, i) in current.quality_report.checks || []" :key="i">{{ check.passed ? '通过' : '待修改' }} · {{ check.criterion }} {{ check.issue }}</li></ul></details>
            <div v-if="current.interrupt_info?.action_required === 'review' && current.status === 'waiting_human'" class="review"><label>修改意见<textarea v-model="feedback" rows="3" placeholder="例如保留重量与尺寸，补充资料边界，避免额外性能承诺" maxlength="2000"></textarea></label><p v-if="article.historical" class="support">当前正在查看历史版本。切回当前版本后可提交审核。</p><div class="actions"><button class="secondary" :disabled="busy || article.historical || !feedback.trim()" @click="action('reject', { feedback })">携反馈改稿</button><button class="primary" :disabled="busy || article.historical || !current.fact_check_report.passed" @click="action('approve_text')">批准当前文本</button></div></div>
            <div v-if="current.status === 'text_approved'" class="approved"><p>文本已批准。FAQ 发布与配图单独执行。</p><div class="actions"><button class="primary" :disabled="busy" @click="openApprovedAsset">查看内容资产</button><button class="secondary" :disabled="busy" @click="action('generate_images')">{{ current.image_status === 'failed' || current.image_status === 'partial_failed' ? '整批重试配图' : '生成配图' }}</button></div></div>
            <div v-if="current.image_urls.length" class="gallery"><img v-for="(url, i) in current.image_urls" :key="url" :src="url" :alt="'配图 ' + (i + 1)"></div></section>
          <details class="surface"><summary>本任务的知识依据</summary><div v-for="source in current.evidence_refs || []" :key="source.chunk_id" class="source"><strong>{{ source.source_section || source.heading }}</strong><p>{{ source.content }}</p></div></details>
        </section><section v-else class="surface empty-task"><h2>选择一个内容任务</h2><p>查看选题、正文、事实检查和人工决策。</p></section>
      </div>

      <section v-else-if="page === 'assets'"><div class="section-head asset-intro"><div><p>审核通过的 Wiki、FAQ、图文与配图。</p></div><button class="secondary" :disabled="busy" @click="run(lists)">刷新资产</button></div><div v-if="!assets.length" class="surface"><h2>审核后，内容会保存在这里</h2><p>先从咨询创建任务，并批准一版正文。</p></div><article v-for="asset in assetRows" :id="'asset-' + asset.asset_id" :key="asset.asset_id" tabindex="-1" :class="['surface', 'asset', { 'asset-highlighted': highlightedAsset === asset.asset_id }]"><div class="section-head"><div><p class="asset-kind">{{ typeNames[asset.content_type] }}</p><h2>{{ asset.topic_direction || typeNames[asset.content_type] }}</h2></div><span :class="['badge', asset.wiki_eligible || asset.reuseView.eligible ? 'good' : 'muted']">{{ asset.wiki_eligible ? '可直接复用' : asset.reuseView.eligible ? '可参与咨询匹配' : assetNames[asset.publication_status] }}</span></div>
        <template v-if="asset.content_type === 'wiki'">
          <section class="wiki-customer-answer">
            <h3>标准答复</h3>
            <pre class="prose">{{ asset.wiki.answer }}</pre>
          </section>
          <section class="asset-reuse">
            <h3>标准问题</h3><p>{{ asset.wiki.question }}</p>
            <p v-if="asset.wiki.aliases.length" class="support">已审核别名 · {{ asset.wiki.aliases.join(' / ') }}</p>
            <details class="wiki-review-notes">
              <summary>适用范围与处理步骤</summary>
              <p>{{ asset.wiki.scope }}</p>
              <ol><li v-for="(step, index) in asset.wiki.steps" :key="index">{{ step }}</li></ol>
              <p><strong>后续处理</strong> · {{ asset.wiki.escalate_when }}</p>
            </details>
            <details><summary>核验资料</summary><div v-for="source in asset.evidence_refs" :key="source.chunk_id" class="source"><strong>{{ source.source_section }}</strong><p>{{ source.content }}</p><a v-if="source.kind === 'wiki_evidence'" :href="source.source" target="_blank" rel="noopener noreferrer">资料出处 ↗</a></div></details>
            <details v-if="asset.wiki.related_ids.length"><summary>关联专题</summary><button v-for="id in asset.wiki.related_ids" :key="id" class="text-button" @click="showAsset(id)">{{ assets.find(row => row.asset_id === id)?.topic_direction || '查看关联专题' }}</button></details>
          </section>
          <div class="actions">
            <button class="secondary" :disabled="busy" @click="run(() => openTask(asset.task_id))">查看来源任务 / 修订</button>
            <button v-if="asset.publication_status === 'draft'" class="primary" :disabled="busy || asset.knowledge_snapshot_id !== knowledge?.knowledge_snapshot_id" @click="publish(asset)">发布为售后 Wiki</button>
            <button v-if="asset.wiki_eligible" class="primary" :disabled="busy || sending" @click="verifyAsset(asset)">去客服验证</button>
            <button v-if="asset.publication_status === 'published'" class="secondary danger" :disabled="busy" @click="revoke(asset)">撤回 Wiki</button>
          </div>
        </template>
        <template v-else><pre class="prose">{{ asset.content }}</pre><div v-if="asset.image_urls?.length" class="gallery asset-gallery"><img v-for="(url, i) in asset.image_urls" :key="url" :src="url" :alt="'资产配图 ' + (i + 1)" loading="lazy"></div><section class="asset-reuse" aria-label="客服选用条件"><h3>{{ asset.content_type === 'faq' ? '客服选用条件' : '内容用途' }}</h3><p>{{ asset.reuseView.message }}</p><p v-if="asset.content_type === 'faq'" class="support">商品范围：{{ asset.product_scope?.length ? asset.product_scope.join('、') : '通用政策' }}<span v-if="asset.reuseView.intent_labels.length"> · 问题主题：{{ asset.reuseView.intent_labels.join('、') }}</span></p><p v-if="asset.reuseView.question" class="sample-question">验证问题：{{ asset.reuseView.question }}</p><p v-if="asset.reuseView.canVerify" class="support">验证会新建文字会话并预填问题，不会自动发送。</p></section><div class="actions"><button v-if="asset.content_type === 'faq'" class="secondary" :disabled="busy || sending || !asset.reuseView.canVerify" @click="verifyAsset(asset)">去客服验证</button><button class="secondary" @click="run(() => openTask(asset.task_id))">查看来源任务</button><button v-if="asset.content_type === 'faq'" class="primary" :disabled="busy || !canPublishAsset(asset, knowledge?.knowledge_snapshot_id)" @click="publish(asset)">发布为客服素材</button><button v-if="asset.publication_status === 'published'" class="secondary danger" :disabled="busy" @click="revoke(asset)">撤回素材</button></div></template></article></section>
      <section v-else class="legacy-host"><LegacyCreator /></section>
    </main>
  </div>
</template>

<style scoped>
.fusion-shell, .fusion-login {
  --ink: #172b27;
  --muted-ink: #536760;
  --line: #dce6e0;
  --surface: #fff;
  --accent: #176244;
  --accent-dark: #104b34;
  --tint: #edf5ef;
  color: var(--ink);
  font-family: 'Segoe UI', 'Microsoft YaHei', sans-serif;
}

.fusion-login {
  min-height: 100svh;
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 410px);
  align-items: center;
  gap: clamp(30px, 8vw, 125px);
  padding: 64px max(32px, calc((100vw - 1100px) / 2));
  background: radial-gradient(ellipse at 15% 45%, #e4f1e8, transparent 67%), #f7f9f7;
}
.login-copy h1 { margin: 24px 0 16px; font-size: clamp(34px, 4.6vw, 62px); line-height: 1.25; letter-spacing: -.04em; }
.login-copy > p:not(.login-brand) { max-width: 34ch; color: var(--muted-ink); font-size: 16px; line-height: 1.85; }
.login-brand, .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; }
.brand-mark { display: inline-grid; width: 32px; height: 32px; flex: 0 0 32px; place-items: center; border-radius: 9px; background: var(--accent); color: #fff; font-size: 14px; }
.login-panel { padding: 32px; border: 1px solid var(--line); border-radius: 18px; background: var(--surface); box-shadow: 0 18px 46px #18392d0c; }
.login-panel h2 { font-size: 22px; }
.login-panel > p { margin: 4px 0 18px; color: var(--muted-ink); font-size: 13px; }
.login-panel > .primary { width: 100%; margin-top: 8px; }
.login-panel > .text-button { display: block; margin: 12px auto 0; }

.fusion-shell { display: grid; grid-template-columns: 216px minmax(0, 1fr); min-height: 100svh; background: #f4f7f5; }
.fusion-nav {
  display: flex;
  flex-direction: column;
  gap: 27px;
  position: sticky;
  top: 0;
  height: 100svh;
  background: var(--surface);
  padding: 26px 15px;
  border-right: 1px solid var(--line);
}
.brand { padding: 0 7px; font-size: 15px; line-height: 1.35; }
.fusion-nav nav { display: grid; gap: 5px; }
.fusion-nav nav button {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  text-align: left;
  padding: 10px 14px;
  border-radius: 8px;
  color: var(--muted-ink);
  font-size: 14px;
}
.fusion-nav nav button:enabled:hover, .fusion-nav nav .active { background: var(--tint); color: var(--accent-dark); }
.fusion-nav nav .active { font-weight: 700; }
.fusion-nav nav span { font-size: 11px; font-variant-numeric: tabular-nums; }
.nav-foot { margin-top: auto; padding: 14px 8px 0; border-top: 1px solid var(--line); font-size: 13px; }
.nav-foot p { overflow-wrap: anywhere; }

.fusion-main { min-width: 0; width: 100%; max-width: 1480px; margin-inline: auto; padding: 27px clamp(18px, 3vw, 48px) 38px; }
.fusion-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 20px; }
.fusion-header h1 { font-size: clamp(25px, 2.4vw, 33px); letter-spacing: -.035em; line-height: 1.35; }
.mode {
  display: flex;
  align-items: center;
  gap: 7px;
  border: 1px solid var(--line);
  border-radius: 20px;
  padding: 5px 10px;
  background: #fff;
  color: var(--muted-ink);
  white-space: nowrap;
  font-size: 12px;
}
.mode::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #8a9c95; }
.mode-live::before { background: #26915d; }

.fusion-login button, .fusion-shell button { font: inherit; cursor: pointer; }
.fusion-login button:disabled, .fusion-shell button:disabled { opacity: .55; cursor: not-allowed; }
.primary, .secondary, .text-button, .examples button, .topics button, .task-item, .fusion-nav nav button {
  border: 0;
  background: transparent;
  color: inherit;
}
.fusion-shell :is(.primary, .secondary), .fusion-login .primary { padding: 9px 13px; border-radius: 8px; font-size: 13px; font-weight: 650; }
.fusion-shell .primary, .fusion-login .primary { color: #fff; background: var(--accent); border: 1px solid var(--accent); }
.primary:enabled:hover { background: var(--accent-dark); }
.fusion-shell .secondary { border: 1px solid #c9d8d0; background: #fff; }
.secondary:enabled:hover { background: var(--tint); }
.fusion-shell .text-button, .fusion-login .text-button { color: var(--accent); padding: 4px; font-size: 13px; border-radius: 4px; }
.text-button:enabled:hover { text-decoration: underline; }
.fusion-shell .danger { color: #9b4a40; }
.fusion-shell :focus-visible, .fusion-login :focus-visible { outline: 2px solid #2b8a64; outline-offset: 3px; }
.fusion-login label, .surface label { display: block; margin: 13px 0; font-size: 13px; font-weight: 600; }
.login-panel input, .surface :is(input:not([type=checkbox]), textarea, select) {
  color: var(--ink);
  background: #fff;
  border: 1px solid #c9d8d0;
  border-radius: 7px;
  padding: 8px 10px;
  font: inherit;
  font-size: 13px;
  width: 100%;
  margin-top: 5px;
}
.surface textarea { display: block; resize: vertical; }
.surface :is(textarea, input)::placeholder { color: #63786f; }
.surface .check { display: flex; align-items: flex-start; gap: 9px; font-weight: 400; }
.check input { margin-top: 4px; accent-color: var(--accent); }

.consult-grid, .task-grid { display: grid; gap: 18px; align-items: start; }
.consult-grid { grid-template-columns: minmax(0, 1fr) minmax(240px, 300px); }
.task-grid { grid-template-columns: minmax(190px, 248px) minmax(0, 1fr); }
.consult-grid > *, .task-grid > *, .section-head > *, .answer, .reuse-reference { min-width: 0; }
.surface { margin-bottom: 16px; padding: 22px; border: 1px solid var(--line); border-radius: 13px; background: var(--surface); }
.surface h2 { font-size: 17px; line-height: 1.42; overflow-wrap: anywhere; }
.surface p { line-height: 1.72; }
.section-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.section-head > button, .section-head > .badge { flex-shrink: 0; }
.section-head > select { width: auto; max-width: 42%; margin: 0; }
.section-head p, .support { margin-top: 5px; color: var(--muted-ink); font-size: 13px; }
.badge { display: inline-block; border-radius: 6px; padding: 4px 8px; font-size: 11px; white-space: nowrap; }
.muted { background: #eff3f1; color: #4c6158; }
.good { background: #e5f4eb; color: #145a3d; }
.bad { background: #f9ede7; color: #8c4133; }
.error { padding: 11px 13px; border: 1px solid #f0d8d0; border-radius: 8px; background: #fbefe9; color: #913c2f; font-size: 13px; white-space: pre-wrap; overflow-wrap: anywhere; }
.global-error { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 16px; }
.login-panel .error { color: #913c2f; }

.conversation { display: flex; flex-direction: column; }
.session-toolbar { display: flex; align-items: center; gap: 9px; margin-top: 16px; }
.session-toolbar select { flex: 1; width: 0; min-width: 0; margin: 0; text-overflow: ellipsis; }
.session-toolbar button { flex-shrink: 0; }
.transcript { min-height: 285px; max-height: clamp(300px, calc(100svh - 345px), 720px); overflow-y: auto; margin-top: 16px; padding: 0 9px 4px 3px; scrollbar-color: #b9cbc0 transparent; }
.start-conversation { display: grid; justify-items: center; align-content: center; min-height: 285px; padding: 22px 10px; }
.start-conversation h3 { font-size: 17px; }
.examples { display: grid; gap: 8px; width: min(100%, 420px); margin-top: 16px; }
.examples button { display: flex; justify-content: space-between; gap: 8px; padding: 8px 12px; border: 1px solid var(--line); border-radius: 7px; color: var(--accent-dark); text-align: left; font-size: 13px; }
.examples button:hover { background: var(--tint); }
.turn { padding: 12px 0; scroll-margin: 9px; }
.turn.linked-turn { background: var(--tint); border-radius: 9px; outline: 2px solid #72a990; outline-offset: -2px; padding-inline: 8px; }
.question { width: fit-content; max-width: 88%; margin-left: auto; padding: 9px 13px; background: #e8f3ec; border-radius: 11px 11px 3px 11px; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 14px; }
.question span, .answer > span { display: block; color: var(--muted-ink); margin-bottom: 4px; font-size: 11px; font-weight: 650; }
.answer { padding: 12px 2px 4px; font-size: 14px; }
.answer > p { white-space: pre-wrap; overflow-wrap: anywhere; }
.answer-route { margin-left: 7px; color: var(--accent); font-size: 11px; }
.positive { display: block; margin-top: 9px; color: var(--accent); font-size: 12px; }
details { margin-top: 11px; font-size: 13px; }
summary { cursor: pointer; color: var(--accent-dark); }
.source { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--line); font-size: 13px; }
.source p { margin-top: 4px; color: var(--muted-ink); white-space: pre-wrap; overflow-wrap: anywhere; }
.link, .source-link { margin-top: 7px; }
.message-form { display: flex; align-items: stretch; gap: 8px; padding-top: 12px; margin-top: 10px; border-top: 1px solid var(--line); }
.message-form textarea { min-width: 0; min-height: 48px; max-height: 190px; margin: 0; }
.message-form button { flex-shrink: 0; }

.steps { list-style: none; margin-top: 15px; }
.steps li { position: relative; padding: 0 0 12px 18px; font-size: 13px; }
.steps li + li { border-top: 1px solid var(--line); padding-top: 12px; }
.steps li::before { position: absolute; content: ''; left: 0; top: 7px; width: 7px; height: 7px; border-radius: 50%; background: #4f9875; }
.steps li + li::before { top: 19px; }
.steps li:last-child { padding-bottom: 0; }
.steps span { display: block; font-weight: 650; }
.steps small { display: block; margin-top: 2px; color: var(--muted-ink); font-size: 12px; }
.consult-side > .surface > .primary { width: 100%; margin-top: 6px; }
.consult-side .secondary { margin-top: 12px; }

.task-list { padding: 14px 12px; }
.task-list > .section-head { padding: 0 5px 10px; }
.task-list > .support { margin-inline: 5px; }
.task-item { width: 100%; text-align: left; padding: 10px; border-top: 1px solid var(--line); border-radius: 7px; }
.task-item strong { display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; overflow: hidden; overflow-wrap: anywhere; font-size: 13px; }
.task-item span { display: block; margin-top: 2px; color: var(--muted-ink); font-size: 11px; }
.task-item.selected, .task-item:hover { background: var(--tint); }
.empty-task { min-height: 180px; }
.empty-task p { margin-top: 8px; color: var(--muted-ink); font-size: 13px; }
.prose { margin: 16px 0; color: var(--ink); font: 14px/1.8 'Segoe UI', 'Microsoft YaHei', sans-serif; white-space: pre-wrap; overflow-wrap: anywhere; }
.progress { color: var(--accent); font-size: 13px; margin-top: 12px; }
.topics { display: grid; gap: 8px; margin-top: 16px; }
.topics h3 { font-size: 14px; }
.topics button { display: flex; justify-content: space-between; gap: 8px; padding: 8px 11px; border: 1px solid var(--line); border-radius: 8px; font-size: 13px; text-align: left; }
.topics button:hover { background: var(--tint); }
.topics span { white-space: nowrap; color: var(--accent); }
.issues { margin: 10px 0; padding: 9px 12px 9px 28px; background: #fbefe9; color: #8c4133; font-size: 13px; }
.checks { padding: 6px 18px; font-size: 13px; line-height: 1.8; }
.review, .approved { margin-top: 16px; padding-top: 8px; border-top: 1px solid var(--line); }
.actions { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.diff { margin-top: 9px; font: 13px/1.75 'Segoe UI', 'Microsoft YaHei', sans-serif; white-space: pre-wrap; overflow-wrap: anywhere; }
.diff > span { display: block; }
.diff .added { background: #e5f4eb; }
.diff .removed { background: #f9ede7; }
.gallery { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; margin-top: 16px; }
.gallery img { display: block; width: 100%; max-width: 260px; border-radius: 8px; }
.asset-intro { margin-bottom: 16px; color: var(--muted-ink); font-size: 13px; }
.asset { max-width: 1020px; scroll-margin: 14px; }
.section-head .asset-kind { margin: 0 0 3px; color: var(--accent); font-size: 12px; font-weight: 650; }
.asset-gallery { margin: 0 0 14px; }
.asset-highlighted { outline: 2px solid #438c69; outline-offset: 2px; }
.reuse-verification, .asset-reuse, .reuse-trace { padding: 10px 12px; border-radius: 8px; background: var(--tint); overflow-wrap: anywhere; font-size: 13px; }
.reuse-verification, .reuse-reference { margin-top: 12px; }
.reuse-verification p, .reuse-trace > p, .sample-question { margin-top: 5px; }
.reuse-reference { padding-top: 8px; border-top: 1px solid var(--line); }
.reuse-excerpt { white-space: pre-wrap; margin-top: 4px; }
.reuse-reasons { padding-left: 16px; margin-top: 8px; color: var(--muted-ink); }
.asset-reuse h3 { font-size: 13px; }
.wiki-customer-answer { margin-top: 14px; }
.wiki-customer-answer h3 { font-size: 13px; color: var(--accent-dark); }
.wiki-customer-answer .prose { margin-top: 5px; }
.wiki-review-notes > p, .wiki-review-notes ol { margin-top: 8px; }
.wiki-review-notes ol { padding-inline-start: 21px; }
.legacy-host { min-width: 0; position: relative; }
.legacy-host :deep(.app-layout) { height: calc(100vh - 140px); min-height: 650px; }
.legacy-host :deep(.sidebar) { position: relative; }
.legacy-host :deep(.main-content) { min-width: 0; }

@media (max-width: 1050px) {
  .fusion-shell { grid-template-columns: 182px minmax(0, 1fr); }
  .consult-grid { grid-template-columns: minmax(0, 1fr) 230px; }
  .surface { padding: 17px; }
}
@media (max-width: 850px) {
  .fusion-login { grid-template-columns: 1fr; gap: 26px; align-content: center; padding: 30px max(20px, calc((100vw - 510px) / 2)); }
  .login-copy h1 { margin: 14px 0 8px; }
  .fusion-shell { grid-template-columns: 1fr; }
  .fusion-nav { z-index: 3; height: auto; flex-direction: row; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 12px; border: 0; border-bottom: 1px solid var(--line); }
  .fusion-nav nav { display: flex; flex: 1; justify-content: flex-end; }
  .fusion-nav nav button { padding: 7px; font-size: 13px; white-space: nowrap; }
  .fusion-nav nav span, .nav-foot p { display: none; }
  .nav-foot { flex-shrink: 0; margin: 0; padding: 0; border: 0; }
  .fusion-main { padding: 16px 13px 28px; }
  .consult-grid, .task-grid { grid-template-columns: 1fr; gap: 0; }
  .consult-side { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .task-list { max-height: 210px; overflow: auto; }
  .asset { scroll-margin-top: 58px; }
}
@media (max-width: 530px) {
  .brand { display: none; }
  .fusion-nav nav { justify-content: flex-start; gap: 2px; }
  .fusion-header { margin-bottom: 13px; }
  .fusion-header h1 { font-size: 23px; }
  .surface, .login-panel { padding: 14px; }
  .consult-side { grid-template-columns: 1fr; gap: 0; }
  .message-form { align-items: stretch; flex-direction: column; }
  .asset .section-head, .asset-intro { flex-wrap: wrap; }
}
</style>
