"""Interpret contextual turns from a bounded, session-local conversation window."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
import re
import unicodedata
from typing import Literal

from .legacy_rag.commerce import normalize
from .privacy import sanitize
from .support_answers import (
    broad_earphone_recommendation, combined_return_question, return_eligibility_question,
    return_freight_question, return_status_reply,
)


Intent = Literal['question', 'reason', 'clarification', 'alternatives', 'product_followup', 'followup',
                 'ambiguous', 'preferences', 'fit_check', 'policy_followup']
SemanticIntent = Literal['reason', 'clarification', 'alternatives', 'new_question']
DIALOGUE_HISTORY_LIMIT = 6
_FILLER_PREFIX = re.compile(r'^(?:嗯+|呃+|额+|唔+|诶+|啊+|请问)+')
_ACKNOWLEDGEMENT = re.compile(r'(?:你好|您好|哈喽|哦+|好(?:的|吧)?|行(?:吧)?|对|明白了?|懂了|知道了|谢谢(?:你|了)?|有道理)+')
_MODEL_MENTION = re.compile(r'(?<![a-z0-9])(?:sc|ew)[ -]*[a-z]+[ -]*\d+[a-z]*', re.I)
_TOPIC_SWITCH = re.compile(r'^(?:(?:那么|那)?(?:换个话题|换一个话题|另一个问题|说回|回到)|另外我(?:想问|想买)|我还想问)')
_REFOCUS_PREFIX = re.compile(
    r'^(?:不是(?:这个意思|这个)?|我(?:问的是|说的是|想问的是|想知道的是|是(?:想)?(?:问|说))|我的意思是)+'
)
_PRODUCT_TOPICS = r'充电宝|移动电源|电池|充电器|耳机|音箱|手表|手机|平板|电脑|键盘|鼠标|扩展坞|硬盘|数据线'
_PRODUCT_TOPIC = re.compile(_PRODUCT_TOPICS)
_NAMED_TOPIC = re.compile(rf'{_PRODUCT_TOPICS}|发票|开票|物流|配送|退货|退款|运费|订单|售后|维修')
_PURCHASE_CONTEXT = re.compile(
    rf'买[了的]|购买[了的]|已购买|(?:刚|已经|已)买|下单[了的]|到货[了的]|收到[了的]|已签收|'
    rf'我(?:买过|手头有|手上有|的(?:这款|那个)?(?:{_PRODUCT_TOPICS}))'
)
_HYPOTHETICAL_PURCHASE = re.compile(r'如果|假如|要是|还没买|没收到|没下单')
_ORDINAL = re.compile(r'第([一二三四五六两123456])(?:款|个|只|种)|前者|后者|最后(?:一)?(?:款|个|只|种)')
_REFERENCED_PRODUCT = (r'(?:(?:上面|刚才|之前|上一轮)(?:推荐的|说的)?)?'
                       r'(?:这(?:一)?款|那(?:一)?款|这(?:一)?个|那(?:一)?个|它|上一款|前者|后者|'
                       r'第[一二三四五六两123456][款个只种]|最后(?:一)?[款个只种])'
                       rf'(?:{_PRODUCT_TOPICS})?')
_SINGULAR_REFERENCE = re.compile(_REFERENCED_PRODUCT)
_GROUP_REFERENCE = re.compile(
    r'^(?:(?:刚才|之前|上面|前面)(?:(?:你|您)?(?:推荐|说|提到)的?)?)?'
    r'(?:(?:这|那)(?:(?P<count>[二两三四五六23456]|几)(?:款|个|只|种)|些|一组)|'
    r'它们|(?P<pair>两者|这对|这俩|那俩)|(?P<bare_count>[二两三四五六23456])(?:款|个|只|种)(?=都|分别|各)|分别)'
    rf'(?P<subject>{_PRODUCT_TOPICS})?'
)
_GROUP_QUALIFIER = re.compile(r'^(?:的|都|分别|各自|各)+')
_GROUP_COMPARISON = re.compile(r'对比|比较|区别|差异')
_REASON = re.compile(
    r'(?:那么|那|所以)?(?:刚才)?(?:你|您)?(?:'
    r'(?:为什么|为啥|为何|什么原因|什么理由|怎么说|推荐理由|理由是什么)(?:你|您)?'
    rf'(?:(?:会|要|就)?(?:选了?|选中|推荐|这么推荐|这么说|这么选)(?:{_REFERENCED_PRODUCT})?|是这样)?|'
    rf'(?:刚才)?(?:推荐|选择?)(?:{_REFERENCED_PRODUCT})?的(?:原因|理由)(?:是什么|是啥)?)'
    r'(?:呢|啊|呀|吗)?'
)
_CLARIFICATION = re.compile(
    r'(?:那么|那|所以)?(?:你|您|这|这句|这句话|刚才那句话|刚才说的|上面那句|它)?'
    r'(?:什么意思|是什么意思|啥意思|怎么理解|能解释一下|解释一下|能说具体点|说具体点|说明一下|能再说一遍|再说一遍|没听懂)'
    r'(?:呢|啊|呀|吗)?'
)
_ALTERNATIVES = re.compile(
    r'(?:那么|那)?(?:还有(?:呢|吗)|(?:还)?(?:有|有没有)(?:别的|其[他它])(?:款|型号|选择|推荐)?(?:呢|啊|呀|吗)?|'
    r'(?:还)?(?:有|有没有)(?:更(?:便宜|轻|轻便)的|便宜点的)(?:呢|啊|呀|吗)?|'
    r'(?:(?:能不能|可以|能)?再推荐|换|再来|再给我推荐)(?:另外一款|另一款|一款|几款|别款|别的)(?:呢|啊|呀|吗)?)'
)
_REFERENCE = re.compile(r'^(?:那么|那)?(?:它|这款|这一个|这个|刚才(?:推荐的)?(?:那款)?|上一款|上面那款|此前那款|前者|后者)')
_CATALOG_DETAIL = re.compile(
    r'多重|重量|净重|轻不轻|重不重|尺寸|体积|长宽|多(?:大|长|宽|厚)|容量|毫安|功率|输出|'
    r'多少钱|售价|价格|支持.{0,6}(?:快充|多少瓦)|保修|质保|配置|参数|包装|配件|续航|防水|防汗|降噪'
)
_VISUAL_FIELD = r'颜色|配色|色系|外观|样子|造型|设计风格|材质|材料|外壳|表面|触感'
_VISUAL_QUERY = re.compile(
    rf'(?:(?:的)?(?:有|还有|是|是什么|是啥|都有哪些|有哪些|都有|有什么|有啥|什么|啥|哪几种|哪种|几种)?(?:{_VISUAL_FIELD})'
    r'(?:是什么样|是什么|怎么样|如何|什么样|有哪些|有几种|有啥)?|长什么样|什么色|什么做的|看起来怎么样|'
    r'(?:还)?(?:有没有|有|是|可选)(?:[\u4e00-\u9fff]{0,5}[黑白灰蓝粉银红绿紫棕金米黄橙]色|'
    r'深色|浅色|[黑白灰蓝粉银红绿紫棕金米黄橙]的|(?:哑光|磨砂)(?:的|款)?)'
    r'|(?:什么|哪种)(?:材质|材料)|(?:哑光|磨砂)|尺寸(?:是多少|多大|怎么样)?)(?:吗|么|呢|啊|呀)?'
)
_FIT_ACTION = re.compile(r'[装放塞](?:得[下进]|不[下进]|[下进])')
_FIT_PROBE = re.compile(r'^(?:(?:我的)?(?:包包?|背包|挎包|手提包|口袋))?(?:能不能|能否|可不可以|能|可以|会不会)[装放塞]')
_DIMENSIONS = re.compile(r'\d+(?:\.\d+)?\s*[x×*＊✕]\s*\d+(?:\.\d+)?'
                         r'(?:\s*[x×*＊✕]\s*\d+(?:\.\d+)?)?\s*(?:厘米|毫米|(?:cm|mm)(?![a-z]))', re.I)
_CONTEXTUAL_PREFIX = re.compile(r'^(?:那么|那(?!一?[款个])|接下来|然后)')
_NEXT_STEP = re.compile(r'(?:(?:我|可以|能|能不能|该|要|应该|需要|想|要不要|先|再){0,5}'
                        r'(?:查什么|查看什么|怎么办|该怎么办|怎么(?:做|处理|排查|解决|检查)|做什么|试什么)|'
                        r'可以吗|能行吗|能用吗|能不能|呢)(?:呢|吗)?')
_AMBIGUOUS_CONTEXT = re.compile(r'^(?:(?:你|您)(?:刚才|方才|上一轮|刚说的)|(?:刚才|方才|上一轮)(?:你|您)|上面那|接着刚才|按你说的)')
_AMBIGUOUS_CUE = re.compile(r'哪些?角度|考虑|依据|讲究|出发点|怎么(?:想|选|考虑|判断|理解)|意思|'
                            r'解释|展开说|换一种|其他的吗|别的吗')
_WHY = re.compile(r'为什么|为何|为啥')
_PREFERENCE = re.compile(
    r'预算(?:[a-z0-9一二两三四五六七八九十百千万]{1,8}|紧|有限)|'
    r'[0-9一二两三四五六七八九十百千万]+(?:元|块钱|块|左右|以内|之内|以下)|'
    r'简约|简洁|简单一点|低调|素雅|商务|运动[风感]|复古|可爱|酷一点|时尚|有设计感|干净利落|'
    r'[黑白灰蓝粉银红绿紫棕金米黄橙]色|[黑白灰蓝粉银红绿紫棕金米黄橙]的|深色|浅色|'
    r'配色|外观|颜值|搭配|大小|尺寸|装得下|放得[下进]|塞得进|小(?:包|一点)|薄一点|'
    r'通勤|地铁|公交|上下班|日常|常戴|跑步|晨跑|健身|打游戏|居家|会议|听歌|'
    r'偏(?:轻|小)|轻便|便携|随身|便宜一点|耐用|防水|防汗|降噪|续航长|舒服|舒适'
)
_PREFERENCE_QUESTION = re.compile(
    rf'为什么|为啥|什么(?:是|叫|时候|{_VISUAL_FIELD}|$)|怎么|有(?:哪|什么|没有)|区别|吗$|么$|呢$'
)
_FACT_ROUTES = frozenset({'recommendation', 'catalog_quote', 'policy_quote', 'rag', 'support_wiki', 'context_followup'})
_SEMANTIC_FOLLOWUPS = frozenset({'reason', 'clarification', 'alternatives'})


@dataclass(frozen=True)
class SupportDialogue:
    """Pointers to relevant turns. Consumers revalidate evidence with the knowledge service."""

    intent: Intent
    anchor: dict | None = None
    evidence_turn: dict | None = None
    topic_query: str = ''
    product_skus: tuple[str, ...] = ()
    sku: str | None = None
    requested_sku: str | None = None
    ambiguous_product_reference: bool = False
    ordinal_reference: bool = False
    group_reference: bool = False
    group_count: int | None = None
    group_subject: str = ''
    seen_skus: tuple[str, ...] = ()
    recent: tuple[dict, ...] = ()
    recommendation_pending: bool = False
    policy_subject: str = ''


def _spoken_form(message: str) -> str:
    return _FILLER_PREFIX.sub('', normalize(message))


def _recommendation_model(message: str) -> str | None:
    normalized = unicodedata.normalize('NFKC', message)
    matches = list(_MODEL_MENTION.finditer(normalized))
    without_model = _REFOCUS_PREFIX.sub('', _spoken_form(_MODEL_MENTION.sub('', normalized)))
    if len(matches) != 1 or '推荐' not in normalized or not _REASON.fullmatch(without_model):
        return None
    return matches[0].group()


def _fit_question(spoken: str, *, question_mark: bool) -> bool:
    return bool(_FIT_ACTION.search(spoken)
                and (question_mark or spoken.endswith(('吗', '么', '呢', '不')) or _FIT_PROBE.match(spoken)))


def _group_reference(spoken: str):
    return _GROUP_REFERENCE.match(spoken) or _GROUP_REFERENCE.match(_CONTEXTUAL_PREFIX.sub('', spoken))


def _reference_count(value: str) -> int:
    return int(value) if value.isascii() else '一二三四五六'.index('二' if value == '两' else value) + 1


def _topic_name(name: str) -> str:
    return '充电宝' if name == '移动电源' else name


def _intent(message: str, spoken: str) -> Intent:
    if _TOPIC_SWITCH.search(spoken):
        return 'question'
    focused = _REFOCUS_PREFIX.sub('', spoken)
    if _TOPIC_SWITCH.search(focused):
        return 'question'
    normalized = unicodedata.normalize('NFKC', message)
    models = list(_MODEL_MENTION.finditer(normalized))
    question_mark = normalized.rstrip().endswith('?')
    if models:
        model_free = _spoken_form(_MODEL_MENTION.sub('', normalized))
        if len(models) == 1 and len(model_free) <= 70 and _fit_question(model_free, question_mark=question_mark):
            return 'fit_check'
        return 'reason' if _recommendation_model(message) else 'question'
    if _REASON.fullmatch(focused):
        return 'reason'
    if _CLARIFICATION.fullmatch(focused):
        return 'clarification'
    if _ALTERNATIVES.fullmatch(focused):
        return 'alternatives'
    if len(focused) <= 46 and (group := _group_reference(focused)):
        group_detail = _GROUP_QUALIFIER.sub('', group.string[group.end():])
        if (_VISUAL_QUERY.fullmatch(group_detail) or _CATALOG_DETAIL.search(group_detail)
                or _GROUP_COMPARISON.search(group_detail)):
            return 'product_followup'
    if not _PRODUCT_TOPIC.search(focused) and return_eligibility_question(message):
        return 'policy_followup'
    detail = _CONTEXTUAL_PREFIX.sub('', focused)
    reference = _SINGULAR_REFERENCE.match(detail)
    product_detail = detail[reference.end():] if reference else detail
    if (len(detail) <= 70 and not _NAMED_TOPIC.search(product_detail)
            and _fit_question(product_detail, question_mark=question_mark)):
        return 'fit_check'
    if len(detail) <= 30 and _VISUAL_QUERY.fullmatch(product_detail):
        return 'product_followup'
    previous_speech = _AMBIGUOUS_CONTEXT.search(focused)
    product_reference = (_SINGULAR_REFERENCE.match(focused) or _SINGULAR_REFERENCE.match(detail)
                         or focused != spoken and _SINGULAR_REFERENCE.search(focused))
    if len(focused) <= 48 and (_AMBIGUOUS_CUE.search(focused) and (previous_speech or product_reference)
                               or previous_speech and _WHY.search(focused)):
        return 'ambiguous'
    if len(focused) > 36 or _NAMED_TOPIC.search(focused):
        return 'question'
    if _ORDINAL.search(focused) or (len(detail) <= 21 and (
            _CATALOG_DETAIL.match(detail) or _REFERENCE.search(focused) and _CATALOG_DETAIL.search(detail))):
        return 'product_followup'
    if _REFERENCE.search(focused) and re.fullmatch(r'(?:那么|那)?(?:它|这款|这个|上一款|上面那款)(?:怎么样|呢|吗)?', focused):
        return 'product_followup'
    if _REFERENCE.search(focused) or _NEXT_STEP.fullmatch(detail):
        return 'followup'
    return 'question'


def _preference_reply(message: str, spoken: str) -> bool:
    normalized = unicodedata.normalize('NFKC', message)
    return (len(spoken) <= 55 and bool(_PREFERENCE.search(spoken) or _DIMENSIONS.search(normalized)) and not (
        _TOPIC_SWITCH.search(spoken) or _NAMED_TOPIC.search(spoken) or _PREFERENCE_QUESTION.search(spoken)
        or _MODEL_MENTION.search(normalized)
    ))


def _prior_intent(turn: dict, question: str, spoken: str) -> Intent:
    saved = turn.get('dialogue_intent')
    if saved == 'preferences':
        return 'preferences'
    local = _intent(question, spoken)
    if local == 'policy_followup' and turn.get('answer_route') == 'policy_quote' and not turn.get('follow_up_to_turn_id'):
        return 'question'
    if local == 'fit_check' and _MODEL_MENTION.search(unicodedata.normalize('NFKC', question)):
        return 'question'
    if local == 'ambiguous' and turn.get('dialogue_intent_source') == 'model':
        if saved == 'question' or saved in _SEMANTIC_FOLLOWUPS:
            return saved
    return local


def dialogue_window(turns: Sequence[dict]) -> tuple[dict, ...]:
    """Read six recent turns plus the linked topic root and latest product list."""
    window = tuple(turns[-DIALOGUE_HISTORY_LIMIT:])
    if not any(item.get('follow_up_to_turn_id') for item in window):
        return window
    root_id = None
    latest_list = None
    for offset, turn in enumerate(reversed(turns)):
        question = turn.get('question') or ''
        spoken = _spoken_form(question)
        if not spoken or _ACKNOWLEDGEMENT.fullmatch(spoken):
            continue
        if _prior_intent(turn, question, spoken) == 'question':
            if offset >= DIALOGUE_HISTORY_LIMIT and root_id and turn.get('turn_id') == root_id:
                return (turn, *(latest_list or ()), *window)
            return window
        link = turn.get('follow_up_to_turn_id')
        if link and root_id and link != root_id:
            return window
        if offset < DIALOGUE_HISTORY_LIMIT and link:
            root_id = link
        if (latest_list is None and root_id and link == root_id and _presented_products(turn)):
            latest_list = (turn,) if offset >= DIALOGUE_HISTORY_LIMIT else ()
        if offset >= DIALOGUE_HISTORY_LIMIT and not root_id:
            return window
    return window


def _can_reuse_evidence(turn: dict, snapshot_id: str, faq_ids: set[str], wiki_ids: set[str]) -> bool:
    wiki = turn.get('wiki_asset_id')
    return bool(snapshot_id and turn.get('knowledge_snapshot_id') == snapshot_id
                and turn.get('answer_route') in _FACT_ROUTES and turn.get('sources')
                and set(turn.get('faq_asset_ids') or ()).issubset(faq_ids)
                and (wiki in wiki_ids if wiki or turn.get('answer_route') == 'support_wiki' else True)
                and all(source.get('knowledge_snapshot_id', snapshot_id) == snapshot_id for source in turn['sources']))


def _product_skus(turn: dict) -> tuple[str, ...]:
    return tuple(dict.fromkeys(source['sku'] for source in turn['sources']
                               if source.get('kind') == 'product' and source.get('sku')))


def _presented_products(turn: dict) -> bool:
    return (turn.get('answer_route') == 'recommendation' and bool(turn.get('sources'))
            or turn.get('answer_route') == 'catalog_quote' and len(_product_skus(turn)) > 1)


def _resolve_sku(spoken: str, products: tuple[str, ...], evidence: dict | None) -> str | None:
    ordinal = _ORDINAL.search(spoken)
    if not ordinal:
        return products[0] if len(products) == 1 else None
    if not products or not evidence or evidence['answer_route'] not in ('recommendation', 'catalog_quote'):
        return None
    label = ordinal.group()
    if label in ('前者', '后者'):
        return products[label == '后者'] if len(products) == 2 else None
    if label.startswith('最后'):
        return products[-1]
    position = _reference_count(ordinal.group(1))
    return products[position - 1] if position <= len(products) else None


def resolve_dialogue(message: str, turns: Sequence[dict], snapshot_id: str, *,
                     valid_faq_ids: Collection[str] = (), valid_wiki_ids: Collection[str] = (),
                     intent_override: SemanticIntent | None = None) -> SupportDialogue:
    spoken = _spoken_form(message)
    intent = _intent(message, spoken)
    focused = _REFOCUS_PREFIX.sub('', spoken)
    ordinal_reference = bool(_ORDINAL.search(focused))
    group = _group_reference(focused) if intent == 'product_followup' else None
    count = (group['count'] or group['bare_count'] or ('两' if group['pair'] else '几')) if group else '几'
    group_count = _reference_count(count) if count != '几' else None
    if intent == 'fit_check' and (explicit_model := _MODEL_MENTION.search(unicodedata.normalize('NFKC', message))):
        return SupportDialogue(intent, requested_sku=explicit_model.group())
    if intent == 'question' and (return_status_reply(message) or return_freight_question(message, related=True)):
        previous = next((turn for turn in reversed(turns)
                         if (prior := _spoken_form(turn.get('question') or '')) and not _ACKNOWLEDGEMENT.fullmatch(prior)), None)
        if previous and previous.get('answer_route') == 'policy_quote':
            question = previous['question']
            if return_eligibility_question(question) or combined_return_question(question) or return_status_reply(question):
                return SupportDialogue('policy_followup', anchor=previous, topic_query=question)
    preference_reply = not group and intent in ('question', 'product_followup') and _preference_reply(message, spoken)
    if intent == 'question' and not preference_reply:
        return SupportDialogue(intent)

    recent = []
    for turn in reversed(dialogue_window(turns)):
        question = turn.get('question') or ''
        prior_spoken = _spoken_form(question)
        if not prior_spoken or _ACKNOWLEDGEMENT.fullmatch(prior_spoken):
            continue
        if _prior_intent(turn, question, prior_spoken) == 'question':
            anchor = turn
            recent.append(turn)
            break
        if turn.get('answer_route') != 'no_evidence' or turn.get('recommendation_pending') is True:
            recent.append(turn)
    else:
        if intent == 'policy_followup':
            return SupportDialogue('question')
        return SupportDialogue(intent, requested_sku=_recommendation_model(message) if intent == 'reason' else None,
                               ordinal_reference=ordinal_reference, group_reference=group is not None,
                               group_count=group_count, ambiguous_product_reference=group is not None)

    recommendation = next((turn for turn in recent
                           if turn.get('recommendation_pending') is True or turn.get('recommendation_query')), None)
    recommendation_pending = bool(recommendation and recommendation.get('recommendation_pending') is True)
    last_selection = next((turn for turn in recent if turn.get('answer_route') == 'recommendation'), {})
    broad_selection = last_selection and broad_earphone_recommendation(
        last_selection.get('recommendation_query') or last_selection['question']
    )
    if preference_reply and (recommendation_pending or broad_selection):
        intent = 'preferences'
    if intent == 'ambiguous':
        if intent_override == 'new_question':
            return SupportDialogue('question')
        if intent_override in _SEMANTIC_FOLLOWUPS:
            intent = intent_override
    if intent == 'question':
        return SupportDialogue(intent)
    requested_sku = _recommendation_model(message) if intent == 'reason' else None
    topic_query = (recommendation or {}).get('recommendation_query') or anchor['question']
    topics = {_topic_name(name) for name in _PRODUCT_TOPIC.findall(anchor['question'])}
    policy_subject = ''
    if intent == 'policy_followup':
        purchase = (anchor.get('answer_route') != 'recommendation' and _PURCHASE_CONTEXT.search(anchor['question'])
                    and not _HYPOTHETICAL_PURCHASE.search(anchor['question']))
        if len(topics) != 1 or not (_SINGULAR_REFERENCE.search(focused) or purchase):
            return SupportDialogue('question')
        policy_subject, = topics

    ordered = tuple(reversed(recent))
    faq_ids, wiki_ids = set(valid_faq_ids), set(valid_wiki_ids)
    factual = [turn for turn in ordered if _can_reuse_evidence(turn, snapshot_id, faq_ids, wiki_ids)]
    reference_turns = factual
    focused_products = ()
    if intent == 'reason' and factual:
        recommendations = [turn for turn in factual if turn['answer_route'] == 'recommendation' and _product_skus(turn)]
        if not recommendations:
            recommendations = [turn for turn in factual if turn['answer_route'] == 'context_followup' and _product_skus(turn)]
        if recommendations:
            reference_turns = recommendations
            if factual[-1]['answer_route'] == 'catalog_quote':
                focused_products = _product_skus(factual[-1])
    evidence = reference_turns[-1] if reference_turns else None
    if requested_sku:
        requested = normalize(requested_sku)
        evidence = next((turn for turn in reversed(reference_turns)
                         if any(normalize(model) == requested for model in _product_skus(turn))), evidence)
    elif group:
        evidence = next((turn for turn in reversed(factual) if _presented_products(turn)), None)
    elif ordinal_reference:
        evidence = next((turn for turn in reversed(reference_turns)
                         if turn['answer_route'] in ('recommendation', 'catalog_quote') and len(_product_skus(turn)) > 1), evidence)
    products = _product_skus(evidence) if evidence else ()
    sku = None if group else _resolve_sku(focused, products, evidence)
    if requested_sku:
        sku = next((model for model in products if normalize(model) == normalize(requested_sku)), None)
    elif (not sku and len(focused_products) == 1 and focused_products[0] in products
          and not ordinal_reference and _SINGULAR_REFERENCE.search(focused)):
        sku = focused_products[0]
    if group:
        ambiguous_product_reference = (len(products) < 2 or group_count is not None and len(products) != group_count
                                       or bool(group['subject'] and topics and _topic_name(group['subject']) not in topics))
    else:
        ambiguous_product_reference = sku is None and bool(
            intent == 'fit_check' and len(products) > 1
            or intent == 'product_followup' and (ordinal_reference or _REFERENCE.search(focused)
                                                 or _SINGULAR_REFERENCE.match(focused))
            or intent == 'reason' and (ordinal_reference or len(products) > 1 and _SINGULAR_REFERENCE.search(focused))
        )
    seen = tuple(dict.fromkeys(sku for turn in factual for sku in _product_skus(turn)))
    return SupportDialogue(intent=intent, anchor=anchor, evidence_turn=evidence, topic_query=sanitize(topic_query)[:300],
                           product_skus=products, sku=sku, requested_sku=requested_sku,
                           ambiguous_product_reference=ambiguous_product_reference, ordinal_reference=ordinal_reference,
                           group_reference=group is not None, group_count=group_count,
                           group_subject=_topic_name(group['subject']) if group and group['subject'] else '',
                           seen_skus=seen, recent=ordered,
                           recommendation_pending=recommendation_pending, policy_subject=policy_subject)
