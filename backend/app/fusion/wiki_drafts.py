"""Persist reviewable support Wiki drafts for a caller's own content tasks."""

import asyncio

from app.fusion.repository import Conflict, Invalid
from app.fusion.runtime import mode
from app.fusion.support_wiki import WikiDraftInput, prepare_wiki_review, suggest_wiki, wiki_evidence


class WikiDraftUnavailable(Exception):
    """The draft provider could not produce a reviewable draft."""


async def create_wiki_draft(rt, owner_id: str, task_id: str, body: WikiDraftInput) -> dict:
    pending = getattr(rt, 'wiki_draft_requests', None)
    if pending is None:
        return await _generate_draft(rt, owner_id, task_id, body)
    key = (owner_id, task_id, body.model_dump_json())
    generation = pending.get(key)
    if generation is None:
        generation = asyncio.create_task(_generate_draft(rt, owner_id, task_id, body))
        pending[key] = generation

        def forget(completed):
            pending.pop(key)
            if not completed.cancelled():
                completed.exception()

        generation.add_done_callback(forget)
    return await asyncio.shield(generation)


async def _generate_draft(rt, owner_id, task_id, body):
    task = await rt.db('get', task_id, owner_id, 'task')
    payload = task['payload']
    if payload.get('content_type') != 'wiki':
        raise Invalid('请选择售后 Wiki 任务')
    snapshot_id, evidence = await asyncio.to_thread(
        wiki_evidence, rt.repo, rt.knowledge, payload, body.evidence_ids, body.supplement,
    )
    inputs = body.model_dump(mode='json')
    if body.related_ids:
        pages = await rt.db('valid_wiki', owner_id)
        related = {page['id'] for page in pages if page['payload']['task_id'] != task_id}
        inputs['related_ids'] = [identity for identity in body.related_ids if identity in related]
    basis = {'snapshot_id': snapshot_id, **inputs}
    cached = payload.get('wiki_draft') or {}
    if cached.get('basis') == basis:
        return {**cached['fields'], 'state_version': task['state_version']}

    try:
        suggestion = await asyncio.wait_for(
            suggest_wiki(payload['source_question'], evidence, mock=mode() == 'mock'), timeout=25,
        )
        fields = prepare_wiki_review(payload['source_question'], inputs, suggestion)
    except Exception as exc:
        raise WikiDraftUnavailable('暂未生成可审核的 Wiki 草稿，请重试或手动整理') from exc

    pending = {'basis': basis, 'fields': fields}
    try:
        saved = await rt.db('update_cas', task_id, owner_id, task['state_version'], {**payload, 'wiki_draft': pending})
    except Conflict:
        saved = await rt.db('get', task_id, owner_id, 'task')
        newer = saved['payload'].get('wiki_draft') or {}
        if newer.get('basis') != basis:
            raise Conflict('任务已变化，请重新打开任务后继续审核')
        fields = newer['fields']
    return {**fields, 'state_version': saved['state_version']}
