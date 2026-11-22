"""Short customer-facing answers built from selected catalogue or policy evidence."""

import re
import unicodedata

from .facts import NUMBER, SKU_RE, extract_product_facts


_RECOMMEND = re.compile(
    r'推荐|选购|(?:买|选|戴|用)哪[款个种]|哪[款个种].{0,15}(?:适合|好用|比较好)|'
    r'(?:想|打算|准备)(?:买|入手|找)|挑[一两几]?[款个种]'
)
_PAST_RECOMMENDATION = re.compile(r'(?:之前|上次|刚才|上面|你|您)(?:给我)?推荐的|推荐(?:过|了)的')
_RECOMMEND_EXPLANATION = re.compile(r'(?:为啥|为什么|为何).{0,8}推荐|推荐(?:的)?理由')
_PERSONAL_STYLE = re.compile(r'气质|我的?(?:风格|穿搭|审美|个性)')
_MATCH_LOOK = re.compile(r'(?:配|搭|适合|符合).{0,14}(?:衣服|外套|桌面|桌搭|书桌|房间)')
_SIMPLE_STYLE = re.compile(r'(?:简洁|简约|低调|素雅)(?!地(?:给|帮|为))')
_SPORT_STYLE = re.compile(r'运动风|运动感')
_COMMUTE = re.compile(r'通勤|地铁|公交|上下班')
_NOISE_PREFERENCE = re.compile(
    r'主动降噪|环境(?:噪音|噪声)|(?:通勤|要|带|有|支持|重视|在意|优先)降噪|(?<!通话)降噪(?:耳机|效果好)|^降噪[，。\s]'
)
_NOISE_OPTIONAL = re.compile(r'不(?:需要|想要|要求|在意|强求)|(?:有无|需不需要).{0,4}降噪')
_EARPHONE_FORM = re.compile(r'头戴|半入耳|耳挂式|挂耳式|运动挂耳|入耳式')
_HALF_IN_EAR = re.compile(r'半入耳')
_EARPHONE_USE_QUESTION = '您更在意轻便、通勤降噪，还是跑步时的佩戴稳定性？'
_STYLE_QUESTION = '您更喜欢简约低调的风格，还是偏运动感的款式？也可以告诉我主要用途，我帮您挑。'
_CARRIER = re.compile(r'包包|(?:背|挎|腰|小|手提|随身|通勤|斜挎)包|(?:我的|新买的|那个|您的)包(?!装)|包(?:口|里|内|是)|口袋|包的(?:颜色|尺寸)|内袋|夹层')
_FIT_CHOICE = re.compile(r'(?:装|放|塞)(?:得下|得进|进|不下|不进)|大小|尺寸|空间|放不放得下')
_FIT_ACTION = re.compile(r'(?:装|放|塞)(?:得下|得进|进)')
_COMPACT = re.compile(r'小巧|小一点|小一些|体积小|尺寸小|薄一点|薄一些|迷你|不占地方|不占空间')
_LOOK_CHOICE = re.compile(r'外观|颜色|配色|色系|颜值|搭配|好看|百搭|同色|简洁|简约|低调|素雅')
_TONE = r'([黑白灰蓝红绿紫橙粉黄棕银金米深浅])(?:色|的)'
_COLOR_TONE = re.compile(_TONE)
_PREFERRED_COLOR = re.compile(rf'(?:想要|希望是|我喜欢|偏好|要一款|买一款|优先选|(?:充电宝|耳机|款式)(?:要|选|用))\s*{_TONE}')
_NEUTRAL_COLORS = ('黑', '白', '灰', '黑色', '白色', '灰色')
_BAG_SIZE = re.compile(
    rf'({NUMBER})\s*[×xX*]\s*({NUMBER})(?:\s*[×xX*]\s*({NUMBER}))?\s*(毫米|厘米|mm|cm)'
    r'(?![a-z])', re.I,
)
_SIZE_UNIT = r'毫米|厘米|mm|cm'
_SIZED_SIDE = rf'\s*(?:是|约|为|[:：])?\s*({NUMBER})\s*({_SIZE_UNIT})?'
_LABELLED_BAG_SIZE = re.compile(
    rf'长(?:度)?{_SIZED_SIDE}[\s,、/]*宽(?:度)?{_SIZED_SIDE}(?:[\s,、/]*(?:厚|深)(?:度)?{_SIZED_SIDE})?',
    re.I,
)
_SIZE_QUESTION = '您方便量一下包里那一格的长、宽和可用厚度吗？有了尺寸我可以帮您筛选。'
_COLOR_QUESTION = '您的包是什么颜色，或者您希望充电宝是什么颜色？'
_COUNT = re.compile(r'(两三|[一二两三四五六七八九十几多]|\d+)\s*[款个种]')
_CATEGORIES = ('充电宝', '移动电源', '充电器', '耳机', '音箱', '手表', '手机', '平板', '键盘', '鼠标', '扩展坞', '硬盘', '数据线')
_HIKING = re.compile(r'登山|爬山|徒步|户外|野营|露营')
_RUNNING = re.compile(r'晨跑|夜跑|慢跑|跑步|健身|运动(?=时|用|耳机)')
_EAR_HOOK = re.compile(r'耳挂式|挂耳式|耳挂设计|运动挂耳')
_PER_EAR_WEIGHT = re.compile(r'单耳(?:重(?:量)?|约)?\s*(\d+(?:\.\d+)?)\s*(?:g|克)(?![a-z])', re.I)
_OVER_NIGHT = re.compile(r'过夜|多天|[两二三四五六七八九]+天|(?<!\d)(?:[2-9]|\d{2,})天|长途|多设备|大容量')
_LIGHT = re.compile(r'轻便|便携|最轻|更轻|重量优先|尽量轻|轻一点|轻一些|越轻越好')
_SPLASH = re.compile(r'防泼溅|防溅|防雨|防水|碰水|雨天|下雨')
_IMMERSION = re.compile(r'浸泡|浸水|泡水|潜水|落水|扔进水')
_PROTECTION = re.compile(r'(?:IP[A-Z0-9]+\s*)?(?:防尘)?防泼溅')
_PRICE_NUMBER = r'(\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)'
_BUDGET = re.compile(
    rf'预算\s*(?:在|不超过|最多|低于|不到)?\s*{_PRICE_NUMBER}|'
    rf'(?:不超过|低于|不到)\s*{_PRICE_NUMBER}\s*(?:元|块钱?)|'
    rf'{_PRICE_NUMBER}\s*(?:元|块钱?)?\s*(?:以内|以下|之内)'
)
_CHINESE_DIGITS = {char: digit for digit, char in enumerate('零一二三四五六七八九')} | {'〇': 0, '两': 2}
_CHINESE_UNITS = {'十': 10, '百': 100, '千': 1_000, '万': 10_000}
_CAPACITY = re.compile(r'(\d+)\s*(?:mAh|毫安(?:时)?)\s*(以上|起|以内|以下)?', re.I)
_POLICY_END = '具体以对应订单和平台公示为准。'
_RETURN_ELIGIBILITY = re.compile(
    r'(?:还|仍)?(?:能不能|能否|能|可不可以|可否|可以|是不是能|是否能|可|支不支持|支持)'
    r'(?:申请)?退(?:换货|货退款|货|掉|回去)?(?:得了|了)?(?:吗|么|嘛|呢|吧|不)?[\s。！!？?]*$'
)
_OTHER_RETURN_TOPIC = re.compile(
    r'运费|邮费|邮寄费|发票|定金|差价|优惠券|取消订单|到账|原路|保修|质保|多少钱|(?:什么|问一下|有哪些)颜色|多重|'
    r'质量问题|故障|坏了|破损|怎么|如何|流程|步骤|投诉|赔付|顺便|再问|'
    r'推荐|对比|配色|尺寸|重量|续航|功率|价格|(?:没|未)(?:有)?(?:收到|发货)|拒收|'
    r'(?:签收|收货).{0,8}(?:天|周|月)'
)
_OPEN_PACKAGE = re.compile(r'拆封|拆包|开封|拆开|(?:打开|开了|拆了)包装')
_USED_PRODUCT = re.compile(r'激活|试用|用过|用了|戴过|戴了')
_MISSING_PARTS = re.compile(r'(?:配件|耳帽|充电线|说明书).{0,6}(?:丢|找不到|弄没|不齐)|弄丢.{0,5}(?:配件|耳帽|充电线|说明书)')
_UNUSED_PRODUCT = re.compile(r'(?:还|都|从来)?(?:没(?:有)?|未)(?:给它)?(?:拆封|拆包|开封|使用(?:过)?|用(?:过)?|试用(?:过)?|激活(?:过)?|戴过)')
_UNOPENED_PACKAGE = re.compile(r'(?:没(?:有)?|未)(?:拆封|拆包|开封|拆开|打开包装)')
_INSPECTION_ONLY = r'(?:就|只)(?:是)?(?:拆开|拆封|拆包|开封)'
_RETURN_STATUS = re.compile(
    rf'(?:{_INSPECTION_ONLY}(?:了)?(?:包装)?(?:看了?(?:一眼|一下)?|检查了?(?:一下)?)?(?:而已)?|'
    r'拆开(?:来)?看了?(?:一眼|一下)|(?:已经)?(?:用过|试用过|激活过?|用(?:了)?[一两二三几\d]+(?:次|天))了?|'
    rf'(?:还|都|从来)?(?:没(?:有)?|未)(?:用过|使用过|试用过|激活)|配件(?:都)?齐全|{_MISSING_PARTS.pattern})'
)
_FREIGHT_QUESTION = re.compile(
    r'(?:那(?:么)?|请问)?(?:(?P<return>退(?:换)?货)(?:的)?|寄回去的?)?(?:运费|邮费)'
    r'(?:由)?谁(?:来)?(?:出|付|承担|支付)(?:啊|呢|吗)?'
)


def return_eligibility_question(message):
    """Recognize a single request about the ability to return an item."""
    message = message.strip()
    match = _RETURN_ELIGIBILITY.search(message)
    return bool(match and not _OTHER_RETURN_TOPIC.search(message) and not re.search(r'[？?]', message[:match.start()]))


def return_status_reply(message):
    """A short status statement can complete a preceding return question."""
    message = re.sub(r'[\s。！!]', '', message)
    parts = [re.sub(r'^(?:嗯+|我)+', '', part) for part in re.split(r'[，,、；;]+', message)]
    return bool(any(parts) and all(_RETURN_STATUS.fullmatch(part) for part in parts if part))


def return_freight_question(message, *, related=False):
    match = _FREIGHT_QUESTION.fullmatch(re.sub(r'[\s，,。！!？?]', '', message))
    return bool(match and (related or match['return']))


def combined_return_question(message):
    match = re.fullmatch(r'(.+)[，,；;？?。]([^，,；;？?。]+)', message.strip().rstrip('？?。'))
    return bool(match and return_eligibility_question(match[1]) and return_freight_question(match[2], related=True))


def answer_return_eligibility(message, sources, *, ask_status=True):
    rule = next((source for source in sources if source.get('kind') == 'rule' and not source.get('sku')
                 and '包装已打开还能无理由退货' in _heading(source)
                 and all(term in source.get('content', '') for term in ('查验', '完好', '配件', '订单'))), None)
    if not rule:
        return '请在购买平台的订单页查看这件商品的退货条件。核对时可以留意商品、包装和配件的状态。', []

    affirmative = _UNUSED_PRODUCT.sub('', message)
    if _MISSING_PARTS.search(affirmative):
        answer = '配件缺失可能影响退货。您可以先在订单页查看退货说明，申请时如实说明缺少哪些配件。'
    elif _USED_PRODUCT.search(affirmative):
        answer = '试用或激活后能否退货，要结合使用程度，以及商品和配件是否完好来判断。请在订单页核对退货条件。'
    elif _UNOPENED_PACKAGE.search(message):
        answer = '包装还没拆的话，可以先在订单页查看这件商品是否符合退货条件。寄回前请保持商品和配件完好。'
    elif _OPEN_PACKAGE.search(message) or _UNUSED_PRODUCT.search(message) or return_status_reply(message):
        answer = ('如果只是拆开包装查验，通常不会仅因此影响退货。还要看商品和配件是否完好，'
                  '以及订单页的退货条件。')
        if ask_status and not (_UNUSED_PRODUCT.search(message) or re.search(_INSPECTION_ONLY, message) or return_status_reply(message)):
            answer += '您拆开后有试用或激活过吗？'
    else:
        answer = '请先看订单页的退货条件，还要核对商品和配件是否完好。'
        if ask_status:
            answer += '您已经拆开包装或使用过了吗？'
    return answer, [rule]


def answer_return_freight(sources):
    rules = [rule for rule in sources if rule.get('kind') == 'rule' and not rule.get('sku')]
    buyer = next((rule for rule in rules if '无理由退货的运费' in _heading(rule)
                  and '买家自行承担' in rule.get('content', '')), None)
    seller = next((rule for rule in rules if '质量问题退货的运费' in _heading(rule)
                   and '卖家承担' in rule.get('content', '')), None)
    if not (buyer and seller):
        return '请在订单的售后页面核对退货运费由谁承担。', []
    return ('一般按退货原因来分。质量问题或商品与描述不符，退货运费通常由卖家承担。'
            '个人原因退货，通常由买家承担退回的运费。' + _POLICY_END), [seller, buyer]


def _heading(source):
    return source.get('source_section') or source.get('heading', '')


def _plain(text):
    return text.replace('**', '').strip()


def _number(value):
    return f'{value:g}'


def recommendation_requested(message):
    return bool(_RECOMMEND.search(message) and not _PAST_RECOMMENDATION.search(message)
                and not _RECOMMEND_EXPLANATION.search(message))


def recommendation_needs_preferences(message):
    """Identify a recommendation whose suitability has no actionable criteria yet."""
    return recommendation_requested(message) and bool(recommendation_preference_question(message))


def recommendation_preference_question(message):
    if _CARRIER.search(message):
        if _bag_dimensions(message) or _COMPACT.search(message) or _LIGHT.search(message):
            return ''
        if _FIT_CHOICE.search(message):
            return _SIZE_QUESTION
        if _color_tone(message) or _SIMPLE_STYLE.search(message):
            return ''
        if _LOOK_CHOICE.search(message):
            return _COLOR_QUESTION
        return '您更在意放进包里不占空间，还是想和包的颜色搭配？'
    if _MATCH_LOOK.search(message) and not (_color_tone(message) or _SIMPLE_STYLE.search(message)):
        return '您想搭配什么颜色，或偏好哪种风格？'
    if _PERSONAL_STYLE.search(message) and not _color_tone(message) and not any(
        pattern.search(message) for pattern in (_SIMPLE_STYLE, _SPORT_STYLE, _COMMUTE, _RUNNING, _HIKING, _LIGHT, _SPLASH)
    ):
        return _STYLE_QUESTION
    if broad_earphone_recommendation(message) and _option_limit(message, default=2) == 1:
        return _EARPHONE_USE_QUESTION
    return ''


def _color_tone(message):
    preferred = _PREFERRED_COLOR.search(message)
    if preferred:
        return preferred.group(1)
    if _CARRIER.search(message) and not _LOOK_CHOICE.search(message) and (
        _FIT_CHOICE.search(message) or _bag_dimensions(message) or _COMPACT.search(message) or _LIGHT.search(message)
    ):
        return ''
    match = _COLOR_TONE.search(message)
    return match.group(1) if match else ''


def _matching_colors(facts, tone):
    colors = facts.get('colors', [])
    if tone == '深':
        return [color for color in colors if color.endswith(('黑', '深灰', '海军蓝')) or '深' in color]
    if tone == '浅':
        return [color for color in colors if color.endswith(('白', '银', '米色')) or '浅' in color]
    return [color for color in colors if color.endswith((tone, tone + '色'))] if tone else []


def _bag_dimensions(message):
    for part in re.split(r'[。！？!?\n]', unicodedata.normalize('NFKC', message)):
        dimensions = _BAG_SIZE.search(part) or _LABELLED_BAG_SIZE.search(part)
        if not dimensions:
            continue
        before = part[:dimensions.start()]
        if (not (_CARRIER.search(before[-24:]) or _FIT_ACTION.search(before[-16:]))
                and re.search(r'(?:SC-|EW-|这款|商品|机身|产品).{0,16}(?:尺寸|是|写着)?\s*$', before, re.I)):
            continue
        if dimensions.re is _BAG_SIZE:
            values = tuple(_to_mm(value, dimensions.group(4)) for value in dimensions.groups()[:3] if value is not None)
        else:
            parts = dimensions.groups()
            default_unit = next((unit for unit in reversed(parts[1::2]) if unit), None)
            if not default_unit:
                continue
            values = tuple(_to_mm(value, unit or default_unit)
                           for value, unit in zip(parts[::2], parts[1::2]) if value is not None)
        if all(0 < value <= 2000 for value in values):
            return values
    return ()


def _to_mm(value, unit):
    return float(value) * (10 if unit.lower() in ('厘米', 'cm') else 1)


def _fits_dimensions(product, carrier):
    if not product:
        return False
    sides = sorted(product[:len(carrier)])
    return all(device <= room for device, room in zip(sides, sorted(carrier)))


def _size_text(dimensions):
    return ' × '.join(_number(value) for value in dimensions) + ' 毫米'


def answer_product_fit(message, sources):
    """Compare a single verified product against user-provided internal dimensions."""
    products = [source for source in sources if source.get('kind') == 'product' and source.get('sku')]
    if len(products) != 1:
        return None
    source, = products
    size = extract_product_facts(source.get('content', '')).get('dimensions_mm')
    if not size:
        return f'现有资料没有 {source["sku"]} 的机身尺寸。请先核对商品页的长、宽、厚，再与包口和内袋的可用空间比较。'
    lead = f'{source["sku"]} 的机身尺寸约 {_size_text(size)}。'
    carrier = _bag_dimensions(message)
    if not carrier:
        return lead + '能否放进去还取决于包口和内袋。' + _SIZE_QUESTION
    if not _fits_dimensions(size, carrier):
        if len(carrier) < 3:
            return lead + '按常规平放方向与您给的长宽对比，空间偏紧。可以再核对包口和内袋的实际空间。'
        return lead + '按您提供的内部长宽厚比较，至少有一边空间不足，建议优先选更小的型号。'
    if len(carrier) < 3:
        return lead + '按常规平放方向看，长和宽有空间。您方便再量一下内袋可用的厚度和包口吗？'
    return lead + '按您给的三边尺寸，这款可以作为候选。请为包口、内袋弧度和取放留一点余量。'


def _budget_value(message):
    match = _BUDGET.search(message)
    if not match:
        return None
    amount = next(value for value in match.groups() if value)
    if amount[0].isdecimal():
        return float(amount)
    if all(char in _CHINESE_DIGITS for char in amount):
        return int(''.join(str(_CHINESE_DIGITS[char]) for char in amount))

    total, digit, unit = 0, 0, 1
    for char in amount:
        if char in _CHINESE_DIGITS:
            digit = _CHINESE_DIGITS[char]
            if digit == 0:
                unit = 1
        else:
            unit = _CHINESE_UNITS[char]
            total = (total + digit or 1) * unit if char == '万' else total + (digit or 1) * unit
            digit = 0
    return total + digit * max(unit // 10, 1)


def _option_limit(message, *, default=1):
    count = _COUNT.search(message)
    if not count:
        return default
    value = count.group(1)
    if value.isdecimal():
        return max(1, min(int(value[:4]), 3))
    if value == '一':
        return 1
    return 2 if value in ('二', '两', '几', '多') else 3


def _earphone(source):
    return '耳机' in _heading(source) or source['sku'].upper().startswith('EW-BUDS')


def _earhook(source):
    return bool(_EAR_HOOK.search(_heading(source) + source.get('content', '')))


def _earphone_weight(source):
    match = _PER_EAR_WEIGHT.search(_plain(source.get('content', '')))
    return float(match.group(1)) if match else None


def _neutral_colors(facts):
    return [color for color in facts.get('colors', []) if color.endswith(_NEUTRAL_COLORS)]


def _active_noise_cancellation(source):
    return bool(re.search(r'(?<!不)支持主动降噪|(?<!通话)降噪最深\s*\d+', source.get('content', '')))


def _wants_noise_cancellation(message):
    match = _NOISE_PREFERENCE.search(message)
    return bool(match and not _NOISE_OPTIONAL.search(message[max(0, match.start() - 5):match.end()]))


def broad_earphone_recommendation(message):
    return ('耳机' in message and recommendation_requested(message) and not SKU_RE.search(message)
            and _budget_value(message) is None and not _color_tone(message)
            and not any(pattern.search(message) for pattern in (
                _COMMUTE, _RUNNING, _LIGHT, _NOISE_PREFERENCE, _EARPHONE_FORM, _CARRIER, _COMPACT,
                _SIMPLE_STYLE, _SPORT_STYLE, _PERSONAL_STYLE, _MATCH_LOOK, _SPLASH, _IMMERSION,
            )))


def _earphone_use(source):
    if _earhook(source):
        return 'running'
    if _active_noise_cancellation(source):
        return 'noise'
    if _earphone_weight(source) is not None:
        return 'light'
    return ''


def _earphone_use_options(candidates, limit):
    options = []
    for use in ('light', 'noise', 'running'):
        matching = [row for row in candidates if _earphone_use(row[0]) == use]
        if not matching:
            continue
        if use == 'light':
            best = min(matching, key=lambda row: _earphone_weight(row[0]))
        else:
            best = min(matching, key=lambda row: row[1].get('price_cny', float('inf')))
        options.append(best)
    return options[:limit]


def _earphone_use_description(source, *, explain=False):
    use = _earphone_use(source)
    sku = source['sku']
    if use == 'light':
        weight = _number(_earphone_weight(source))
        form = '是半入耳设计，' if _HALF_IN_EAR.search(_heading(source)) else ''
        spec = f'{form}单耳约 {weight} 克'
        if explain:
            return f'{sku} {spec}，日常佩戴想减轻耳朵的负担可以先考虑这款。贴合度和舒适度以试戴为准。'
        return f'想轻便一些，可以看 {sku}。这款{spec}。'
    if use == 'noise':
        if explain:
            detail = '需要听周围声音时还可以切到通透模式。' if '通透模式' in source.get('content', '') else ''
            return f'{sku} 支持主动降噪，坐地铁或公交时有助于减轻环境噪声。{detail}'
        return f'通勤想减轻环境噪声，可以看支持主动降噪的 {sku}。'
    if use == 'running':
        if explain:
            return f'{sku} 的耳挂能挂住耳朵，跑动时多一处固定。佩戴是否稳固还要看试戴效果。'
        return f'跑步在意佩戴稳定性，可以看采用耳挂式设计的 {sku}。'
    return ''


def _running_details(source):
    content = _plain(source.get('content', ''))
    details = ['资料标注耳挂式设计'] if _earhook(source) else []
    weight = _earphone_weight(source)
    if weight is not None:
        details.append('单耳约 ' + _number(weight) + ' 克')
    if '防汗防泼溅' in content:
        details.append('资料标注防汗防泼溅')
    elif '通透模式' in content and len(details) < 2:
        details.append('支持通透模式')
    return details


def _running_tradeoff(sources):
    hooked = next((source for source in sources if _earhook(source)), None)
    light = next((source for source in sources if not _earhook(source) and _earphone_weight(source) is not None), None)
    if hooked and light:
        return f'更在意耳挂式固定可以先看 {hooked["sku"]}，更在意重量可以先看 {light["sku"]}。'
    return ''


def _details(source, facts, *, message='', power_bank=False, show_price=False, splash=False, running=False):
    details = _running_details(source) if running else []
    carrier = _CARRIER.search(message)
    if carrier and facts.get('dimensions_mm'):
        details.append('机身约 ' + _size_text(facts['dimensions_mm']))
    colors = _matching_colors(facts, _color_tone(message))
    if not colors and _SIMPLE_STYLE.search(message):
        colors = _neutral_colors(facts)
    if not colors and carrier:
        colors = facts.get('colors', [])
    if colors:
        details.append('有' + '、'.join(colors) + '配色')
    if _SPORT_STYLE.search(message) and not running and _earhook(source):
        details.append('资料标注耳挂式设计')
    content = source.get('content', '')
    if (_COMMUTE.search(message) or _wants_noise_cancellation(message)) and (
        _earphone(source) and _active_noise_cancellation(source)
    ):
        details.append('支持主动降噪')
        if '通透模式' in content:
            details.append('也能切换通透模式')
    if power_bank and not (carrier and _color_tone(message)) and 'capacity_mah' in facts:
        details.append('电池容量 ' + _number(facts['capacity_mah']) + ' mAh')
    weight = _earphone_weight(source) if _earphone(source) else facts.get('weight_g')
    if not running and weight is not None:
        details.append(('单耳约 ' if _earphone(source) else '净重约 ') + _number(weight) + ' 克')
    if show_price and 'price_cny' in facts:
        details.append('资料标价 ' + _number(facts['price_cny']) + ' 元')
    protection = _PROTECTION.search(content)
    if splash and protection and not (running and '防汗防泼溅' in content):
        details.append('资料标注' + protection.group())
    elif power_bank and '自带一根 C 线' in content:
        details.append('自带 USB-C 线')
    return '，'.join(details)


def explain_recommendation(message, sources, *, clarify=False):
    """Explain an earlier recommendation from only the already verified products."""
    sources = [source for source in sources if source.get('kind') == 'product' and source.get('sku')][:3]
    if not sources:
        return None
    if broad_earphone_recommendation(message) and all(_earphone(source) for source in sources):
        explanations = [_earphone_use_description(source, explain=True) for source in sources]
        if all(explanations):
            lead = '这几款各有侧重。' if len(sources) > 1 else ''
            return lead + ''.join(explanations), sources
    activity = _RUNNING.search(message)
    running = bool(activity and all(_earphone(source) for source in sources))
    power_bank = all('充电宝' in _heading(source) or source['sku'].upper().startswith('SC-PB') for source in sources)
    budget = _budget_value(message)
    details = []
    for source in sources:
        description = _details(source, extract_product_facts(source.get('content', '')), message=message,
                               power_bank=power_bank, show_price=budget is not None, splash=bool(_SPLASH.search(message)),
                               running=running)
        if description:
            details.append(f'{source["sku"]} {description}')
    if not details:
        return None
    if _CARRIER.search(message):
        lead = ('如果更看重配色，可以先看这些有记录的颜色。' if _color_tone(message)
                else '给包里选随身用品时，先看机身能否放进常用夹层，再看日常携带的重量。')
        answer = lead + '。'.join(details) + '。'
        prompt = recommendation_preference_question(message)
        if prompt:
            answer += prompt
        elif not _color_tone(message):
            answer += _bag_check_prompt(_bag_dimensions(message))
        else:
            answer += '可以对照包和商品的实拍图看看搭配效果。'
        return answer, sources
    if recommendation_needs_preferences(message):
        explanation = '我刚才给出的依据是 ' + '。'.join(details) + '。'
        if any(_earphone(source) and _earphone_weight(source) is not None for source in sources):
            explanation += '如果您在意轻便，单耳重量可以作为参考。'
        return explanation + '要判断外观是否合适，还需要先了解您的喜好。' + recommendation_preference_question(message), sources
    if running:
        hooked = any(_earhook(source) for source in sources)
        if clarify:
            lead = f'简单说，{activity.group()}时可以按佩戴方式和重量来挑。'
        elif _LIGHT.search(message) or not hooked:
            lead = f'{activity.group()}时，较轻的单耳重量可以减轻佩戴负担。'
        else:
            lead = f'{activity.group()}时，耳挂能多一处固定。'
    elif power_bank and _HIKING.search(message):
        lead = '简单说，按您的登山用途看容量和重量。' if clarify else '考虑到您登山时要随身带，我主要比较容量和重量。'
    elif _COMMUTE.search(message) and all(_earphone(source) for source in sources):
        lead = ('通勤坐地铁或公交时，主动降噪有助于减轻环境噪声。'
                if any(_active_noise_cancellation(source) for source in sources)
                else '通勤随身使用，可以把重量作为佩戴和携带时的参考。')
    elif _SIMPLE_STYLE.search(message):
        lead = '您偏好简约风，我优先看了型号的配色。'
    elif _LIGHT.search(message) and all(_earphone(source) for source in sources):
        lead = '您在意轻便，所以我优先比较耳机重量。舒适度可以通过试戴确认。'
    elif _wants_noise_cancellation(message) and all(_earphone(source) for source in sources):
        lead = '您在意环境噪声，我筛选了资料标注主动降噪的型号。'
    elif budget is not None:
        lead = f'我先按您给的 {_number(budget)} 元预算筛选。'
    else:
        lead = '简单说，刚才推荐的依据是这些资料。' if clarify else '我刚才的推荐参考了这些已记录的商品条件。'
    answer = lead + '。'.join(details) + '。'
    if running:
        answer += _running_tradeoff(sources)
    if _SIMPLE_STYLE.search(message):
        answer += '具体外观可以对照商品图，看是否符合您的喜好。'
    return answer, sources


def _bag_check_prompt(dimensions):
    if not dimensions:
        return '想确认装得下，您方便量一下内袋的长、宽和厚度吗？'
    if len(dimensions) < 3:
        return '按常规平放方向筛选了长、宽。您方便再量一下内袋可用的厚度和包口吗？'
    return '建议再核对包口和内袋形状，并为取放留一点余量。'


def recommend_product(message, sources, *, excluded_skus=()):
    if not recommendation_requested(message):
        return None

    sku = SKU_RE.search(message)
    category = next((name for name in _CATEGORIES if name in message), '')
    if not category and sku is None:
        return '您想选哪类商品？告诉我用途和预算，我可以帮您挑一款。', []
    power_bank = category in ('充电宝', '移动电源')
    if power_bank:
        category = '充电宝'
    activity = _RUNNING.search(message)
    if _IMMERSION.search(message):
        return '当前资料无法确认有可浸水使用的商品。如果您只需要防泼溅，我可以按这个条件帮您筛选。', []
    if power_bank and re.search(r'飞机|登机|托运|航班', message):
        return '当前商品资料无法核验实物标识和召回状态，暂时无法推荐能登机的具体型号。请以航司核验结果为准。', []
    preference_question = recommendation_preference_question(message)
    if preference_question:
        return preference_question, []

    budget = _budget_value(message)
    capacity_match = _CAPACITY.search(message) if power_bank else None
    splash = bool(_SPLASH.search(message))
    carrier = bool(_CARRIER.search(message))
    carrier_size = _bag_dimensions(message) if carrier else ()
    color_tone = _color_tone(message)
    noise = category == '耳机' and _wants_noise_cancellation(message)
    excluded = {value.upper() for value in excluded_skus}
    candidates = []
    for source in sources:
        if source.get('kind') != 'product' or not source.get('sku') or source['sku'].upper() in excluded:
            continue
        if sku:
            if source['sku'].upper() != sku.group().upper():
                continue
        else:
            heading = _heading(source)
            if category not in heading:
                continue
            if category == '耳机':
                if '头戴' in message and '头戴' not in heading:
                    continue
                if _HALF_IN_EAR.search(message) and not _HALF_IN_EAR.search(heading):
                    continue
                if _EAR_HOOK.search(message) and not _earhook(source):
                    continue
                if activity and '头戴' not in message and not (
                    _earhook(source) or _earphone_weight(source) is not None
                ):
                    continue
        if noise and not _active_noise_cancellation(source):
            continue
        facts = extract_product_facts(source.get('content', ''))
        if carrier_size and not _fits_dimensions(facts.get('dimensions_mm'), carrier_size):
            continue
        if color_tone and not _matching_colors(facts, color_tone):
            continue
        if budget is not None and facts.get('price_cny', float('inf')) > budget:
            continue
        if capacity_match:
            wanted, bound = capacity_match.groups()
            actual = facts.get('capacity_mah')
            if actual is None or (bound in ('以上', '起') and actual < int(wanted)) or \
                    (bound in ('以内', '以下') and actual > int(wanted)) or (not bound and actual != int(wanted)):
                continue
        if splash and not _PROTECTION.search(source.get('content', '')):
            continue
        candidates.append((source, facts))

    if not candidates:
        if excluded:
            return '现有商品资料里暂时没有其他符合这些条件的型号。您愿意调整预算或其他要求吗？', []
        if carrier_size or color_tone:
            return '现有商品里暂未找到同时满足您给出的条件、且有资料可核对的型号。您愿意放宽哪项要求？', []
        return '我暂时无法从现有资料确认有符合这些条件的型号。您最看重哪一项，我可以优先帮您筛选？', []

    if category == '耳机' and broad_earphone_recommendation(message):
        options = _earphone_use_options(candidates, _option_limit(message, default=2))
        if options:
            selected = [source for source, _ in options]
            answer = ''.join(_earphone_use_description(source) for source in selected)
            if len(selected) > 1 and not excluded:
                answer += '您更偏向哪种用途？'
            return answer, selected
        return _EARPHONE_USE_QUESTION, []

    hiking = power_bank and bool(_HIKING.search(message))
    overnight = power_bank and bool(_OVER_NIGHT.search(message))
    running = bool(activity and all(_earphone(source) for source, _ in candidates))
    prefers_light = bool(_LIGHT.search(message))
    compact = bool(_COMPACT.search(message))
    simple_style = bool(_SIMPLE_STYLE.search(message))
    sporty_style = bool(_SPORT_STYLE.search(message))
    commute = bool(_COMMUTE.search(message) and all(_earphone(source) for source, _ in candidates))
    if (carrier and not color_tone and not simple_style) or compact:
        if prefers_light and not compact:
            candidates.sort(key=lambda pair: (pair[1].get('weight_g', float('inf')), not pair[1].get('dimensions_mm')))
        else:
            candidates.sort(key=lambda pair: (_volume(pair[1]), pair[1].get('weight_g', float('inf'))))
    elif running:
        candidates.sort(key=lambda row: (0 if prefers_light or _earhook(row[0]) else 1,
                                         _earphone_weight(row[0]) or float('inf')))
    elif prefers_light:
        candidates.sort(key=lambda pair: pair[1].get('weight_g', float('inf')))
    elif overnight and not capacity_match:
        candidates.sort(key=lambda pair: -pair[1].get('capacity_mah', 0))
    elif hiking and not capacity_match:
        candidates.sort(key=lambda pair: (abs(pair[1].get('capacity_mah', 0) - 10000), pair[1].get('weight_g', float('inf'))))
    elif simple_style or sporty_style or commute:
        candidates.sort(key=lambda pair: (
            simple_style and not _neutral_colors(pair[1]),
            sporty_style and not _earhook(pair[0]),
            commute and not _active_noise_cancellation(pair[0]),
        ))

    chosen = candidates[:_option_limit(message)]
    selected = [source for source, _ in chosen]
    day_trip = hiking and not (overnight or splash or capacity_match or prefers_light)
    lead = '如果是当天往返、主要给手机补电，' if day_trip else ''
    if carrier and color_tone:
        lead = '如果想按这个色系搭配，'
    elif carrier:
        lead = '按您提供的尺寸，' if carrier_size else '如果您主要在意随身携带时少占空间，'
    elif running:
        lead = f'{activity.group()}的话，'
    elif commute:
        lead = '通勤的话，'
    if len(chosen) == 1:
        source, facts = chosen[0]
        answer = lead + f'我建议先看 {source["sku"]}。'
        details = _details(source, facts, message=message, power_bank=power_bank, show_price=budget is not None,
                           splash=splash, running=running)
        if details:
            answer += '这款' + details + '。'
    else:
        options = []
        for source, facts in chosen:
            details = _details(source, facts, message=message, power_bank=power_bank, show_price=True,
                               splash=splash, running=running)
            options.append(f'{source["sku"]} {details}'.rstrip())
        answer = lead + '可以先对比这几款。' + '。'.join(options) + '。'
        if running:
            answer += _running_tradeoff(selected)
    if splash:
        answer += '浸水使用的条件目前无法确认。'
    elif day_trip and budget is None:
        answer += '如果会在山上过夜，您预计给几台设备补电？'
    if simple_style or sporty_style:
        answer += '具体外观可以对照商品图，看是否符合您的喜好。'
    if carrier and (carrier_size or not (color_tone or simple_style)):
        answer += _bag_check_prompt(carrier_size)
    elif carrier and color_tone:
        answer += '可以对照包和商品的实拍图看看搭配效果。'
    return answer, selected


def _volume(facts):
    dimensions = facts.get('dimensions_mm')
    if not dimensions:
        return float('inf')
    length, width, height = dimensions
    return length * width * height


# Match the current intent to retrieved rule headings. Generic lexical overlap
# alone can rank invoices and logistics for an unrelated complaint.
_POLICY_MATCH = (
    (r'质量问题.*运费|(?:运费|邮费).*质量问题', r'质量问题退货的运费'),
    (r'无理由.*运费|退货.*(?:运费|邮费)', r'无理由退货的运费'),
    (r'运费险.*(?:上门|取件)', r'上门取件如何赔付'),
    (r'运费险.*(?:赔|保障)|(?:赔|保障).*运费险', r'运费险保障什么'),
    (r'退款.*(?:到账|多久|几天)', r'退款多久能到账'),
    (r'退款.*(?:退到|原路|退回.*哪)', r'退款退回到哪里'),
    (r'(?:怎么|如何|在哪).*申请.*(?:退货|退款)|(?:退货|退款).*(?:流程|申请步骤)', r'如何申请退货退款'),
    (r'无理由.*(?:期限|从什么|几天算)', r'七天期限从什么时候开始算'),
    (r'无理由.*退|七天.*退', r'什么是七天无理由退换货'),
    (r'发票.*(?:开错|错了|重开|写错)', r'发票内容开错'),
    (r'(?:发货|卖家).*(?:晚发|延迟|未按|不发货|没发货)', r'卖家未按约定时间发货'),
    (r'(?:物流|快递).*(?:查不到|没更新|无信息)|(?:查不到|没更新).*物流', r'发货后查不到物流信息'),
    (r'(?:怎么|如何|什么情况).*平台介入|平台介入.*(?:怎么|如何|什么情况)', r'什么情况下可以申请平台介入'),
)


def mock_support_answer(message, sources):
    message = message.rsplit('；追问：', 1)[-1]
    rules = [source for source in sources if source.get('kind') == 'rule']
    if re.search(r'拆(?:封|包)|(?:打开|开了|拆了)包装', message) and re.search(r'退', message):
        for source in rules:
            content = source.get('content', '')
            if '商品完好' in content and re.search(r'必要查验|查验而拆包', content):
                return ('仅为必要查验而拆封，通常不会因此失去退货资格。还要核对商品完好、实际使用情况及适用条件。'
                        + _POLICY_END), [source]
        return '目前无法确认拆封后的退货条件。建议在购买平台的订单页查看适用规则，或联系商家人工客服。', []

    for intent, heading in _POLICY_MATCH:
        if not re.search(intent, message):
            continue
        source = next((rule for rule in rules if re.search(heading, _heading(rule))), None)
        if source:
            first_sentence = re.split(r'(?<=。)|\r?\n', _plain(source['content']), maxsplit=1)[0]
            return first_sentence + _POLICY_END, [source]
        break

    fields = (
        (r'多重|重量|净重', 'weight_g', '净重约 {} 克'),
        (r'保修|质保', 'warranty_months', '保修 {} 个月'),
        (r'价格|售价|多少钱', 'price_cny', '资料标价 {} 元'),
        (r'容量|毫安|mAh', 'capacity_mah', '电池容量 {} mAh'),
    )
    descriptions, evidence = [], []
    for source in sources:
        if source.get('kind') != 'product' or not source.get('sku'):
            continue
        facts = extract_product_facts(source.get('content', ''))
        details = [template.format(_number(facts[key])) for pattern, key, template in fields
                   if key in facts and re.search(pattern, message, re.I)]
        if details:
            descriptions.append(source['sku'] + ' ' + '，'.join(details) + '。')
            evidence.append(source)
    if descriptions:
        return ' '.join(descriptions[:2]), evidence[:2]

    return '这点我暂时无法从现有资料确认。您可以补充具体型号或想了解的问题，涉及订单时可在购买平台联系商家核实。', []
