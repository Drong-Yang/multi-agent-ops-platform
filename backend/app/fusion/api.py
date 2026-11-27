"""Authenticated fusion API and a separately authenticated RTC protocol edge."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.dependencies.auth import get_current_user
from app.graph.quality import ContentBrief
from app.fusion.repository import RepositoryError, Conflict, Invalid, NotFound
from app.fusion.runtime import public_task, public_asset, mode
from app.fusion.privacy import sanitize
from app.fusion.faq import select_faq_assets, describe_asset, hydrate_asset_topic
from app.fusion.legacy_rag.commerce import direct_answer, normalize, retrieval_query
from app.fusion.legacy_rag.llm_service import LLMService
from app.fusion.legacy_rag.rag_service import terms
from app.fusion.catalog_answers import catalog_quote
from app.fusion.deepseek_support import (
    DeepSeekUnavailable, answer_knowledge_gap, classify_dialogue_intent, deepseek_available,
)
from app.fusion.support_answers import (
    answer_product_fit, answer_return_eligibility, answer_return_freight, combined_return_question,
    explain_recommendation, mock_support_answer, recommend_product,
    recommendation_needs_preferences, recommendation_preference_question, recommendation_requested,
    return_eligibility_question, return_freight_question, return_status_reply,
)
from app.fusion.support_dialogue import dialogue_window, resolve_dialogue
from app.fusion.knowledge_access import KnowledgeUnavailable, active_snapshot_id
from app.fusion.support_wiki import WikiDraftInput, WikiReview, candidate_question, review_wiki, select_page
from app.fusion.support_triage import safety_incident_answer
from app.fusion.wiki_drafts import WikiDraftUnavailable, create_wiki_draft

router=APIRouter(prefix='/api/v1',tags=['Fusion'])
callbacks=APIRouter(prefix='/callbacks/rtc',tags=['RTC callbacks'])
_NO_EVIDENCE = '这点我暂时无法从现有资料确认。您可以补充具体型号和想了解的问题，也可以在购买平台联系商家核实。'
_INTRO = re.compile(
    r'(?:你好|您好|请问){0,2}(?:(?:你|您)(?:是(?:谁|什么|什么助手|什么机器人|干嘛的)|叫(?:什么|啥)(?:名字)?|'
    r'(?:有|会|能|可以)(?:帮我)?(?:做|干)?(?:什么|啥|嘛)(?:功能|用|事|忙)?)|'
    r'(?:能|可以|请)?(?:介绍(?:一下)?(?:你自己|自己)|自我介绍(?:一下)?))(?:吗|呢)?'
)
_INTRO_REPLY = ('您好，我是星核数码的智能客服助手。您可以问我商品参数、选购建议、常见故障排查和售后流程。'
                '查订单或办理退款，请通过购买平台的订单页操作。您想先了解哪一项？')
_PRIVATE_ACTION = re.compile(
    r'帮我(?:把钱退|把.{0,8}退款|取消订单|改地址|改收货|补发)|请把钱退给我|订单.{0,6}(?:查一下|现在什么状态)|'
    r'帮我(?:申请退款|(?:查(?:询)?|查看)(?:我的|这笔)?退款进度)|把我(?:的)?收货地址.{0,4}(?:改|修改)|'
    r'我(?:的|这笔)订单|我(?:的|这笔)退款|扣我钱|从我账号.{0,5}(?:转账|付款)|'
    r'你们(?:家|店|这里)?.{0,8}(?:客服电话|联系电话|门店地址|营业时间|今天价格|当前优惠|实时库存)'
)
_HAZARD_QUESTION = re.compile(r'爆炸|炸伤|起火|着火|冒.{0,2}烟|漏液|烫伤|触电|拆电池|短接电池')
_SKU_TOKEN = re.compile(r'(?<![a-z0-9])(?:sc|ew)[ -]*[a-z]+[ -]*\d+[a-z]*', re.I)
_VERIFY_RECOMMENDATION = re.compile(r'浸泡|浸水|泡水|潜水|落水|扔进水|飞机|航班|托运|登机')
_POLICY_TOPICS = tuple(map(re.compile, (
    r'退款|退钱|退回|退货|退换|无理由|拆[封包]|开封|能退|退吗',
    r'发票|开票|抬头', r'发货|物流|快递|运单|签收|配送|没收到', r'运费|邮费|上门取件',
    r'保修|质保|维修|返修|换新', r'平台介入|申诉|纠纷|投诉|赔付|赔偿',
)))
_POLICY_SUBJECT_REFERENCE = re.compile(r'^(?:(?:那(?:么)?|请问|我|这(?:一)?款|这个|它)[，、,\s]*)+')
_SUPPORT_ISSUES = tuple(map(re.compile, (
    r'断断续续|时断时续|断连|频繁断开|掉线|连接中断|卡顿|卡卡的',
    r'只有(?:一边|一只|左耳|右耳)|一边.{0,6}(?:响|声音)|(?:单耳|单边).{0,6}(?:响|声音)|没声音|无声|不出声',
    r'配对失败|配对不上|无法配对|连不上|连不起来|连接失败|无法连接|连接不上',
    r'不充电|无法充电|充不进|充不上|充不了|不通电',
    r'指示灯.{0,6}(?:异常|异样|一直闪|不亮)|不开机|无法开机|开不了机|死机|重启',
    r'报错|错误码|故障码|升级失败|无法升级|更新失败',
    r'不兼容|兼容问题|不识别|无法识别|识别不到',
)))
_GENERIC_TERMS = frozenset('怎么 如何 请问 什么 可以 能不 不能 是不 有没 没有 您好 你好 谢谢 '
                          '我想 想问 一下 处理 平台 商品 产品 客服 商家 购买 对应 '
                          '实际 具体 规则 问题 服务 情况 充电 电宝 耳机 手机 平板 电池'.split())
_ANSWER_NOTICES = {
    'assistant_intro': '本轮介绍客服身份和可提供的服务。',
    'business_boundary': '本轮使用固定业务答复。',
    'support_wiki': '本轮直接复用已审核的售后 Wiki。',
    'catalog_quote': '本轮引用商品原文。',
    'policy_quote': '本轮根据当前知识快照中的售后规则答复。',
    'recommendation': '本轮根据商品资料给出选购建议。',
    'no_evidence': '暂未找到与本轮问题匹配的资料。',
    'model_assist': '知识库暂无可靠依据。本轮为 DeepSeek 提供的通用建议，尚待核验。',
    'safety_incident': '本轮优先提供安全处置指引。',
    'context_followup': '本轮结合当前会话中的问题和有效资料回答追问。',
    'clarification': '需要确认咨询对象或选购偏好。',
}


def _answer_trace(route):
    return {'status': route, 'message': _ANSWER_NOTICES[route], 'selected_assets': [], 'candidates': []}


def _fixed_reply(rt, message, route, text):
    result = {'sources': [], 'context': '', 'knowledge_snapshot_id': rt.knowledge.active_snapshot_id,
              'answer_route': route, 'answer_model_skipped': True, 'direct_text': text, 'faq_reuse': _answer_trace(route)}
    return [{'role': 'user', 'content': sanitize(message)}], result, [], ''


def _policy_topics(text):
    return {pattern for pattern in _POLICY_TOPICS if pattern.search(text)}


def _relevant_sources(query, result, *, policy_only=False):
    """Keep strong semantic hits and use topical overlap for borderline matches."""
    question = query.rsplit('；追问：', 1)[-1]
    informative = terms(_SKU_TOKEN.sub('', question)) - _GENERIC_TERMS
    topics = _policy_topics(question)
    issues = {pattern for pattern in _SUPPORT_ISSUES if pattern.search(question)}
    sources = []
    for source in result['sources']:
        heading = source.get('source_section') or source.get('heading', '')
        heading_topics = _policy_topics(heading) if source.get('kind') == 'rule' else set()
        content = source.get('content', '')
        if policy_only and (source.get('kind') != 'rule' or not topics & (heading_topics | _policy_topics(content))):
            continue
        title_overlap = informative & terms(_SKU_TOKEN.sub('', heading))
        if heading_topics and not topics & heading_topics and len(title_overlap) < 2:
            continue
        issue_match = any(pattern.search(heading + '\n' + content) for pattern in issues)
        if issues and not topics & heading_topics and not issue_match:
            continue
        content_overlap = informative & terms(_SKU_TOKEN.sub('', content))
        score = source.get('score')
        semantic = isinstance(score, (int, float)) and score >= 0.65
        if semantic or issue_match or topics & heading_topics or title_overlap or len(content_overlap) >= 2:
            sources.append(source)
    return sources


def _eligible_for_assistance(message, result):
    return (result.get('retrieval_mode') not in ('unknown_sku', 'out_of_scope')
            and not _PRIVATE_ACTION.search(message) and not _HAZARD_QUESTION.search(message))


def _refresh_recommended_products(knowledge, snapshot_id, recent):
    """Recover a known recommendation after an index upgrade and cite current facts."""
    previous = next((turn for turn in reversed(recent) if turn.get('answer_route') == 'recommendation'), {})
    old_id = previous.get('knowledge_snapshot_id')
    refs = [source for source in previous.get('sources', []) if source.get('kind') == 'product']
    if not old_id or old_id == snapshot_id or not refs:
        return {}
    try:
        historic = knowledge.fact_bundle(old_id, refs)['evidence']
        current = knowledge.fact_bundle(snapshot_id, [source['id'] for source in historic])['evidence']
    except ValueError:
        return {}
    if [(source['sku'], source['kind']) for source in current] != [
            (source['sku'], source['kind']) for source in historic]:
        return {}
    return {**previous, 'sources': current}


def _dialogue_reply(knowledge, snapshot_id, dialogue, pages, message, *, voice=False, recommendation_query=''):
    """Answer contextual requests using current evidence."""
    intent = dialogue.intent
    contextual_intents = ('reason', 'clarification', 'alternatives', 'product_followup', 'followup')
    if intent in contextual_intents and not dialogue.anchor:
        return 'clarification', '您想接着聊哪个商品或问题？告诉我关键词，我接着帮您看。', [], None
    previous = dialogue.evidence_turn or {}
    if not previous and intent in ('reason', 'clarification', 'product_followup', 'fit_check'):
        previous = _refresh_recommended_products(knowledge, snapshot_id, dialogue.recent)
    product_refs = [source for source in previous.get('sources', []) if source.get('kind') == 'product']
    if intent == 'reason' and dialogue.requested_sku and not any(
            normalize(source['sku']) == normalize(dialogue.requested_sku) for source in product_refs):
        return 'clarification', '您问的是另一个型号吗？请确认想了解哪款的推荐理由。', [], None
    legacy_group = (dialogue.group_reference and len(product_refs) > 1
                    and dialogue.group_count in (None, len(product_refs))
                    and (not dialogue.group_subject or all(
                        dialogue.group_subject in source['source_section'] for source in product_refs)))
    legacy_single = not dialogue.group_reference and len(product_refs) == 1
    if dialogue.ambiguous_product_reference and not (
            not dialogue.product_skus and (legacy_group or legacy_single)
            and not dialogue.ordinal_reference and intent in ('product_followup', 'fit_check')):
        options = '、'.join(dialogue.product_skus or [source['sku'] for source in product_refs])
        if dialogue.group_reference:
            prompt = (f'刚才列出了 {options}。您想比较这几款，还是有其他型号？'
                      if options else '您想比较哪些商品？可以告诉我型号。')
        else:
            prompt = f'刚才提到了 {options}。您想了解哪一款？' if options else '您想了解哪一款？可以告诉我商品型号。'
        return 'clarification', prompt, [], None
    if dialogue.anchor and dialogue.anchor.get('answer_route') == 'safety_incident':
        advice = safety_incident_answer(dialogue.topic_query, ())
        if advice:
            return 'safety_incident', advice, [], None
    if dialogue.anchor and dialogue.anchor.get('answer_route') == 'business_boundary':
        boundary = direct_answer(dialogue.topic_query)
        if boundary:
            return 'business_boundary', boundary, [], None
    if intent == 'ambiguous':
        return 'clarification', '您想接着问刚才哪一点？也可以告诉我想聊的新问题。', [], None
    if intent in ('reason', 'clarification', 'alternatives', 'fit_check') and dialogue.recommendation_pending:
        prompt = recommendation_preference_question(dialogue.topic_query)
        return 'clarification', '我可以按您的实际需求筛选。' + prompt, [], None
    if intent == 'fit_check':
        if dialogue.requested_sku:
            requested = normalize(dialogue.requested_sku)
            product_refs = [chunk['id'] for chunk in knowledge.snapshot(snapshot_id)['corpus']['chunks']
                            if chunk.get('kind') == 'product' and normalize(chunk.get('sku', '')) == requested]
        elif dialogue.sku:
            product_refs = [source for source in product_refs if source['sku'] == dialogue.sku]
        if len(product_refs) != 1:
            return 'clarification', '您想核对哪个型号能不能放进去？', [], None
        evidence = knowledge.fact_bundle(snapshot_id, product_refs)['evidence']
        answer = answer_product_fit(f'{message}。{dialogue.topic_query}', evidence)
        if answer:
            return 'context_followup' if dialogue.anchor else 'catalog_quote', answer, evidence, None
        return 'clarification', '当前商品资料缺少完整尺寸。您可以核对商品详情页上的长、宽和厚度。', [], None
    if intent == 'product_followup' and product_refs:
        selected = [source for source in product_refs if source['sku'] == dialogue.sku] if dialogue.sku else product_refs
        try:
            evidence = knowledge.fact_bundle(snapshot_id, selected)['evidence']
        except ValueError:
            evidence = []
        quote = catalog_quote(message, {'retrieval_mode': 'catalog_exact', 'sources': evidence}, voice=voice)
        if quote is not None:
            answer, sources = quote
            return ('catalog_quote' if sources else 'no_evidence'), answer, sources, None
    if intent not in ('reason', 'clarification', 'alternatives'):
        return None

    original = dialogue.topic_query
    is_recommendation = recommendation_requested(original)
    is_recommendation_reply = previous.get('answer_route') in ('recommendation', 'context_followup')
    if intent == 'alternatives':
        if not is_recommendation:
            return 'clarification', '您想看其他商品，还是想了解其他处理办法？', [], None
        catalogue = knowledge.snapshot(snapshot_id)['corpus']['chunks']
        shown = dialogue.seen_skus
        answer, products = recommend_product(recommendation_query, catalogue, excluded_skus=shown)
        if not products:
            return 'context_followup', '同样条件下，现有资料里没有新的合适型号。您更愿意调整预算，还是调整用途和功能要求？', [], None
        evidence = knowledge.fact_bundle(snapshot_id, [product['id'] for product in products])['evidence']
        return 'recommendation', answer, evidence, None

    if is_recommendation and is_recommendation_reply and product_refs:
        if dialogue.sku:
            product_refs = [source for source in product_refs if source['sku'] == dialogue.sku]
        elif dialogue.requested_sku:
            product_refs = [source for source in product_refs if normalize(source['sku']) == normalize(dialogue.requested_sku)]
        try:
            evidence = knowledge.fact_bundle(snapshot_id, product_refs)['evidence']
        except ValueError:
            return None
        criteria = previous.get('recommendation_query') or previous.get('question', '')
        explained = explain_recommendation(criteria if recommendation_requested(criteria) else original, evidence,
                                           clarify=intent == 'clarification')
        if explained:
            answer, sources = explained
            return 'context_followup', answer, sources, None
    if is_recommendation and dialogue.anchor.get('answer_route') == 'recommendation' and not product_refs:
        return 'clarification', '您想了解刚才哪款商品的推荐理由？告诉我型号，我可以按当前资料重新核对。', [], None
    wiki_id = previous.get('wiki_asset_id')
    page = next((page['payload'] for page in pages if page['id'] == wiki_id), None) if wiki_id else None
    if page:
        wiki = page['wiki']
        answer = f'这条说明适用于{wiki["scope"]}。{wiki["answer"]}' if intent == 'reason' else wiki['answer']
        return 'context_followup', answer, page['evidence_refs'], wiki_id
    return None


class Input(BaseModel):
    model_config=ConfigDict(extra='forbid')


class ConsultationInput(Input):
    channel: Literal['text','voice']='text'


class MessageInput(Input):
    message: str=Field(min_length=1,max_length=3000)


class InsightInput(Input):
    session_id: str
    turn_id: str
    summary: str=Field(min_length=1,max_length=2000)
    confirmed: bool


class TaskInput(Input):
    insight_id: str|None=None
    topic_direction: str=Field(default='',max_length=200)
    content_type: Literal['faq','article','wiki']='faq'
    product_scope: list[str]=Field(default_factory=list,max_length=5)
    content_brief: ContentBrief=Field(default_factory=lambda:ContentBrief(min_chars=50,max_chars=1500))


class ActionInput(Input):
    action: Literal['select_topic','reject','approve_text','retry','generate_images']
    data: dict=Field(default_factory=dict)
    expected_state_version: int=Field(ge=1)


class VersionInput(Input):
    expected_state_version: int=Field(ge=1)


def runtime(request):return request.app.state.fusion


def key_required(key):
    if not key or not key.strip() or len(key)>200:
        raise HTTPException(422,'需要1～200字符的 Idempotency-Key')
    return key


def sse(kind,payload,**meta):
    return 'data: '+json.dumps({'schema_version':'1.0','event_id':uuid.uuid4().hex,
                              'type':kind,'payload':payload,**meta},ensure_ascii=False)+'\n\n'


async def active_id(rt):
    try:
        return await active_snapshot_id(rt)
    except KnowledgeUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


async def prepare_answer(rt,session,message):
    turns = session['payload'].get('turns', [])
    safety = safety_incident_answer(message, turns[-2:])
    if safety:
        # A product-safety reply must still be delivered during a knowledge-service outage.
        return _fixed_reply(rt, message, 'safety_incident', safety)
    if _INTRO.fullmatch(normalize(message)):
        return _fixed_reply(rt, message, 'assistant_intro', _INTRO_REPLY)
    fixed = direct_answer(message)
    if fixed:
        return _fixed_reply(rt, message, 'business_boundary', fixed)
    if _PRIVATE_ACTION.search(message):
        return _fixed_reply(rt, message, 'business_boundary',
                            '请通过购买平台的订单页核对实时信息、办理业务或联系商家人工客服。')

    snapshot_id = await active_id(rt)
    owner = session['owner_id']
    available = await rt.db('valid_faq', owner)
    valid_ids = {a['id'] for a in available if a['payload'].get('knowledge_snapshot_id') == snapshot_id}
    recent_turns = dialogue_window(turns)
    needs_wiki = candidate_question(message) or _policy_topics(message) or any(
        t.get('wiki_asset_id') for t in recent_turns)
    pages = await rt.db('valid_wiki', owner) if needs_wiki else []
    wiki_ids = {page['id'] for page in pages if page['payload']['knowledge_snapshot_id'] == snapshot_id}
    evidence_state = {'valid_faq_ids': valid_ids, 'valid_wiki_ids': wiki_ids}
    dialogue = resolve_dialogue(message, recent_turns, snapshot_id, **evidence_state)
    classification = None
    if (dialogue.intent == 'ambiguous' and dialogue.anchor
            and dialogue.anchor.get('answer_route') not in ('safety_incident', 'business_boundary')
            and mode() == 'live' and deepseek_available()):
        classification = await classify_dialogue_intent(message, dialogue.topic_query)
        if classification:
            dialogue = resolve_dialogue(message, recent_turns, snapshot_id,
                                        intent_override=classification, **evidence_state)
    previous = [dialogue.recent[0], dialogue.recent[-1]] if len(dialogue.recent) > 2 else list(dialogue.recent)
    history = []
    for turn in previous:
        history.append({'role':'user', 'content':turn['question']})
        if (turn.get('answer_route') != 'model_assist' and turn.get('knowledge_snapshot_id') == snapshot_id
                and set(turn.get('faq_asset_ids', [])).issubset(valid_ids)
                and (not turn.get('wiki_asset_id') or turn['wiki_asset_id'] in wiki_ids)):
            history.append({'role':'assistant', 'content':turn['answer']})
    current_question = sanitize(message)
    if dialogue.policy_subject:
        current_question = dialogue.policy_subject + _POLICY_SUBJECT_REFERENCE.sub('', current_question)
    history.append({'role':'user', 'content':current_question})
    query = retrieval_query(history)
    if dialogue.intent == 'product_followup' and dialogue.product_skus:
        products = dialogue.sku or ' '.join(dialogue.product_skus)
        query = f'{products}；追问：{history[-1]["content"]}'
    elif dialogue.policy_subject:
        query = current_question
    elif dialogue.anchor and dialogue.intent in ('reason', 'clarification'):
        query = dialogue.topic_query
    elif dialogue.anchor and query == history[-1]['content']:
        query = f'{dialogue.topic_query}；追问：{history[-1]["content"]}'
    recommendation_query = sanitize(message)
    if dialogue.intent in ('preferences', 'alternatives') and recommendation_requested(dialogue.topic_query):
        recommendation_query = f'{recommendation_query}。{dialogue.topic_query}'
    voice = session['payload'].get('channel') == 'voice'
    wiki = select_page(pages, current_question, snapshot_id)
    return_context = dialogue.intent == 'policy_followup' and dialogue.anchor and dialogue.anchor.get('answer_route') == 'policy_quote'
    combined_return = combined_return_question(message)
    return_eligibility = combined_return or return_eligibility_question(message) or (return_context and return_status_reply(message))
    return_freight = combined_return or return_freight_question(message, related=bool(return_context))
    may_assist = False
    if wiki:
        payload = wiki['payload']
        fixed = payload['wiki']['answer']
        result = {'sources':payload['evidence_refs'], 'context':'', 'knowledge_snapshot_id':snapshot_id,
                  'wiki_asset_id':wiki['id']}
        route = 'support_wiki'
    elif return_eligibility or return_freight:
        catalogue = rt.knowledge.snapshot(snapshot_id)['corpus']['chunks']
        fixed, rules = '', []
        if return_eligibility:
            fixed, rules = answer_return_eligibility(message, catalogue, ask_status=not return_freight)
        if return_freight:
            freight, freight_rules = answer_return_freight(catalogue)
            fixed += freight
            rules.extend(freight_rules)
        sources = rt.knowledge.fact_bundle(snapshot_id, [rule['id'] for rule in rules])['evidence'] if rules else []
        result = {'sources': sources, 'context': '', 'knowledge_snapshot_id': snapshot_id}
        route = 'policy_quote' if sources else 'no_evidence'
    elif followup := _dialogue_reply(rt.knowledge, snapshot_id, dialogue, pages, message, voice=voice,
                                    recommendation_query=recommendation_query):
        route, fixed, sources, wiki_id = followup
        result = {'sources': sources, 'context': '', 'knowledge_snapshot_id': snapshot_id}
        if wiki_id:
            result['wiki_asset_id'] = wiki_id
        if route == 'recommendation' and dialogue.intent == 'alternatives':
            result['recommendation_query'] = recommendation_query
    elif recommendation_requested(recommendation_query):
        catalogue = rt.knowledge.snapshot(snapshot_id)['corpus']['chunks']
        fixed, selected_products = recommend_product(recommendation_query, catalogue)
        sources = rt.knowledge.fact_bundle(snapshot_id, [item['id'] for item in selected_products])['evidence']
        result = {'sources':sources, 'context':'', 'knowledge_snapshot_id':snapshot_id}
        if dialogue.intent == 'preferences':
            result['recommendation_query'] = recommendation_query
        if not sources and recommendation_needs_preferences(recommendation_query):
            route = 'clarification'
            result['recommendation_pending'] = True
        else:
            route = 'recommendation' if sources else 'no_evidence'
            may_assist = (not sources and dialogue.intent != 'preferences' and not _SKU_TOKEN.search(message)
                          and not _VERIFY_RECOMMENDATION.search(message))
    else:
        retrieval_unavailable = False
        try:
            result = await asyncio.wait_for(rt.knowledge.retrieve(query, snapshot_id=snapshot_id), timeout=5)
        except (TimeoutError, httpx.HTTPError, RuntimeError):
            if mode() == 'mock':
                raise
            retrieval_unavailable = True
            result = {'sources': [], 'context': '', 'knowledge_snapshot_id': snapshot_id}
        quote = catalog_quote(message, result, voice=voice)
        if quote is not None:
            fixed, result['sources'] = quote
            result['context'] = ''
            route = 'catalog_quote' if result['sources'] else 'no_evidence'
        elif mode() == 'mock':
            fixed, result['sources'] = (mock_support_answer(query, result['sources'])
                                        if result['sources'] else (_NO_EVIDENCE, []))
            result['context'] = ''
            route = 'rag' if result['sources'] else 'no_evidence'
        else:
            relevant = _relevant_sources(query, result, policy_only=dialogue.intent == 'policy_followup')
            if relevant:
                if relevant != result['sources']:
                    result['sources'] = relevant
                    result['context'] = '\n\n'.join(
                        f'[来源：{source["source"]}；章节：{source["source_section"]}]\n{source["content"]}'
                        for source in relevant
                    )
                    result['retrieved_context'] = result['context']
                route = 'rag'
            else:
                result.update(sources=[], context='', retrieved_context='', knowledge_gap=True)
                route, fixed = 'no_evidence', _NO_EVIDENCE
                if retrieval_unavailable:
                    fixed = '资料检索暂时不可用。您可以稍后重试，或在购买平台联系商家核实。'
                may_assist = True
    model_attempted = (may_assist and mode() == 'live' and _eligible_for_assistance(message, result)
                       and deepseek_available())
    if model_attempted:
        result['knowledge_gap'] = True
        current = history[-1]['content']
        try:
            async with asyncio.timeout(9 if voice else 16):
                answer = await answer_knowledge_gap(current, previous if query != current else ())
        except (DeepSeekUnavailable, TimeoutError):
            if fixed == _NO_EVIDENCE:
                fixed = '暂时无法给出可靠建议。您可以补充详细情况再试，或在购买平台联系商家核实。'
        else:
            route, fixed = 'model_assist', answer
            result['answer_model'] = 'deepseek-flash'
    selected = []
    if route == 'rag':
        selected, trace = await select_faq_assets(rt, owner, message, query, result, candidates=available)
        if fixed and selected:
            trace['message'] = f'{len(selected)} 份已审 FAQ 与本轮匹配。本轮演示答复基于原始资料。'
    else:
        trace = _answer_trace(route)
    result.update(faq_reuse=trace, answer_route=route, answer_model_skipped=bool(fixed) and not model_attempted,
                  dialogue_intent=dialogue.intent)
    if classification:
        result['dialogue_intent_source'] = 'model'
    if dialogue.anchor and dialogue.anchor.get('turn_id'):
        result['follow_up_to_turn_id'] = dialogue.anchor['turn_id']
    if fixed:
        result['direct_text'] = fixed
    context = result['context']
    if selected:
        context += '\n以下为已审FAQ表达素材，不能新增或覆盖上方基础事实：\n' + '\n'.join(
            a['payload']['content'] for a in selected)
    return history,result,selected,context


async def answer_chunks(rt,session,message):
    history,result,selected,context=await prepare_answer(rt,session,message)
    fixed = result.pop('direct_text', None)
    yield {'kind':'evidence','result':result,'faq_asset_ids':[a['id'] for a in selected]}
    answer = fixed or ''
    if fixed:
        yield {'kind':'text', 'text':fixed}
    else:
        llm = LLMService(rt.knowledge.config, rt.knowledge.snapshot(result['knowledge_snapshot_id'])['corpus'])
        try:
            async for chunk in llm.chat_stream(history, context):
                for choice in chunk.get('choices', []):
                    text = choice.get('delta', {}).get('content') or ''
                    if text:
                        answer += text
                        yield {'kind':'text', 'text':text}
        except Exception:
            failure = '服务暂时不可用，请稍后重试；本次未完成回答。'
            yield {'kind':'text', 'text':failure}
            answer += failure
    turn={'turn_id':uuid.uuid4().hex,'question':sanitize(message),'answer':sanitize(answer),
          'sources':result['sources'],'knowledge_snapshot_id':result['knowledge_snapshot_id'],
          'faq_asset_ids':[a['id'] for a in selected],'faq_reuse':result['faq_reuse'],'mode':mode(),
          'answer_route':result['answer_route'],'answer_model_skipped':result['answer_model_skipped'],
          'wiki_asset_id':result.get('wiki_asset_id'),
          'wiki_candidate':result['answer_route'] not in ('safety_incident', 'assistant_intro', 'business_boundary')
                            and candidate_question(message)}
    for field in ('answer_model', 'knowledge_gap', 'dialogue_intent', 'dialogue_intent_source',
                  'follow_up_to_turn_id', 'recommendation_pending', 'recommendation_query'):
        if field in result:
            turn[field] = result[field]
    # A concurrent turn can't silently overwrite this one.
    for attempt in range(3):
        current=await rt.db('get',session['id'],session['owner_id'],'consultation')
        try:
            await rt.db('update_cas',current['id'],current['owner_id'],current['state_version'],
                        {**current['payload'],'turns':current['payload'].get('turns',[])+[turn]})
            break
        except Conflict:
            if attempt==2:raise
    yield {'kind':'done','turn':turn}


@router.get('/knowledge/active')
async def knowledge_active(request:Request,user=Depends(get_current_user)):
    rt=runtime(request);sid=await active_id(rt)
    return {'knowledge_snapshot_id':sid,'mode':mode(),'retrieval_mode':rt.knowledge.snapshot(sid)['retrieval_profile']['mode'],
            'snapshot':rt.knowledge.snapshot(sid)}


@router.post('/consultations')
async def create_consultation(body:ConsultationInput,request:Request,user=Depends(get_current_user)):
    record=await runtime(request).db('create','consultation',str(user.id),
                                    {'channel':body.channel,'turns':[],'mode':mode()})
    return {**record['payload'],'session_id':record['id'],'state_version':record['state_version']}


@router.get('/consultations/{session_id}')
async def get_consultation(session_id:str,request:Request,user=Depends(get_current_user)):
    record=await runtime(request).db('get',session_id,str(user.id),'consultation')
    return {**record['payload'],'session_id':record['id'],'state_version':record['state_version']}


@router.get('/consultations')
async def list_consultations(request:Request,user=Depends(get_current_user)):
    records=await runtime(request).db('list','consultation',str(user.id))
    return {'sessions':[{'session_id':r['id'],'channel':r['payload']['channel'],
                         'turn_count':len(r['payload'].get('turns',[])),
                         'last_question':r['payload'].get('turns',[{}])[-1].get('question','') if r['payload'].get('turns') else '',
                         'updated_at':r['updated_at']} for r in reversed(records)]}


@router.post('/consultations/{session_id}/messages')
async def consultation_message(session_id:str,body:MessageInput,request:Request,user=Depends(get_current_user)):
    rt=runtime(request);session=await rt.db('get',session_id,str(user.id),'consultation')
    async def stream():
        try:
            async for item in answer_chunks(rt,session,body.message):
                if item['kind']=='text':yield sse('answer_delta',{'content':item['text']},session_id=session_id)
                elif item['kind']=='evidence':yield sse('evidence',item['result'],session_id=session_id)
                else:yield sse('answer_completed',item['turn'],session_id=session_id)
        except Exception:
            yield sse('run_failed',{'message':'知识检索不可用，本次未切换模拟模式'},session_id=session_id)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@router.post('/insights')
async def create_insight(body:InsightInput,request:Request,user=Depends(get_current_user),
                         idem:str|None=Header(None,alias='Idempotency-Key')):
    if not body.confirmed:raise HTTPException(422,'请先确认要保存的脱敏摘要')
    rt=runtime(request);owner=str(user.id);key_required(idem)
    def operation(tx):
        session=tx.get(body.session_id,owner,'consultation')
        turn=next((t for t in session['payload']['turns'] if t['turn_id']==body.turn_id),None)
        if turn is None:raise NotFound('Consultation turn not found')
        record=tx.create('insight',owner,{'summary':sanitize(body.summary),'confirmed':True,
            'session_id':body.session_id,'turn_id':body.turn_id,'evidence_refs':turn['sources'],
            'knowledge_snapshot_id':turn['knowledge_snapshot_id'],'mode':turn['mode']},source_id=body.session_id)
        return {**record['payload'],'insight_id':record['id']}
    return await rt.db('idempotent',owner,'create_insight',idem,body.model_dump(),operation)


@router.post('/content-tasks',status_code=202)
async def create_task(body:TaskInput,request:Request,user=Depends(get_current_user),
                      idem:str|None=Header(None,alias='Idempotency-Key')):
    rt=runtime(request);owner=str(user.id);key_required(idem)
    replay=await rt.db('lookup_operation',owner,'create_task',idem,body.model_dump())
    if replay is not None:
        rt.wake()
        return replay
    insight=await rt.db('get',body.insight_id,owner,'insight') if body.insight_id else None
    topic=sanitize(body.topic_direction or (insight['payload']['summary'] if insight else ''))[:200]
    if not topic:raise HTTPException(422,'需要已确认的咨询线索或内容方向')
    sid=await active_id(rt)
    if body.content_type == 'wiki':
        if not insight:
            raise Invalid('售后 Wiki 须来自已确认的咨询线索')
        return await rt.db('idempotent', owner, 'create_task', idem, body.model_dump(),
                           lambda tx: create_wiki_task(tx, rt.knowledge, owner, insight, topic, sid))
    try:
        result=await rt.knowledge.retrieve(topic,snapshot_id=sid,product_scope=body.product_scope or None,purpose='writing')
    except Exception as exc:
        raise HTTPException(503,'知识检索不可用，暂未创建内容任务') from exc
    if not result['sources']:raise HTTPException(422,'没有可用于创作的商品证据，请补充准确型号或资料')
    bundle=rt.knowledge.fact_bundle(sid,result['sources'])
    scope=body.product_scope or bundle.get('supported_skus',[])
    def operation(tx):
        task_id=uuid.uuid4().hex;run_id=uuid.uuid4().hex
        source={'insight_id':body.insight_id or '',
                'session_id':insight['payload']['session_id'] if insight else '',
                'turn_id':insight['payload']['turn_id'] if insight else ''}
        task=tx.create('task',owner,{'run_id':run_id,'thread_id':'fusion_'+owner+'_'+task_id,
            'status':'queued','content_type':body.content_type,'topic_direction':topic,
            'content_brief':body.content_brief.model_dump(),'product_scope':scope,
            'knowledge_snapshot_id':sid,'evidence_refs':result['sources'],'fact_bundle':bundle,
            'source':source,'values':{},'interrupt_info':None,'mode':mode(),'image_status':'not_requested'},
            record_id=task_id,source_id=body.insight_id)
        tx.create('run',owner,{'task_id':task_id,'action':'start','status':'queued'},record_id=run_id,source_id=task_id)
        return public_task(task)
    value=await rt.db('idempotent',owner,'create_task',idem,body.model_dump(),operation)
    rt.wake();return value


def create_wiki_task(tx, knowledge, owner, insight, topic, sid):
    origin = insight['payload']
    session = tx.get(origin['session_id'], owner, 'consultation')
    turn = next((item for item in session['payload']['turns'] if item['turn_id'] == origin['turn_id']), None)
    assisted = bool(turn and turn.get('answer_route') == 'model_assist')
    if (not turn or turn.get('answer_route') in ('safety_incident', 'assistant_intro', 'business_boundary')
            or (assisted and turn.get('wiki_candidate') is not True) or not candidate_question(turn['question'])):
        raise Invalid('请选择问题明确、可独立说明的售后咨询轮次')
    evidence = []
    if not assisted and origin['knowledge_snapshot_id'] == sid:
        refs = [item for item in origin['evidence_refs'] if item.get('kind') != 'wiki_evidence']
        evidence = knowledge.fact_bundle(sid, refs)['evidence']
    task = tx.create('task', owner, {
        'content_type':'wiki', 'status':'waiting_human', 'topic_direction':topic, 'knowledge_snapshot_id':sid,
        'source':{'insight_id':insight['id'], 'session_id':origin['session_id'], 'turn_id':origin['turn_id']},
        'source_question':turn['question'], 'source_answer':turn['answer'][:800], 'source_answer_route':turn.get('answer_route'),
        **({'source_answer_model': turn.get('answer_model')} if assisted else {}),
        'evidence_refs':evidence, 'values':{},
    }, source_id=insight['id'])
    return public_task(task)


@router.post('/content-tasks/{task_id}/wiki-draft')
async def draft_wiki(task_id: str, body: WikiDraftInput, request: Request, user=Depends(get_current_user)):
    try:
        return await create_wiki_draft(runtime(request), str(user.id), task_id, body)
    except WikiDraftUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post('/content-tasks/{task_id}/wiki-review')
async def approve_wiki(task_id: str, body: WikiReview, request: Request, user=Depends(get_current_user),
                       idem: str | None = Header(None, alias='Idempotency-Key')):
    rt = runtime(request)
    owner = str(user.id)
    key_required(idem)
    def operation(tx):
        task, asset = review_wiki(tx, rt.knowledge, task_id, owner, body)
        return {'task':public_task(task), 'asset':public_asset(asset)}
    return await rt.db('idempotent', owner, 'wiki_review:' + task_id, idem, body.model_dump(mode='json'), operation)


@router.get('/content-tasks')
async def list_tasks(request:Request,user=Depends(get_current_user)):
    return {'tasks':[public_task(r) for r in reversed(await runtime(request).db('list','task',str(user.id)))]}


@router.get('/content-tasks/{task_id}')
async def get_task(task_id:str,request:Request,user=Depends(get_current_user)):
    return public_task(await runtime(request).db('get',task_id,str(user.id),'task'))


@router.post('/content-tasks/{task_id}/actions',status_code=202)
async def task_action(task_id:str,body:ActionInput,request:Request,user=Depends(get_current_user),
                      idem:str|None=Header(None,alias='Idempotency-Key')):
    rt=runtime(request);owner=str(user.id);key_required(idem)
    def operation(tx):
        task=tx.get(task_id,owner,'task');payload=task['payload'];values=payload.get('values',{})
        if payload.get('content_type') == 'wiki':raise Invalid('请在售后 Wiki 核对表单中审核或修订')
        if task['state_version']!=body.expected_state_version:raise Conflict('任务已变化，请刷新后重试')
        if payload['status'] in ('queued','running'):raise Conflict('任务正在执行，请勿重复推进')
        info=payload.get('interrupt_info') or {};resume={}
        if body.action=='select_topic':
            topic=body.data.get('selected_topic')
            if info.get('action_required')!='select_topic':raise Conflict('当前不是选题阶段')
            if topic not in values.get('generated_topics',[]):raise Invalid('选题不在候选列表中')
            resume={'selected_topic':topic}
        elif body.action in ('reject','approve_text'):
            if info.get('action_required')!='review':raise Conflict('当前不是人工审稿阶段')
            if body.action=='approve_text':
                active=tx.get_active_snapshot()
                if not active or active['id']!=payload['knowledge_snapshot_id']:raise Conflict('知识已更新，请以新版本重新创建任务并审核')
                if values.get('fact_check_report',{}).get('passed') is not True:raise Conflict('关键事实检查未通过，暂不能批准')
                resume={'action':'approve'}
            else:
                feedback=sanitize(body.data.get('feedback',''))
                if not feedback or len(feedback)>2000:raise Invalid('修改意见须为1～2000字符')
                resume={'action':'reject','feedback':feedback}
        elif body.action=='retry':
            if payload['status'] not in ('failed','interrupted'):raise Conflict('任务不处于可重试状态')
        elif body.action=='generate_images':
            if payload['status']!='text_approved':raise Conflict('请先批准文本再生成配图')
        run_id=uuid.uuid4().hex
        tx.create('run',owner,{'task_id':task_id,'action':body.action,'resume':resume,'status':'queued',
                              'previous_run_id':payload.get('run_id')},record_id=run_id,source_id=task_id)
        updated=tx.update_cas(task_id,owner,body.expected_state_version,
                              {**payload,'run_id':run_id,'status':'queued','error':''})
        return public_task(updated)
    value=await rt.db('idempotent',owner,'task_action:'+task_id,idem,body.model_dump(),operation)
    rt.wake();return value


@router.get('/content-tasks/{task_id}/events')
async def task_events(task_id:str,request:Request,user=Depends(get_current_user)):
    rt=runtime(request);owner=str(user.id)
    await rt.db('get',task_id,owner,'task')
    async def stream():
        async with rt.subscribe(task_id) as queue:
            task=public_task(await rt.db('get',task_id,owner,'task'))
            yield sse('state_snapshot',task,task_id=task_id,run_id=task['run_id'],state_version=task['state_version'])
            while task['status'] in ('queued','running'):
                if await request.is_disconnected():return
                try:
                    event=await asyncio.wait_for(queue.get(),timeout=1)
                    yield 'data: '+json.dumps(event,ensure_ascii=False)+'\n\n'
                except asyncio.TimeoutError:
                    yield ': heartbeat\n\n'
                task=public_task(await rt.db('get',task_id,owner,'task'))
            yield sse('state_snapshot',task,task_id=task_id,run_id=task['run_id'],state_version=task['state_version'])
            yield sse('run_completed',{'status':task['status']},task_id=task_id,run_id=task['run_id'])
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache'})


@router.get('/assets')
async def list_assets(request:Request,user=Depends(get_current_user)):
    rt=runtime(request);owner=str(user.id);sid=await active_id(rt)
    eligible={a['id'] for a in await rt.db('valid_faq',owner)}
    wiki_ids = {a['id'] for a in await rt.db('valid_wiki', owner)}
    assets=[]
    for record in reversed(await rt.db('list','asset',owner)):
        record=await hydrate_asset_topic(rt,record,owner)
        if record['payload'].get('content_type') == 'wiki':
            assets.append({**public_asset(record), 'wiki_eligible':record['id'] in wiki_ids})
        else:
            assets.append({**public_asset(record),'reuse':describe_asset(record,sid,eligible=record['id'] in eligible)})
    return {'assets':assets}


async def asset_operation(request,user,asset_id,body,idem,action):
    key_required(idem);rt=runtime(request);owner=str(user.id)
    def operation(tx):
        return public_asset(getattr(tx,action)(asset_id,owner,body.expected_state_version))
    return await rt.db('idempotent',owner,action+':'+asset_id,idem,body.model_dump(),operation)


@router.post('/assets/{asset_id}/publish')
async def publish_asset(asset_id:str,body:VersionInput,request:Request,user=Depends(get_current_user),
                        idem:str|None=Header(None,alias='Idempotency-Key')):
    return await asset_operation(request,user,asset_id,body,idem,'publish')


@router.post('/assets/{asset_id}/revoke')
async def revoke_asset(asset_id:str,body:VersionInput,request:Request,user=Depends(get_current_user),
                       idem:str|None=Header(None,alias='Idempotency-Key')):
    return await asset_operation(request,user,asset_id,body,idem,'revoke')


def callback_secret():
    value=os.getenv('FUSION_CALLBACK_SECRET','')
    if len(value)<24:raise HTTPException(503,'回调凭据未配置')
    return value


async def bound_voice(request,handle,authorization):
    expected='Bearer '+callback_secret()
    if not authorization or not hmac.compare_digest(authorization,expected):raise HTTPException(401,'回调鉴权失败')
    digest=hashlib.sha256(handle.encode()).hexdigest()
    # Internal-only lookup; callbacks never accept an operator/user ID from the body.
    for record in await runtime(request).db('scan','voice'):
        if hmac.compare_digest(record['payload']['handle_sha256'],digest):
            if record['payload']['expires_at']<time.time():raise HTTPException(403,'语音会话已过期')
            return record
    raise HTTPException(404,'语音会话不存在')


@router.post('/voice/sessions')
async def voice_session(body:ConsultationInput,request:Request,user=Depends(get_current_user)):
    rt=runtime(request);owner=str(user.id);handle=secrets.token_urlsafe(32)
    session=await rt.db('create','consultation',owner,{'channel':'voice','turns':[],'mode':mode()})
    voice=await rt.db('create','voice',owner,{'handle_sha256':hashlib.sha256(handle.encode()).hexdigest(),
        'session_id':session['id'],'expires_at':time.time()+3600,'status':'created'},source_id=session['id'])
    return {'voice_session_id':voice['id'],'session_id':session['id'],'callback_handle':handle,
            'callback_path':'/callbacks/rtc/llm/'+handle,'mode':mode(),'rtc_ready':False}


@callbacks.post('/llm')
async def unbound_callback(request:Request,authorization:str|None=Header(None)):
    if not authorization or not hmac.compare_digest(authorization,'Bearer '+callback_secret()):raise HTTPException(401,'回调鉴权失败')
    raise HTTPException(403,'需要已绑定的专用回调地址')


@callbacks.post('/llm/{handle}')
async def voice_callback(handle:str,request:Request,authorization:str|None=Header(None)):
    voice=await bound_voice(request,handle,authorization)
    body=await request.json();messages=body.get('messages',[])
    user_messages=[m for m in messages if isinstance(m,dict) and m.get('role')=='user' and isinstance(m.get('content'),str)]
    if not user_messages or len(user_messages[-1]['content'])>3000:raise HTTPException(422,'无有效用户消息')
    rt=runtime(request);session=await rt.db('get',voice['payload']['session_id'],voice['owner_id'],'consultation')
    async def stream():
        yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{'role':'assistant'}}]})+'\n\n'
        try:
            async for item in answer_chunks(rt,session,user_messages[-1]['content']):
                if item['kind']=='text':
                    yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{'content':item['text']}}]},ensure_ascii=False)+'\n\n'
        except Exception:
            yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{'content':'资料暂时不可用，请稍后再试。'}}]},ensure_ascii=False)+'\n\n'
        yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})+'\n\n'
        yield 'data: [DONE]\n\n'
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache'})
