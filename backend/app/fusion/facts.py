"""Deterministic, finite SKU/numeric/policy checks, independently scanning prose.

This is not a general semantic truth judge. Unknown/ambiguous target claims block
publishing instead of being treated as verified. A bundle must originate from a
trusted snapshot; build_fact_bundle is also useful for explicit offline fixtures.
"""
import copy
import hashlib
import json
import re
import unicodedata
from decimal import Decimal

SKU_RE = re.compile(r"(?<![A-Za-z0-9])(?:SC|EW)-[A-Za-z]+\d+[A-Za-z]*(?![A-Za-z0-9])", re.I)
NUMBER = r"\d+(?:\.\d+)?"
WEIGHT_RE = re.compile(r"(" + NUMBER + r")\s*(kg|mg|g|千克|公斤|毫克|克)(?![A-Za-z])", re.I)
CAPACITY_RE = re.compile(r"(" + NUMBER + r")\s*(mAh|毫安时)(?![A-Za-z])", re.I)
PRICE_RE = re.compile(r"(" + NUMBER + r")\s*元")
WARRANTY_RE = re.compile(r"(?:保修|质保)\s*[:：为期约]*\s*(" + NUMBER + r")\s*(个月|月|年)")
DIMENSION_RE = re.compile(r"(" + NUMBER + r")\s*[×xX*]\s*(" + NUMBER + r")\s*[×xX*]\s*(" + NUMBER + r")\s*(mm|cm|毫米|厘米)", re.I)
FACT_RE = re.compile(r"(" + NUMBER + r")\s*(Wh|W|V|mm|cm)(?![A-Za-z])", re.I)


def _digest(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text):
    return unicodedata.normalize("NFKC", text).replace("**", "")


def _weight_g(value, unit):
    multipliers = {"kg": "1000", "千克": "1000", "公斤": "1000", "g": "1", "克": "1", "mg": ".001", "毫克": ".001"}
    return float(Decimal(value) * Decimal(multipliers[unit.lower()]))


def extract_product_facts(content):
    text = _clean(content)
    facts = {}
    weight = WEIGHT_RE.search(text)
    if weight:
        facts["weight_g"] = _weight_g(*weight.groups())
    for match in CAPACITY_RE.finditer(text):
        field = "rated_capacity_mah" if "额定容量" in text[max(0, match.start() - 12):match.start()] else "capacity_mah"
        facts.setdefault(field, float(match.group(1)))
    price = PRICE_RE.search(text)
    if price:
        facts["price_cny"] = float(price.group(1))
    warranty = WARRANTY_RE.search(text)
    if warranty:
        facts["warranty_months"] = float(warranty.group(1)) * (12 if warranty.group(2) == "年" else 1)
    dimensions = DIMENSION_RE.search(text)
    if dimensions:
        factor = 10 if dimensions.group(4).lower() in ("cm", "厘米") else 1
        facts["dimensions_mm"] = [float(dimensions.group(i)) * factor for i in (1, 2, 3)]
    # Preserve supported values with their units. No inference of undocumented ratings.
    values = {}
    for match in FACT_RE.finditer(text):
        unit = match.group(2).lower()
        values.setdefault(unit, [])
        value = float(match.group(1))
        if value not in values[unit]:
            values[unit].append(value)
    facts["numeric_values"] = values
    for direction in ("input", "output"):
        label = "输入" if direction == "input" else "输出"
        directional = []
        for line in text.splitlines():
            if re.search(r"(?:^|-)\s*" + label + r"\s*[:：]", line):
                directional.extend(float(m.group(1)) for m in re.finditer(r"(" + NUMBER + r")\s*W(?![A-Za-z])", line, re.I))
        if directional:
            facts[direction + "_w"] = sorted(set(directional))
    return facts


def build_fact_bundle(snapshot_id, evidence):
    """Build from trusted, server-selected evidence; does not authorize chunk IDs.

Use KnowledgeService.fact_bundle for untrusted external reference submissions: it
additionally resolves every chunk against the immutable snapshot registry.
"""
    verified = []
    for source in evidence:
        source = copy.deepcopy(source)
        content = source.get("content", "")
        if not isinstance(content, str):
            raise ValueError("证据正文必须为文本")
        sha = _digest(content)
        if any(source.get(k, sha) != sha for k in ("content_sha256", "hash")):
            raise ValueError("证据正文哈希不符")
        if source.get("knowledge_snapshot_id", snapshot_id) != snapshot_id:
            raise ValueError("证据知识版本不符")
        source["knowledge_snapshot_id"] = snapshot_id
        source["chunk_id"] = source.get("chunk_id", source.get("id", ""))
        if not source["chunk_id"]:
            raise ValueError("证据缺少知识单元ID")
        source["content_sha256"] = source["hash"] = sha
        source["sku"] = source.get("sku", "").upper()
        source["kind"] = source.get("kind", "product" if source["sku"] else "rule")
        verified.append(source)
    products = {s["sku"]: extract_product_facts(s["content"]) for s in verified if s["kind"] == "product" and s["sku"]}
    bundle = {"schema_version": "1.0", "knowledge_snapshot_id": snapshot_id,
              "evidence": verified, "sources": verified, "supported_skus": sorted(products),
              "products": products, "facts": products,
              "policy_evidence": [s for s in verified if s["kind"] == "rule"], "scope": "sku_numeric_policy_v1"}
    bundle["bundle_sha256"] = _digest(bundle)
    return bundle


def check_facts(content, bundle, evidence_refs=None):
    if not isinstance(content, str) or not isinstance(bundle, dict):
        raise ValueError("正文与事实包格式错误")
    checks, issues = [], []
    evidence = bundle.get("evidence", bundle.get("sources", []))
    products = bundle.get("products", bundle.get("facts", {}))
    text = _clean(content)

    def issue(field, reason, status="unsupported", **extra):
        item = {"field": field, "reason": reason, "status": status, **extra}
        if item not in issues:
            issues.append(item)

    if bundle.get("bundle_sha256"):
        unsigned = {k: v for k, v in bundle.items() if k != "bundle_sha256"}
        if _digest(unsigned) != bundle["bundle_sha256"]:
            issue("evidence", "事实包完整性校验失败")
    allowed_ids = {s.get("chunk_id", s.get("id")) for s in evidence}
    for source in evidence:
        sha = _digest(source.get("content", ""))
        if any(source.get(k, sha) != sha for k in ("content_sha256", "hash")):
            issue("evidence", "证据正文哈希不符")
    refs = evidence_refs
    if refs is not None:
        chosen_ids = set()
        for ref in refs:
            ref_id = ref if isinstance(ref, str) else ref.get("chunk_id", ref.get("id"))
            chosen_ids.add(ref_id)
            source = next((s for s in evidence if s.get("chunk_id", s.get("id")) == ref_id), None)
            if source is None:
                issue("evidence", "未知或伪造引用", reference=ref_id)
            elif isinstance(ref, dict):
                for key in ("content_sha256", "hash", "knowledge_snapshot_id", "content"):
                    if key in ref and ref[key] != source.get(key):
                        issue("evidence", "引用内容或版本不符", reference=ref_id)
        if not refs and (SKU_RE.search(text) or WEIGHT_RE.search(text) or CAPACITY_RE.search(text)):
            issue("evidence", "关键事实缺少引用")
        chosen_evidence = [s for s in evidence if s.get("chunk_id", s.get("id")) in chosen_ids]
        chosen_skus = {s.get("sku", "").upper() for s in chosen_evidence if s.get("kind") == "product"}
        products = {sku: value for sku, value in products.items() if sku in chosen_skus}
    else:
        chosen_evidence = evidence
    # Also inspect references present only in generated text, not only declared refs.
    for ref in re.findall(r"\[\[([^\]\n]+)\]\]|\[(ecommerce_demo_[^\]\n]+)\]", text):
        ref_id = next((value for value in ref if value), "")
        if ref_id not in allowed_ids:
            issue("evidence", "正文含未知引用", reference=ref_id)
    for ref_id in re.findall(r"\[证据\s*[:：]\s*([^\]\n]+)\]", text):
        if ref_id.strip() not in allowed_ids:
            issue("evidence", "正文含未知引用", reference=ref_id.strip())

    for match in SKU_RE.finditer(text):
        sku = match.group().upper()
        if sku not in products:
            issue("sku", "正文型号没有基础证据", sku=sku)
        else:
            checks.append({"field": "sku", "sku": sku, "passed": True})

    def compare(field, value, skus, display):
        if not skus:
            issue(field, "多商品事实缺少明确型号归属", "needs_review", actual=value)
            return
        for sku in skus:
            if sku not in products:
                issue(field, "型号或事实缺少证据", sku=sku, actual=value)
                continue
            expected = products[sku].get(field)
            if expected is None:
                issue(field, "基础资料未覆盖此字段", sku=sku, actual=value)
                continue
            passed = expected == value
            checks.append({"field": field, "sku": sku, "actual": value, "expected": expected, "passed": passed})
            if not passed:
                issue("weight" if field == "weight_g" else field, "正文数值与基础资料冲突", "conflict", sku=sku, actual=value, expected=expected, text=display)

    previous_skus = []
    for sentence in re.split(r"[。；;\n]", text):
        mentions = list(SKU_RE.finditer(sentence))
        sentence_skus = list(dict.fromkeys(m.group().upper() for m in mentions))
        # A verbatim general rule (e.g. "PD high power, such as 65W") is not
        # a new rating for the product mentioned in an earlier paragraph. This
        # exception requires the complete visible clause in an actual selected
        # rule, and never applies to an explicit SKU claim or a paraphrased value.
        clause = re.sub(r"\[证据\s*[:：][^\]\n]+\]", "", sentence).strip()
        quoted_rule = next((source for source in chosen_evidence
                            if source.get("kind") == "rule" and clause
                            and clause in _clean(source.get("content", ""))), None) if not mentions else None

        def owners(position):
            before = [m for m in mentions if m.end() <= position]
            if len(before) >= 2:
                connector = sentence[before[-2].end():before[-1].start()]
                after = sentence[before[-1].end():position]
                if re.fullmatch(r"\s*(?:与|和|及|、|以及|/)\s*", connector) and re.search(r"均|都|各", after):
                    return [before[-2].group().upper(), before[-1].group().upper()]
            if before:
                return [before[-1].group().upper()]
            if len(sentence_skus) == 1:
                return sentence_skus
            if not sentence_skus and len(previous_skus) == 1:
                return previous_skus
            if not sentence_skus and len(products) == 1:
                return list(products)
            return []

        dimensions_ranges = []
        for match in DIMENSION_RE.finditer(sentence):
            factor = 10 if match.group(4).lower() in ("cm", "厘米") else 1
            dimensions = [float(match.group(i)) * factor for i in (1, 2, 3)]
            compare("dimensions_mm", dimensions, owners(match.start()), match.group())
            dimensions_ranges.append(match.span())
        for match in WEIGHT_RE.finditer(sentence):
            compare("weight_g", _weight_g(*match.groups()), owners(match.start()), match.group())
        for match in re.finditer(r"(?:净重|重量|重)\s*(?:为|约|只有|仅|是|:|：)?\s*([零一二三四五六七八九十百千万两\d.]+)\s*([a-zA-Z\u4e00-\u9fff]{1,4})", sentence):
            if WEIGHT_RE.fullmatch(match.group(1) + match.group(2)) is None:
                issue("weight", "重量表达超出已实现的数字和单位白名单", "needs_review", text=match.group())
        for match in CAPACITY_RE.finditer(sentence):
            prefix = sentence[max(0, match.start() - 12):match.start()]
            # A comma starts a new field; an earlier "额定容量" must not leak.
            prefix = re.split(r"[，,]", prefix)[-1]
            field = "rated_capacity_mah" if "额定容量" in prefix else "capacity_mah"
            compare(field, float(match.group(1)), owners(match.start()), match.group())
        for match in PRICE_RE.finditer(sentence):
            compare("price_cny", float(match.group(1)), owners(match.start()), match.group())
        for match in WARRANTY_RE.finditer(sentence):
            compare("warranty_months", float(match.group(1)) * (12 if match.group(2) == "年" else 1), owners(match.start()), match.group())
        for match in FACT_RE.finditer(sentence):
            if any(start <= match.start() < end for start, end in dimensions_ranges):
                continue
            if quoted_rule:
                checks.append({"field": "generic_rule_numeric", "reference": quoted_rule["chunk_id"],
                               "actual": match.group(), "passed": True})
                continue
            unit, value = match.group(2).lower(), float(match.group(1))
            skus = owners(match.start())
            if not skus:
                issue("numeric", "数值缺少明确商品归属", "needs_review", actual=match.group())
            for sku in skus:
                supported = products.get(sku, {}).get("numeric_values", {}).get(unit, [])
                if unit == "w":
                    prefix = re.split(r"[，,]", sentence[:match.start()])[-1]
                    direction = "input" if "输入" in prefix else "output" if "输出" in prefix else None
                    if direction:
                        supported = products.get(sku, {}).get(direction + "_w", [])
                if value not in supported:
                    issue("numeric", "数值或单位无对应商品证据", "conflict" if supported else "unsupported", sku=sku, actual=match.group(), expected=supported)
                else:
                    checks.append({"field": "numeric", "sku": sku, "actual": match.group(), "passed": True})
        if sentence_skus:
            previous_skus = sentence_skus

    policy = [s for s in chosen_evidence if s.get("kind") == "rule"]
    is_returns = bool(re.search(r"无理由|退货|退款|运费", text))
    if is_returns:
        if not policy:
            issue("policy", "政策主张缺少规则证据")
        if re.search(r"(?:所有|全部|任何|一律|无条件|一定|保证).{0,12}(?:退货|退款|免运费)|(?:退货|退款).{0,8}(?:无条件|一定|保证)", text):
            issue("policy", "无条件或绝对政策承诺与适用边界冲突", "conflict")
        if "无理由" in text and not ("完好" in text and "二次销售" in text):
            issue("policy", "无理由退货遗漏商品完好和二次销售条件", "needs_review")
        if not re.search(r"对应订单|平台公示|平台规则|平台.*为准", text):
            issue("policy", "政策说明遗漏订单或平台适用范围", "needs_review")
        if "无理由" in text:
            policy_text = "\n".join(s.get("content", "") for s in policy)
            known_days = {int(v) for v in re.findall(r"(\d+)\s*(?:天|日)", policy_text)}
            if "七天" in policy_text:
                known_days.add(7)
            for days in re.findall(r"(\d+)\s*(?:天|日)", text):
                if int(days) not in known_days:
                    issue("policy", "无理由退货时限没有相应规则依据", "conflict" if known_days else "unsupported", actual=int(days), expected=sorted(known_days))
    if "登机" in text or "托运" in text:
        if re.search(r"(?:保证|一定|肯定|直接|放心|可以|允许).{0,8}登机", text) and not re.search(r"不能|不可|不保证|无法保证|需.*核验|是否", text):
            issue("policy", "容量或额定能量不能证明允许登机", "conflict")
        if "登机" in text and not ("3C" in text and "召回" in text and ("核验" in text or "核实" in text)):
            issue("policy", "登机说明缺少标识、召回与实际核验条件", "needs_review")
    if re.search(r"已(?:为您|帮您|经)?(?:退款|转人工|登记|下单|查询订单)", text):
        issue("capability", "宣称完成未接入的业务操作", "conflict")

    statuses = {item["status"] for item in issues}
    status = next((s for s in ("conflict", "unsupported", "needs_review") if s in statuses), "pass")
    return {"passed": not issues, "status": status, "checks": checks, "issues": issues,
            "scope": "sku_numeric_policy_v1", "knowledge_snapshot_id": bundle.get("knowledge_snapshot_id"),
            "content_sha256": _digest(content), "checker_version": "fusion-facts-v1"}
