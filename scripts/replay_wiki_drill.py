"""Replay 80 synthetic Wiki cases through the existing backend without network access."""
import asyncio
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
PACKET = ROOT / 'knowledge' / 'wiki-drill'
RUNS = ROOT / 'evals' / 'runs'
OWNER = 'wiki-drill-80'
CATEGORIES = {'product': '商品规则', 'aftersales': '售后政策', 'faq': '常见问题'}
PHASES = ('新增', '补充', '冲突', '问答回流')
INTAKE = (
    ('文字客服', '咨询案例', '售后工单'),
    ('咨询案例', '售后工单', '文字客服'),
    ('售后工单', '文字客服', '咨询案例'),
    ('售后复问', '售后复问', '售后复问'),
)
os.environ.update(MOCK_MODE='true', DEEPSEEK_API_KEY='', FUSION_KB_MODE='sample')
sys.path.insert(0, str(ROOT / 'backend'))


def deny_network(event, _args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname'}:
        raise AssertionError(f'离线演练触发网络访问: {event}')


from app.fusion import api
from app.fusion.knowledge import KnowledgeService
from app.fusion.repository import Repository, content_hash
from app.fusion.support_wiki import WikiDraftInput, WikiReview, question_key


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def load_packet():
    dataset = json.loads((PACKET / 'scenarios.json').read_text(encoding='utf-8'))
    pages = {page['id']: page for page in dataset['pages']}
    questions = [question_key(q) for page in pages.values() for q in page['questions']]
    require(len(pages) == len(dataset['pages']) == 20 and len(questions) == len(set(questions)) == 80
            and all(len(page['questions']) == len(PHASES) for page in pages.values()),
            '素材须为 20 个主题和 80 条独立问法')
    links = {key: set() for key in pages}
    for left, right in dataset['links']:
        require(left in pages and right in pages and left != right and right not in links[left],
                f'无效关联: {left}, {right}')
        links[left].add(right)
        links[right].add(left)
    return dataset['date'], pages, links


def cite(page, part):
    return f"{page['id']}-{part}"


def page_link(page):
    return f"[{page['title']}]({CATEGORIES[page['category']]}.md#{page['id'].lower()})"


def case_link(case_id):
    return f'[{case_id}](案例与复核.md#{case_id.lower()})'


def excerpt(page, phase):
    basic, extra, feedback = page['notes']
    parts = [(cite(page, 'S1'), basic)]
    if phase >= 1:
        parts.append((cite(page, 'S2'), extra))
    if phase >= 2 and not page.get('pending'):
        parts.append((cite(page, 'S3'), page['review']))
    if phase == 3:
        parts.append((cite(page, 'S4'), feedback))
    return parts


def trace(turn):
    return {key: turn.get(key) for key in (
        'turn_id', 'answer_route', 'answer_model_skipped', 'wiki_asset_id', 'answer'
    )} | {'sources': [item['chunk_id'] for item in turn['sources']]}


async def replay(repo, knowledge, pages, links):
    # Start guarding after asyncio creates its internal Windows loopback socket.
    sys.addaudithook(deny_network)
    snapshot = knowledge.snapshot()
    sid = snapshot['knowledge_snapshot_id']
    chunks = {chunk['id']: chunk for chunk in snapshot['corpus']['chunks']}
    repo.initialize()
    repo.create('knowledge_snapshot', 'system', snapshot, record_id=sid)
    repo.activate_snapshot(sid)

    async def db(method, *args, **kwargs):
        return getattr(repo, method)(*args, **kwargs)

    rt = SimpleNamespace(db=db, repo=repo, knowledge=knowledge)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fusion=rt)))
    user = SimpleNamespace(id=OWNER)

    async def ask(question, owner=OWNER):
        session = repo.create('consultation', owner, {'channel': 'text', 'turns': []})
        stream = [part async for part in api.answer_chunks(rt, session, question)]
        return session, stream[-1]['turn']

    async def capture(session, turn, label):
        body = api.InsightInput(session_id=session['id'], turn_id=turn['turn_id'],
                               summary=turn['question'], confirmed=True)
        return await api.create_insight(body, request, user, idem=label)

    async def hit(question, asset, label):
        search_calls = knowledge.retrieve.await_count
        _, turn = await ask(question)
        expected_sources = [item['chunk_id'] for item in asset['evidence_refs']]
        require(
            turn['answer_route'] == 'support_wiki' and turn['answer_model_skipped']
            and turn['wiki_asset_id'] == asset['asset_id'] and turn['answer'] == asset['content']
            and trace(turn)['sources'] == expected_sources
            and knowledge.retrieve.await_count == search_calls,
            f"{label} 复问未原样复用审核版本: {trace(turn)}",
        )
        return trace(turn)

    cases = []
    checks = []
    tasks = {}
    active = {}
    original_evidence = {}
    baseline = {}

    # Capture all source turns against the same corpus before installing any Wiki.
    for index, page in enumerate(pages.values()):
        require(set(page['anchors']).issubset(chunks), f"{page['id']} 的原始出处不存在")
        for phase, question in enumerate(page['questions']):
            case_id = f'W{phase * len(pages) + index + 1:03}'
            session, turn = await ask(question)
            require(turn['answer_route'] != 'support_wiki', f'{case_id} 基线意外命中 Wiki')
            insight = await capture(session, turn, case_id)
            baseline[page['id'], phase] = (case_id, turn, insight)

    for phase, label in enumerate(PHASES):
        for index, page in enumerate(pages.values()):
            key = page['id']
            case_id, before, insight = baseline[key, phase]
            question = page['questions'][phase]
            row = {'id': case_id, 'page': key, 'kind': label, 'intake': INTAKE[phase][index % 3],
                   'question': question, 'insight_id': insight['insight_id'], 'before': trace(before)}
            cases.append(row)
            if phase == 0:
                body = api.TaskInput(insight_id=insight['insight_id'],
                                     topic_direction=page['title'], content_type='wiki')
                tasks[key] = await api.create_task(body, request, user, idem=case_id)
                available = {ref['chunk_id'] for ref in tasks[key]['evidence_refs']}
                original_evidence[key] = [item for item in page['anchors'] if item in available]
            if phase == 2 and page.get('pending'):
                _, withheld = await ask(question)
                require(withheld['answer_route'] != 'support_wiki', f'{case_id} 待核说法进入了 Wiki')
                row.update(status='挂起待补证', claim=page['claim'], decision=page['review'],
                           withheld=trace(withheld), after=await hit(page['questions'][0], active[key], case_id))
                continue
            if phase == 3:
                session, followup = await ask(question)
                require(followup['answer_route'] != 'support_wiki', f'{case_id} 回流前已被收录')
                row['before_feedback'] = trace(followup)
                feedback = await capture(session, followup, case_id + '-feedback')
                row['feedback_insight_id'] = feedback['insight_id']

            parts = excerpt(page, phase)
            supplement = {
                'title': '纯演练证据 ' + ' / '.join(code for code, _ in parts),
                'url': f'https://wiki.example.com/drill/{key.lower()}',
                'excerpt': '\n'.join(f'{code} {part}' for code, part in parts),
            }
            evidence_ids = original_evidence[key]
            if phase == 0:
                await api.draft_wiki(tasks[key]['task_id'], WikiDraftInput(
                    evidence_ids=evidence_ids, supplement=supplement), request, user)
                tasks[key] = api.public_task(repo.get(tasks[key]['task_id'], OWNER, 'task'))
            aliases = [q for i, q in enumerate(page['questions'][1:phase + 1], 1)
                       if i != 2 or not page.get('pending')]
            body = WikiReview(
                expected_state_version=tasks[key]['state_version'], title=page['title'],
                question=page['questions'][0], aliases=aliases,
                answer=''.join(part for _, part in parts), scope=page['scope'],
                steps=[part for _, part in parts],
                escalate_when=(page['review'] if page.get('pending') else
                               '对照后仍无法确认时，通过订单售后渠道交由人工核对。'),
                confirmed=True, evidence_ids=evidence_ids, supplement=supplement,
            )
            approved = await api.approve_wiki(tasks[key]['task_id'], body, request, user, idem=case_id)
            tasks[key] = approved['task']
            asset = approved['asset']
            if key in active:
                await hit(page['questions'][0], active[key], f'{case_id} 发布前')
            active[key] = await api.publish_asset(asset['asset_id'], api.VersionInput(
                expected_state_version=asset['state_version']), request, user, idem=case_id)
            row.update(status='演练已发布', decision=(
                page['review'] if phase == 2 else page['feedback'] if phase == 3 else parts[-1][1]
            ), after=await hit(question, active[key], case_id),
                accepted_sources=[code for code, _ in parts])
            if phase == 2:
                row['claim'] = page['claim']

        checks.append(lint(repo, pages, links, active, phase, cases, sid))
        print(f"{label}: 20 条，{checks[-1]['published']} 个当前版本，"
              f"{checks[-1]['superseded']} 个过时版本退出复用")

    final_hits = 0
    for row in cases:
        if row['status'] == '挂起待补证':
            _, turn = await ask(row['question'])
            require(turn['answer_route'] != 'support_wiki', f"{row['id']} 的争议问法在回流后命中")
            row['final_check'] = trace(turn)
        else:
            row['final_check'] = await hit(row['question'], active[row['page']], row['id'])
            final_hits += 1
    _, outside = await ask(next(iter(pages.values()))['questions'][0], owner='wiki-drill-outsider')
    require(outside['answer_route'] != 'support_wiki', '其他账号读到了演练账号的 Wiki')
    return {
        'knowledge_snapshot_id': sid, 'corpus_sha256': snapshot['corpus_sha256'],
        'corpus_chunk_count': len(chunks), 'cases': cases, 'lint': checks, 'active': active,
        'final_replay': {'wiki_hits': final_hits, 'held_out': len(cases) - final_hits,
                         'other_account_route': outside['answer_route']},
        'asset_history': [api.public_asset(a) for a in repo.list('asset', OWNER)],
        'links': {key: sorted(targets) for key, targets in links.items()},
    }, chunks


def lint(repo, pages, links, active, phase, cases, sid):
    valid = repo.valid_wiki(OWNER)
    live_ids = {row['id'] for row in valid}
    require(live_ids == {row['asset_id'] for row in active.values()}, '当前 Wiki 版本与发布记录不一致')
    keys = []
    for row in valid:
        payload = row['payload']
        keys.extend(payload['wiki']['question_keys'])
        require(payload['knowledge_snapshot_id'] == sid and all(
            content_hash(item['content']) == item['content_sha256'] for item in payload['evidence_refs']
        ), f"{row['id']} 来源或快照已失效")
    require(len(keys) == len(set(keys)), '出现跨主题的重复问法')
    for key, targets in links.items():
        require(key in active and targets and all(key in links[target] for target in targets),
                f'{key} 孤立或缺少反向链接')
    counts = Counter(row['payload']['publication_status'] for row in repo.list('asset', OWNER))
    closed = [row for row in cases if row['kind'] == '冲突' and row['status'] == '演练已发布']
    pending = [row for row in cases if row['status'] == '挂起待补证']
    require({row['page'] for row in pending} == (
        {key for key, page in pages.items() if page.get('pending')} if phase >= 2 else set()
    ), '待补证冲突有遗漏')
    for row in pending:
        page, asset = pages[row['page']], active[row['page']]
        require(question_key(row['question']) not in keys and page['review'] in asset['wiki']['escalate_when']
                and page['claim'] not in asset['content'], f"{row['id']} 的待核内容没有隔离")
    expected_versions = len(pages) * (phase + 1) - len(pending)
    require(counts['published'] == len(pages) and counts['superseded'] == expected_versions - len(pages)
            and sum(counts.values()) == expected_versions, '资产版本总数有误')
    return {'phase': PHASES[phase], 'published': len(valid), 'superseded': counts['superseded'],
            'orphans': 0, 'duplicate_questions': 0, 'resolved': len(closed),
            'pending': [row['id'] for row in pending], 'question_keys': len(keys)}


def render(date, pages, findings, chunks):
    documents = {}
    by_page = {key: [row for row in findings['cases'] if row['page'] == key] for key in pages}
    for category, title in CATEGORIES.items():
        out = [f'# {title}', '', '纯模拟演练条目。每条标准答复均取自隔离库的最新审核版本。',
               '', '[返回演练入口](README.md)', '']
        for key, page in pages.items():
            if page['category'] != category:
                continue
            wiki = findings['active'][key]['wiki']
            out += [f'<a id="{key.lower()}"></a>', f'## {key} {page["title"]}', '',
                    f'**标准问题**　{wiki["question"]}', '', f'**标准答复**　{wiki["answer"]}', '',
                    f'**适用范围**　{wiki["scope"]}', '', f'**升级条件**　{wiki["escalate_when"]}', '',
                    '**已审核复问**　' + ' / '.join(wiki['aliases']), '',
                    f'**语料缺口**　{page["gap"]}', '',
                    f'**依据**　[模拟来源及原始语料位置](证据索引.md#{key.lower()})', '',
                    '**关联专题**　' + ' · '.join(page_link(pages[target]) for target in findings['links'][key]), '',
                    '**版本轨迹**　' + ' · '.join(case_link(row['id']) for row in by_page[key]), '']
        documents[f'{title}.md'] = '\n'.join(out)

    out = ['# 证据索引', '', '原始语料用于核对已有知识和适用范围。S1–S4 是独立撰写的模拟材料。'
           '工作流使用保留的 `wiki.example.com` 演练地址，此页保存材料文本。',
           '', '[返回演练入口](README.md)', '']
    for key, page in pages.items():
        out += [f'<a id="{key.lower()}"></a>', f'## {key} {page["title"]}', '',
                f'返回 {page_link(page)}', '', '**原始语料**', '']
        for chunk_id in page['anchors']:
            chunk = chunks[chunk_id]
            out.append(f"- {chunk['source']} · {chunk['heading']} · `{chunk_id}`")
        out += ['', '**模拟依据与冲突来源**', '',
                f"- {cite(page, 'S1')} 模拟检查单。{page['notes'][0]}",
                f"- {cite(page, 'S2')} 模拟复测纪要。{page['notes'][1]}",
                f"- {cite(page, 'C1')} 未采信说法。{page['claim']}",
                f"- {cite(page, 'S3')} 模拟复核记录。{page['review']}",
                f"- {cite(page, 'S4')} 模拟回访确认。{page['notes'][2]}", '']
    documents['证据索引.md'] = '\n'.join(out)

    out = ['# 80 条案例与复核', '', f'演练日期 {date}（北京时间）。以下按主题排列，编号按演练批次递增。',
           '基线路由来自版本 `ecommerce-v2` 的本地字面检索及应用规则。',
           '基线能召回规格或规则时，按主题的证据索引复核它是否足以回答具体情形。',
           '', '[返回演练入口](README.md)', '']
    for key, page in pages.items():
        out += [f'## {key} {page["title"]}', '', f'返回 {page_link(page)}', '']
        for row in by_page[key]:
            out += [f'<a id="{row["id"].lower()}"></a>',
                    f"### {row['id']} {row['kind']} · {row['status']}", '',
                    f"**{row['intake']}原话**　{row['question']}", '',
                    f"**基线路由**　`{row['before']['answer_route']}`", '']
            if 'accepted_sources' in row:
                accepted = '、'.join(row['accepted_sources'])
                out += [f'**采信依据**　{accepted}。[出处](证据索引.md#{key.lower()})', '']
            if 'claim' in row:
                out += [f"**分歧材料**　{row['claim']}", '']
            if 'before_feedback' in row:
                out += [f"**回写前实测**　新问法走 `{row['before_feedback']['answer_route']}`。", '']
            out += [f"**模拟复核**　{row['decision']}", '']
            after = row['after']
            if 'withheld' in row:
                out += [f"**挂起实测**　原问题走 `{row['withheld']['answer_route']}`。以下答复来自已批准范围内的标准问题。", '']
            out += [f"**Agent 复测**　`{after['answer_route']}` · 跳过检索和回答模型",
                    '', f"> {after['answer']}", '']
    documents['案例与复核.md'] = '\n'.join(out)

    out = ['# Wiki 自检记录', '', f'演练日期 {date}（北京时间）。每批处理完成后执行自检。',
           '', '[返回演练入口](README.md)', '']
    for check in findings['lint']:
        out += [f"## {check['phase']}批次", '',
                f"- 当前版本 {check['published']} 个，可直答问法 {check['question_keys']} 条。",
                f"- 累计退出复用的旧版本 {check['superseded']} 个，保留历史与出处。",
                f"- 孤立主题 {check['orphans']} 个，跨主题重复问法 {check['duplicate_questions']} 条。",
                f"- 已解决冲突 {check['resolved']} 条，挂起待补证 {len(check['pending'])} 条。"]
        if check['pending']:
            out += ['- 待补证 ' + ' · '.join(case_link(case_id) for case_id in check['pending']) + '。']
        out += ['']
    final = findings['final_replay']
    out += ['## 结案复放', '',
            f"- {final['wiki_hits']} 条已审核问法全部命中最新版本并跳过检索和回答模型。",
            f"- {final['held_out']} 条待补证问法均未命中 Wiki。有效主题仍保留各自已审范围。",
            '- 另用不同账号测试，确认读不到演练账号的 Wiki。', '',
            '逐批校对知识快照和引用摘要的哈希。生成页面时另检查所有内部链接的目标。']
    documents['自检记录.md'] = '\n'.join(out)
    return documents


def write_documents(documents):
    paths = {**documents, 'README.md': (PACKET / 'README.md').read_text(encoding='utf-8')}
    for name, body in paths.items():
        for target, anchor in re.findall(r'\]\(([^)#]+\.md)(?:#([\w-]+))?\)', body):
            require(target in paths and (not anchor or f'id="{anchor}"' in paths[target]),
                    f'链接失效: {name} -> {target}#{anchor}')
    for name, body in documents.items():
        (PACKET / name).write_text(body.rstrip() + '\n', encoding='utf-8')


def main():
    date, pages, links = load_packet()
    RUNS.mkdir(parents=True, exist_ok=True)
    knowledge = KnowledgeService(PACKET / 'snapshots', mode='sample')
    repo = Repository('sqlite:///:memory:')
    try:
        with (
            patch.object(api, 'LLMService', side_effect=AssertionError('调用了回答模型')),
            patch.object(api, 'answer_knowledge_gap', side_effect=AssertionError('调用了 DeepSeek 补答')),
            patch.object(api, 'classify_dialogue_intent', side_effect=AssertionError('调用了 DeepSeek 意图识别')),
            patch.object(knowledge, 'retrieve', AsyncMock(wraps=knowledge.retrieve)),
        ):
            findings, chunks = asyncio.run(replay(repo, knowledge, pages, links))
    finally:
        repo.close()
    write_documents(render(date, pages, findings, chunks))
    (RUNS / 'wiki-drill.json').write_text(json.dumps(findings, ensure_ascii=False, indent=2) + '\n',
                                         encoding='utf-8')
    print(f'通过 {len(findings["cases"])} 条案例。详细结果：{RUNS / "wiki-drill.json"}')


if __name__ == '__main__':
    main()
