"""Frozen business inputs and expected outcomes for the collaboration replay."""
from itertools import product

from app.fusion.repository import content_hash

PRODUCTS = (
    ("SC-PB27", "ecommerce_demo_v1_1_3", "568g", "329", "丛林绿"),
    ("SC-PB05", "ecommerce_demo_v1_1_0", "128g", "129", "曜石黑"),
    ("SC-PB10", "ecommerce_demo_v1_1_1", "215g", "169", "海军蓝"),
    ("SC-PB20", "ecommerce_demo_v1_1_2", "415g", "229", "雪山白"),
    ("SC-CG20", "ecommerce_demo_v1_1_5", "38g", "79", "白"),
    ("SC-CG65", "ecommerce_demo_v1_1_6", "92g", "149", "黑"),
    ("SC-CG100", "ecommerce_demo_v1_1_7", "285g", "299", "深空灰"),
    ("SC-WM15", "ecommerce_demo_v1_1_11", "68g", "99", "黑"),
)
WIKI_TOPICS = (
    ("SC-PB27", "无法充电", "重新插紧随机充电线"),
    ("SC-PB05", "磁吸充电中断故障", "取下保护壳后检查对齐"),
    ("SC-PB10", "数显指示灯不亮", "记录电量后按一次电源键"),
    ("SC-PB20", "无法连接笔记本", "核对供电接口与线材标识"),
    ("SC-CG20", "充电器无法充电", "断电后换用配套线缆"),
    ("SC-CG65", "双口充电自动断开", "退出双口模式并单口测试"),
    ("SC-CG100", "功率数显异常故障", "断开负载后重新连接单口"),
    ("SC-WM15", "磁吸充电不稳定故障", "检查线圈位置并重新放置"),
    ("EW-Buds1", "耳机无法配对", "从设备列表移除旧配对"),
    ("EW-Buds2", "耳机无法连接", "将耳机放回盒中重新取出"),
    ("EW-Mini2", "音箱无法连接", "解除旧设备连接后重试"),
    ("EW-Watch1", "手表同步故障", "重新启动配套应用"),
)
WIKI_ACTIONS = {
    "publish": "审核后发布，原问题及审核别名引用同一版本",
    "replace": "发布新版后只复用新版，原轮次仍可核对旧证据",
    "revoke": "撤回后在原会话复问，保留已落库的历史引用",
    "snapshot": "切换到独立语料快照，旧页置 stale 并停止复用",
    "reject": "检查他人账号看不到已发布条目，且缺证或错型号的另一草稿无法审核",
}
CREATE_ACTIONS = {
    "approve": "创建任务，选题，审核并生成仅供审核使用的资产",
    "reject": "创建任务，退回稿件，重写后保留父版本与原证据",
    "private": "交接仅暴露当前用户的会话与线索，其他账号不可读",
    "retry": "创作检索超时，保留原请求键，恢复后只创建一项任务",
    "duplicate": "同键重复领取选题推进请求，保留唯一后继运行",
}
TOOLS = {
    "knowledge": ["search_knowledge"],
    "operations": ["create_wiki_draft", "search_knowledge"],
    "maintenance": ["get_reindex_job", "request_reindex"],
}
RACE_ACTIONS = {
    "submit": "用屏障同时提交两个幂等事务，核对唯一副作用与请求冲突",
    "claim": "用屏障从同一版本同时领取运行，核对一个提交与一个冲突",
    "review": "同一 Wiki 版本并发审核，核对最终任务和资产",
    "draft_review": "草稿生成停在模型调用边界，交错审核或发布并核对状态版本",
    "publish": "并发发布同一问题下的资产，核对唯一有效 Wiki",
    "index_failure": "故障注入后检查指针，从同一目标以新请求恢复",
    "index_superseded": "索引同步暂停时推进数据库指针，旧任务不得激活",
    "pointer": "延迟一次成功激活的返回，同时发起后续激活，内存与数据库必须一致",
    "rollback": "事务写入后抛出失败，再用原键重试，核对数据库回滚",
    "recover": "执行状态不确定时重建运行视图，核对终态且不自动重放",
}


def case(case_id, family, cluster, action, state, *, held=False, route=None, refs=(),
         title="", repeat=False, **inputs):
    return {
        "case_id": case_id, "family": family, "cluster": cluster,
        "split": "holdout" if held else "development", "source_type": "synthetic",
        "preconditions": ["isolated database", "synthetic accounts", "pinned ecommerce-v2 corpus", "mock providers"],
        "steps": [title or action], "expected_route": route, "expected_state": state,
        "expected_evidence_ids": list(refs), "severity": "high" if family != "routing" else "medium",
        "action": action, "inputs": inputs, "repeat": repeat,
    }


def wiki_source(sku, excerpt):
    url = f"https://support.example.com/manual/{sku.lower()}"
    return {"title": f"{sku} 合成手册", "url": url, "excerpt": f"测试步骤记录：{excerpt}。"}


def wiki_ref(sku, excerpt):
    source = wiki_source(sku, excerpt)
    return "wiki_source_" + content_hash(source["url"] + source["excerpt"])[:24]


def routing_cases():
    for i, (sku, ref, weight, price, color) in enumerate(PRODUCTS, 1):
        for field, query, fragment, confirmed in (
            ("weight", "多重？", weight, True), ("price", "多少钱？", price, False),
            ("color", "有什么颜色？", color, True), ("mixed", "多重，保修多久？", f"{weight[:-1]} 克", True),
        ):
            yield case(f"RO-{i:02d}-{field}", "routing", sku, "consult", "saved" if confirmed else "unconfirmed",
                       held=i in (4, 7), route="rag" if field == "mixed" else "catalog_quote",
                       refs=[ref], title=f"{sku} {query}",
                       messages=[f"{sku} {query}"], fragment=fragment, confirmed=confirmed,
                       channel="voice" if i in (1, 2) and field == "weight" else "text")
    for i, (group, question, route, fragment) in enumerate((
        ("private-order", "帮我查订单物流到哪里了", "business_boundary", "订单"),
        ("private-order", "把我的收货地址改一下", "business_boundary", "订单"),
        ("private-order", "帮我取消订单", "business_boundary", "订单"),
        ("private-order", "帮我查看退款进度", "business_boundary", "订单"),
        ("private-order", "帮我申请退款", "business_boundary", "订单"),
        ("assistant", "你是谁？", "assistant_intro", "客服"),
        ("unknown-model", "SC-PB90 多重？", "no_evidence", "资料"),
        ("unknown-model", "EW-Buds99 是什么颜色？", "no_evidence", "资料"),
        ("insufficient", "SC-PB90 充电失败怎么办？", "no_evidence", "资料"),
        ("insufficient", "SC-PB91 无法连接怎么办？", "no_evidence", "资料"),
        ("insufficient", "SC-CG99 充电异常怎么处理？", "no_evidence", "资料"),
        ("insufficient", "EW-Buds98 无法配对怎么办？", "no_evidence", "资料"),
        ("safety", "充电宝鼓包了还能继续用吗？", "safety_incident", "停止"),
        ("safety", "充电宝冒烟了怎么处理？", "safety_incident", "远离"),
    ), 1):
        yield case(f"RO-X{i:02d}", "routing", group, "consult", "unconfirmed",
                   held=group == "unknown-model", route=route, title=question, messages=[question],
                   fragment=fragment, confirmed=False, channel="text")
    for i, (previous, current) in enumerate(((0, 1), (1, 3), (4, 5), (6, 7)), 1):
        sku, ref, weight, *_ = PRODUCTS[current]
        messages = [f"{PRODUCTS[previous][0]} 多重？", f"{sku} 多重？", "这个多重？"]
        yield case(f"RO-SW{i:02d}", "routing", "product-switch", "consult", "saved",
                   held=True, route="catalog_quote", refs=[ref], title=" → ".join(messages),
                   messages=messages, fragment=weight, confirmed=True, channel="text")


def wiki_cases():
    for i, (sku, issue, excerpt) in enumerate(WIKI_TOPICS, 1):
        for action, title in WIKI_ACTIONS.items():
            terminal = {"publish": "published", "replace": "superseded", "revoke": "revoked",
                        "snapshot": "stale", "reject": "waiting_human"}[action]
            refs = [wiki_ref(sku, excerpt + "并重新核对指示灯" if action == "replace" else excerpt)]
            yield case(f"WI-{i:02d}-{action}", "wiki", sku, action, terminal, held=i in (4, 9, 12),
                       route="support_wiki" if action in ("publish", "replace") else "exclude_wiki",
                       refs=refs if action in ("publish", "replace") else [],
                       title=f"{sku} {issue}｜{title}", sku=sku, issue=issue, excerpt=excerpt,
                       invalid="model" if i % 2 else "evidence")


def creation_cases():
    for i, (sku, ref, weight, *_rest) in enumerate(PRODUCTS, 1):
        for action, title in CREATE_ACTIONS.items():
            terminal = {"approve": "text_approved", "reject": "waiting_human", "private": "queued",
                        "retry": "queued", "duplicate": "queued"}[action]
            yield case(f"CR-{i:02d}-{action}", "creation", sku, action, terminal, held=i in (4, 7),
                       route="catalog_quote", refs=[ref], title=f"{sku}｜{title}",
                       sku=sku, weight=weight, content_type="faq" if i % 2 else "article")


def mcp_cases():
    scopes = tuple(TOOLS)
    for source, target in product(scopes, repeat=2):
        yield case(f"MC-S-{source}-{target}", "mcp", f"scope-{source}", "scope",
                   200 if source == target else 403, held=source == "operations",
                   title=f"{source} 凭证初始化 {target} 端点", source=source, target=target)
    for scope, credential in product(scopes, ("missing", "expired", "application", "invalid", "revoked_operator")):
        expected = 403 if credential == "revoked_operator" and scope == "maintenance" else (
            200 if credential == "revoked_operator" else 401)
        yield case(f"MC-T-{scope}-{credential}", "mcp", f"credential-{credential}", "credential", expected,
                   held=credential in ("expired", "application"), title=f"{scope} 对 {credential} 凭证的准入",
                   scope=scope, credential=credential)
    for scope in scopes:
        yield case(f"MC-D-{scope}", "mcp", "tool-discovery", "discover", TOOLS[scope], held=True,
                   title=f"枚举 {scope} 的工具集合", scope=scope)
    for kind in ("own", "foreign", "unknown_evidence", "empty_evidence", "duplicates", "repeat", "nonwiki", "stale_snapshot"):
        yield case(f"MC-O-{kind}", "mcp", "draft-ownership-evidence", "draft",
                   "draft_only" if kind in ("own", "repeat", "duplicates") else "denied",
                   refs=["ecommerce_demo_v1_1_3"] if kind in ("own", "repeat", "duplicates") else [],
                   title=f"运营工具创建待审草稿｜{kind}", kind=kind)
    for kind in ("grant", "empty_allowlist", "deny_grant", "queue", "replay", "different_body", "foreign_job", "unknown_job"):
        expected = {"grant": 200, "empty_allowlist": 403, "deny_grant": 403,
                    "queue": "queued", "replay": "queued"}.get(kind, "denied")
        yield case(f"MC-M-{kind}", "mcp", "maintenance-job", "maintain", expected,
                   title=f"运维凭证发放或索引任务｜{kind}", kind=kind)
    for kind in ("no_publish_tool", "knowledge_no_draft", "maintenance_no_draft", "unreviewed",
                 "foreign_publish", "stale_version", "missing_key"):
        expected = {"unreviewed": 409, "foreign_publish": 404, "stale_version": 409, "missing_key": 422}.get(kind, "denied")
        yield case(f"MC-P-{kind}", "mcp", "publication-boundary", "publish_guard", expected,
                   title=f"直接发布与工具边界｜{kind}", kind=kind)


RACE_VARIANTS = {
    "submit": ("identical", "different_body", "different_owner", "different_action"),
    "claim": ("queued_start", "queued_rewrite", "queued_retry", "queued_image"),
    "review": ("same_key", "different_keys", "different_bodies", "missing_evidence"),
    "draft_review": ("review", "publish_old", "same_basis", "different_basis"),
    "publish": ("same_asset", "same_question", "alias_overlap", "different_owners"),
    "index_failure": ("sync_timeout", "sync_error", "not_ready", "readiness_timeout"),
    "index_superseded": ("changed_target", "aba", "changed_before_sync", "same_target"),
    "pointer": ("two_forward", "return_original", "three_forward", "cancel_after_commit"),
    "rollback": ("insight", "task", "asset", "run"),
    "recover": ("active_run", "superseded_run", "waiting_human", "text_approved"),
}
RACE_STATES = {
    "submit": "deduplicated", "claim": "single_claim", "review": "one_approved",
    "draft_review": "winner_preserved", "publish": "unique_per_owner", "index_failure": "recovered",
    "index_superseded": "superseded", "pointer": "aligned", "rollback": "retried_once",
    "recover": "not_replayed",
}


def race_cases():
    for action, variants in RACE_VARIANTS.items():
        for i, variant in enumerate(variants):
            yield case(f"RC-{action}-{variant}", "recovery", action, action, RACE_STATES[action],
                       held=action in ("index_superseded", "recover", "rollback"),
                       repeat=i == 0 or variant == "same_basis" or (action == "pointer" and i in (1, 3)),
                       title=f"{RACE_ACTIONS[action]}｜{variant}", variant=variant)


BUILDERS = (routing_cases, wiki_cases, creation_cases, mcp_cases, race_cases)
REPRESENTATIVES = {
    "RO-01-weight", "RO-X02", "WI-01-publish", "WI-01-revoke", "CR-01-approve",
    "MC-S-knowledge-operations", "MC-O-foreign", "RC-pointer-two_forward",
    "RC-submit-identical", "RC-index_failure-sync_timeout", "RC-draft_review-same_basis",
}
