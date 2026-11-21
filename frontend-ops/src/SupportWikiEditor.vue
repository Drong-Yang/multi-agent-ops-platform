<script setup>
import { computed, onMounted, reactive, ref } from 'vue'

const props = defineProps({
  task: { type: Object, required: true },
  busy: Boolean,
  suggest: { type: Function, required: true },
  activeSnapshot: String,
  pages: { type: Array, default: () => [] }
})
const emit = defineEmits(['approve'])
const pending = props.task.wiki_draft?.fields
const version = pending || props.task.wiki || {}
const availableEvidence = computed(() => props.task.knowledge_snapshot_id === props.activeSnapshot ? (props.task.evidence_refs || []) : [])
const generating = ref(false)
const draft = reactive({
  title: version.title ?? props.task.topic_direction.slice(0, 120),
  question: version.question ?? props.task.source_question,
  aliases: version.aliases?.join('\n') ?? '',
  scope: version.scope ?? '',
  steps: version.steps?.join('\n') ?? '',
  answer: version.answer ?? '',
  escalate: version.escalate_when ?? '',
  evidence: [...(version.evidence_ids ?? version.snapshot_evidence_ids ?? availableEvidence.value.slice(0, 8).map(source => source.chunk_id))],
  related: [...(version.related_ids || [])],
  titleSource: version.supplement?.title ?? '',
  urlSource: version.supplement?.url ?? '',
  excerpt: version.supplement?.excerpt ?? ''
})
const lines = value => value.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
const canUseOriginal = computed(() => ['rag', undefined, null].includes(props.task.source_answer_route) && !!props.task.source_answer)
const hasSupplement = computed(() => Boolean(draft.titleSource.trim() || draft.urlSource.trim() || draft.excerpt.trim()))
const canDraft = computed(() => hasSupplement.value
  ? Boolean(draft.titleSource.trim() && draft.urlSource.trim() && draft.excerpt.trim())
  : availableEvidence.value.some(e => draft.evidence.includes(e.chunk_id)))
const canApprove = computed(() => canDraft.value && [draft.title, draft.question, draft.scope, draft.steps,
  draft.answer, draft.escalate].every(value => value?.trim()))
const relatedIds = () => draft.related.filter(id => props.pages.some(page => page.asset_id === id))
function evidenceFields() {
  return {
    evidence_ids: draft.evidence.filter(id => availableEvidence.value.some(e => e.chunk_id === id)),
    supplement: hasSupplement.value ? { title: draft.titleSource, url: draft.urlSource, excerpt: draft.excerpt } : null,
    related_ids: relatedIds()
  }
}
async function suggest() {
  if (generating.value || !canDraft.value) return
  generating.value = true
  try {
    const result = await props.suggest(evidenceFields())
    if (!result) return
    Object.assign(draft, { title: result.title, question: result.question, aliases: result.aliases.join('\n'),
      scope: result.scope, answer: result.answer, steps: result.steps.join('\n'), escalate: result.escalate_when })
  } finally {
    generating.value = false
  }
}

onMounted(() => {
  if (!pending && !props.task.wiki && canDraft.value) void suggest()
})
function approve() {
  emit('approve', {
    expected_state_version: props.task.state_version,
    title: draft.title, question: draft.question, aliases: lines(draft.aliases), scope: draft.scope,
    steps: lines(draft.steps), answer: draft.answer, escalate_when: draft.escalate, confirmed: true,
    ...evidenceFields()
  })
}
</script>

<template>
  <form @submit.prevent="approve">
    <h2>{{ task.wiki ? '修订售后 Wiki' : '核对并整理售后 Wiki' }}</h2>
    <p v-if="generating" class="support" role="status">正在根据已选资料填写 Wiki 草稿…</p>
    <p v-else-if="pending" class="support" role="status">Wiki 草稿已预填，可核对或修改后批准。</p>
    <p v-else class="support">选取原始资料或补充证据即可预填草稿。审核后到内容资产发布。</p>
    <fieldset :disabled="busy || generating">
      <label>条目标题<input v-model="draft.title" required minlength="2" maxlength="120"></label>
      <label>标准问题<input v-model="draft.question" required minlength="4" maxlength="200"></label>
      <label>经审核的同义问法<textarea v-model="draft.aliases" rows="2" placeholder="选填，每行一条。保持适用型号与含义一致。"></textarea></label>
      <label>适用范围<textarea v-model="draft.scope" required minlength="2" maxlength="400" rows="2" placeholder="说明适用型号、症状、版本或平台条件"></textarea></label>
      <label>标准答复<textarea v-model="draft.answer" required minlength="2" maxlength="800" rows="3"></textarea></label>
      <p class="support">命中这篇 Wiki 时，客服会原样发送批准后的标准答复。适用范围、步骤和后续处理可在内容资产中展开查看。</p>
      <button v-if="canUseOriginal" type="button" class="text-button" @click="draft.answer = task.source_answer">采用原回答作为初稿</button>
      <label>有序处理步骤<textarea v-model="draft.steps" required rows="4" placeholder="每行一步，按执行顺序填写"></textarea></label>
      <label>后续处理<textarea v-model="draft.escalate" required minlength="2" maxlength="250" rows="2" placeholder="说明哪些情况需要自行联系官方售后"></textarea></label>

      <details v-if="availableEvidence.length" open>
        <summary>选用本轮原始资料</summary>
        <label v-for="source in availableEvidence" :key="source.chunk_id" class="check evidence">
          <input v-model="draft.evidence" type="checkbox" :value="source.chunk_id">
          <span><strong>{{ source.source_section }}</strong><small>{{ source.content }}</small></span>
        </label>
      </details>
      <details :open="!availableEvidence.length || hasSupplement">
        <summary>补充可核验资料</summary>
        <p class="support">可填写官方手册、公告或经核实的内部文档出处。请核对摘录支持上面的答复及步骤。</p>
        <label>资料名称<input v-model="draft.titleSource" :required="hasSupplement" minlength="2" maxlength="150"></label>
        <label>HTTPS 来源地址<input v-model="draft.urlSource" :required="hasSupplement" type="url"></label>
        <label>原始资料摘录<textarea v-model="draft.excerpt" :required="hasSupplement" minlength="10" maxlength="3000" rows="3"></textarea></label>
      </details>
      <button type="button" class="secondary" :disabled="!canDraft" @click="suggest">用所选资料生成 Wiki 草稿</button>
      <details v-if="pages.length">
        <summary>关联其他已发布的售后专题</summary>
        <label v-for="item in pages" :key="item.asset_id" class="check">
          <input v-model="draft.related" type="checkbox" :value="item.asset_id">{{ item.wiki.title }}
        </label>
      </details>
    </fieldset>
    <p class="support" id="wiki-approval-note">点击批准即确认已核对资料出处、适用条件、处理步骤和答复。</p>
    <button class="primary" :disabled="busy || generating || !canApprove" aria-describedby="wiki-approval-note">{{ task.wiki ? '批准新版本' : '批准 Wiki 草稿' }}</button>
  </form>
</template>

<style scoped>
form { margin-top: 14px; }
.support { color: var(--muted-ink); font-size: 13px; margin: 5px 0 12px; }
fieldset { border: 0; min-width: 0; margin: 0 0 14px; }
label { display: block; margin: 11px 0; font-size: 13px; font-weight: 600; }
label > :is(input:not([type=checkbox]), textarea) {
  display: block;
  width: 100%;
  margin-top: 5px;
  padding: 8px 10px;
  background: #fff;
  border: 1px solid #c9d8d0;
  border-radius: 7px;
  color: var(--ink);
  font: inherit;
  font-weight: 400;
}
textarea { resize: vertical; }
.check { display: flex; align-items: flex-start; gap: 8px; font-weight: 400; }
.check input { margin-top: 4px; accent-color: var(--accent); }
details { margin: 12px 0; padding: 9px 12px; border: 1px solid var(--line); border-radius: 8px; }
summary { cursor: pointer; color: var(--accent-dark); font-size: 13px; }
button { cursor: pointer; font: inherit; font-size: 13px; }
button:disabled { opacity: .55; cursor: not-allowed; }
.primary, .secondary { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--accent); }
.primary { color: #fff; background: var(--accent); }
.secondary, .text-button { color: var(--accent-dark); background: #fff; }
.text-button { border: 0; padding: 3px 0; text-decoration: underline; }
:focus-visible { outline: 2px solid #2b8a64; outline-offset: 3px; }
.evidence small { display: block; margin-top: 4px; color: var(--muted-ink); white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
