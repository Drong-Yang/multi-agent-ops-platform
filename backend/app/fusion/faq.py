"""Conservative, observable reuse of approved expressions, never new facts."""
import re

from .intents import intent_tags
from .privacy import sanitize

LABELS = {'weight': '重量', 'dimensions': '尺寸', 'capacity': '容量', 'warranty': '保修',
          'returns': '退货售后', 'price': '价格', 'portability': '携带使用',
          'air_travel': '航空携带', 'charging': '充电', 'compatibility': '兼容性', 'comparison': '型号比较'}
QUESTIONS = {'weight': '多重？', 'dimensions': '尺寸是多少？', 'capacity': '容量是多少？',
             'warranty': '怎么保修？', 'returns': '退货需要满足什么条件？', 'price': '多少钱？',
             'portability': '适合外出携带吗？', 'air_travel': '携带登机要核对什么？',
             'charging': '充电有哪些注意事项？', 'compatibility': '兼容哪些设备？', 'comparison': '有哪些区别？'}


def asset_tags(payload):
    # Derive from the actually approved content, including old empty-tag records.
    return sorted(set(intent_tags(payload.get('content', ''))) | set(payload.get('intent_tags') or []))


def title(payload):
    return sanitize(re.sub(r'[#*`>]', '', (payload.get('content') or '').strip().split('\n')[0]))[:90]


def sample_question(payload):
    scope = ' 和 '.join(payload.get('product_scope') or [])
    tags = asset_tags(payload)
    if tags:
        phrase = QUESTIONS.get(tags[0], '请介绍相关信息。')
        if scope and tags[0] in {'weight', 'dimensions', 'capacity', 'price', 'comparison', 'charging'}:
            # A pure specification question now quotes the catalog. Include a
            # genuine support question when demonstrating the FAQ context path.
            phrase = phrase.rstrip('？') + '，并说明保修条件？'
        return (scope + ' ' + phrase).strip()
    direction = payload.get('source_question') or payload.get('topic_direction') or title(payload)
    if scope and not any(sku in direction for sku in payload.get('product_scope', [])):
        direction = scope + ' ' + direction
    return sanitize(direction)[:300]


def describe_asset(record, snapshot_id, *, eligible):
    p = record['payload']
    status, message = 'ready', '已发布为客服素材；后续相关问题匹配后可选用。'
    if p.get('content_type') != 'faq':
        status, message = 'not_faq', '图文内容用于内容发布；需要另建简短 FAQ 才能作为客服素材。'
    elif p.get('publication_status') == 'revoked':
        status, message = 'revoked', '已撤回，后续问答不再选用。'
    elif p.get('publication_status') == 'stale' or p.get('knowledge_snapshot_id') != snapshot_id:
        status, message = 'stale', '知识版本已变化，请依据当前资料重新创作、审核。'
    elif p.get('publication_status') != 'published':
        status, message = 'draft', '尚未发布为客服素材。'
    elif not eligible:
        status, message = 'invalid_approval', '当前正文、审批或来源任务已变化，暂不可选用。'
    tags = asset_tags(p)
    return {'eligible': bool(eligible and status == 'ready'), 'status': status, 'message': message,
            'intent_tags': tags, 'intent_labels': [LABELS.get(tag, tag) for tag in tags],
            'sample_question': sample_question(p)}


def _refs(items):
    return {r.get('chunk_id', r.get('id')) for r in items if isinstance(r, dict) and (r.get('chunk_id') or r.get('id'))}


def _words(text):
    text = re.sub(r'\b[A-Z]{1,5}[-_][A-Z0-9-]+\b', '', text, flags=re.I).lower()
    stop = {'什么', '多少', '请问', '一下', '介绍', '这个', '这款', '商品', '产品', '参数', '资料', '问题', '关于', '如何', '怎么', '可以', '是否', '哪些'}
    return {part[i:i+2] for part in re.findall(r'[\u4e00-\u9fff]+', text) for i in range(len(part)-1)} - stop


def _related(message, query, payload):
    # Current question wins over the preceding turn's intent (e.g. dimensions vs weight).
    asked = set(intent_tags(message) or intent_tags(query))
    supported = set(asset_tags(payload))
    shared = asked & supported
    if shared:
        return '主题匹配：' + '、'.join(LABELS.get(tag, tag) for tag in sorted(shared))
    if asked and supported:
        return ''
    words = _words(message)
    reference = _words(' '.join([payload.get('source_question', ''), payload.get('topic_direction', ''), payload.get('content', '')]))
    overlap = words & reference
    if len(overlap) >= 2 and len(overlap) / max(1, len(words)) >= .5:
        return '问题与已审素材的内容相关'
    return ''


async def hydrate_asset_topic(runtime, record, owner):
    # Old assets did not store their direction; read it without migrating user data.
    from .repository import NotFound
    payload = record['payload']
    if payload.get('topic_direction') or not payload.get('task_id'):
        return record
    try:
        task = await runtime.db('get', payload['task_id'], owner, 'task')
    except NotFound:
        return record
    return {**record, 'payload': {**payload, 'topic_direction': task['payload'].get('topic_direction', '')}}


async def select_faq_assets(runtime, owner, message, query, result, *, candidates=None):
    trace = {'status': 'no_available', 'message': '尚无当前账号可选的已发布 FAQ；本轮使用基础资料回答。',
             'selected_assets': [], 'candidates': []}
    if not result.get('sources'):
        trace.update(status='no_evidence', message='本轮未找到基础资料，FAQ 不作为补充未知商品事实的依据。')
        return [], trace
    if candidates is None:
        candidates = await runtime.db('valid_faq', owner)
    if not candidates:
        return [], trace
    scope = {s['sku'].upper() for s in result['sources'] if s.get('sku')}
    rules = _refs([s for s in result['sources'] if s.get('kind') == 'rule'])
    matches = []
    for record in candidates:
        record = await hydrate_asset_topic(runtime, record, owner)
        p = record['payload']
        code, reason = 'topic_mismatch', '本轮问题与这份素材的主题不匹配。'
        asset_scope = {s.upper() for s in p.get('product_scope', [])}
        refs = p.get('evidence_refs') or []
        if p.get('knowledge_snapshot_id') != result['knowledge_snapshot_id']:
            code, reason = 'knowledge_mismatch', '素材与本轮检索的知识版本不同。'
        elif asset_scope and not asset_scope.issubset(scope):
            code, reason = 'product_scope_mismatch', '本轮未确认素材涉及的全部商品型号。'
        elif not asset_scope and not rules.intersection(_refs(refs)):
            code, reason = 'policy_evidence_mismatch', '通用素材与本轮政策依据不匹配。'
        else:
            try:
                if not refs:
                    raise ValueError('empty evidence')
                runtime.knowledge.fact_bundle(result['knowledge_snapshot_id'], refs)
            except ValueError:
                code, reason = 'evidence_invalid', '素材引用的基础资料无法在当前知识版本核验。'
            else:
                related = _related(message, query, p)
                if related:
                    code, reason = 'matched', related
                    matches.append((record, reason))
        trace['candidates'].append({'asset_id': record['id'], 'title': title(p), 'code': code, 'reason': reason})
    matches.sort(key=lambda pair: (pair[0]['payload'].get('published_at', ''), pair[0].get('updated_at', ''), pair[0]['id']), reverse=True)
    selected = [record for record, _ in matches[:2]]
    for record, reason in matches[:2]:
        trace['selected_assets'].append({'asset_id': record['id'], 'title': title(record['payload']),
            'excerpt': sanitize(record['payload'].get('content', ''))[:500], 'match_reason': reason})
    picked = {r['id'] for r in selected}
    for candidate in trace['candidates']:
        if candidate['code'] == 'matched' and candidate['asset_id'] not in picked:
            candidate.update(code='limit', reason='主题匹配，本轮优先选用最近发布的两份相关素材。')
    trace.update(status='selected' if selected else 'no_match', message=(
        f'本轮选用 {len(selected)} 份已审 FAQ 作为回答参考；商品事实仍以基础资料为准。'
        if selected else '已有可用 FAQ，但本轮商品或主题未匹配；本轮使用基础资料回答。'))
    return selected, trace
