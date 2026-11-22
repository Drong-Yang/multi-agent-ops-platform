"""Read the active knowledge snapshot without invoking the answer model."""

import asyncio

from app.fusion.privacy import sanitize
from app.fusion.repository import Invalid


class KnowledgeUnavailable(Exception):
    """The active snapshot or its retriever is unavailable."""


async def active_snapshot_id(rt) -> str:
    active = await rt.db('get_active_snapshot')
    if not active:
        raise KnowledgeUnavailable('知识快照尚未就绪')
    return active['id']


async def search_knowledge(rt, query: str, sku: str | None = None) -> dict:
    query = query.strip()
    if not 1 <= len(query) <= 3000:
        raise Invalid('检索问题须为 1～3000 字符')
    query = sanitize(query)
    if sku is not None:
        sku = sanitize(sku)
        if not 1 <= len(sku) <= 64:
            raise Invalid('商品型号须为 1～64 字符')
        if sku.casefold() not in query.casefold():
            query = f'{sku} {query}'

    try:
        async with asyncio.timeout(5):
            snapshot_id = await active_snapshot_id(rt)
            result = await rt.knowledge.retrieve(
                query, snapshot_id=snapshot_id,
                product_scope=[sku] if sku is not None else None,
            )
    except KnowledgeUnavailable:
        raise
    except Exception as exc:
        raise KnowledgeUnavailable('当前知识检索暂不可用，请稍后重试') from exc

    return {
        'knowledge_snapshot_id': snapshot_id,
        'retrieval_mode': result['retrieval_mode'],
        'sources': [
            {
                'chunk_id': item['chunk_id'],
                'source': sanitize(item['source']),
                'source_section': sanitize(item['source_section']),
                'sku': sanitize(item['sku']),
                'kind': item['kind'],
                'content': sanitize(item['content']),
            }
            for item in result['sources']
        ],
    }
