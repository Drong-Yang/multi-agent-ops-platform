"""Handle product safety reports before commerce retrieval or answer generation."""

import re


_DEVICE = re.compile(
    r'充电宝|移动电源|电池|电芯|充电器|电源|耳机|手机|平板|电脑|手表|设备|'
    r'(?<![a-z0-9])(?:sc|ew)[\s-]*[a-z]+[\s-]*\d+[a-z]*', re.I,
)
_FIRE = re.compile(r'爆炸|炸了|炸开|炸过|起火|着火|燃烧|烧起来|冒烟|冒黑烟|冒白烟|冒火|有明火|看到明火|冒火花')
_DAMAGE = re.compile(r'鼓包|鼓起来|鼓胀|漏液|异常发热|烫手|很烫|烫伤|烧伤|被炸伤|焦味|烧焦味')
_CLAUSE = re.compile(r'[，,。！？!?；;\n]+')
_NOT_REPORTED = re.compile(
    r'(?:会不会|会否|会|是否会|可能会|避免|防止|防|如果|假如|假设|预防|'
    r'(?:没|没有|未|并未|不会)(?:有|出现|发现|看到|发生|再)?)\s*$'
)
_HYPOTHETICAL = re.compile(r'如果|假如|假设|万一|要是|担心|远离|避免接触')
_REFERENCE = re.compile(r'^(?:那|它|这(?:个|款)|上面|刚才)|一(?:直|边).{0,10}(?:充电|插着)')
_OMITTED_SUBJECT = re.compile(
    rf'^(?:(?:刚刚|刚才|刚|突然|现在|正在|已经|开始|还在|一直|又)\s*){{0,3}}'
    rf'(?:{_FIRE.pattern}|{_DAMAGE.pattern})'
)
_FOLLOW_UP = re.compile(
    r'^(?:(?:那|现在|请问|所以|我|如果|已经|刚才|要是)\s*){0,4}'
    r'(?:怎么办|怎么处理|咋办|能不能.{0,8}(?:碰|挪|移动|寄|扔|继续用|灭火)|'
    r'把它|可以.{0,8}(?:碰|挪|移动|寄|扔|继续用|灭火)|需要.{0,6}(?:报警|求救|就医)|'
    r'还在冒烟|还在烧|没有人受伤|烟散了|火灭了|已经安全|要不要.{0,6}(?:报警|求救|就医)|'
    r'联系谁|怎么寄|怎么赔|怎么退款|怎么退货|要赔偿)'
)

# Fire-service guidance prioritizes evacuation over handling the device.
# https://www.usfa.fema.gov/a-z/lithium-ion-batteries/community-education/
_FIRE_REPLY = (
    '这确实让人担心。请立即远离设备和烟雾，提醒附近的人一起撤到安全处。'
    '请从安全处立即联系当地紧急救援。如果有人受伤或吸入烟雾，也请告诉救援人员。'
    '先不要触碰、搬动或尝试自行灭火，即使烟火已经消失也不要返回处理。'
    '人员安全、现场得到处理后，请通过购买平台的订单页联系商家人工售后，询问后续处理方式。请勿自行寄送受损设备。'
)
_DAMAGE_REPLY = (
    '请先停止使用和充电，远离设备，避免触碰、挤压或拆开。'
    '如果已经冒烟、起火或有人受伤，请撤到安全处并立即联系当地紧急救援。'
    '确认人员安全后，请通过购买平台的订单页联系商家人工售后，询问后续处理方式。请勿自行寄送受损设备。'
)


def _incident_in(message):
    for pattern, response in ((_FIRE, _FIRE_REPLY), (_DAMAGE, _DAMAGE_REPLY)):
        for clause in _CLAUSE.split(message):
            for match in pattern.finditer(clause):
                prefix = clause[:match.start()]
                if not (_NOT_REPORTED.search(prefix) or _HYPOTHETICAL.search(prefix)):
                    return response
    return None


def safety_incident_answer(message, previous):
    """Return advice for a reported device incident or clearly related follow-up."""
    message = message.strip()
    latest = previous[-1] if previous else {}
    prior_question = latest.get('question', '')
    prior_device = any(_DEVICE.search(turn.get('question', '')) for turn in previous)
    prior_incident = latest.get('answer_route') == 'safety_incident' or (
        bool(_DEVICE.search(prior_question)) and bool(_incident_in(prior_question))
    )
    incident = _incident_in(message)
    referring_to_device = (prior_device or prior_incident) and (
        _REFERENCE.search(message) or _OMITTED_SUBJECT.search(message)
    )
    if incident and (_DEVICE.search(message) or referring_to_device):
        return incident
    if prior_incident and _FOLLOW_UP.search(message):
        return incident or _incident_in(prior_question) or _FIRE_REPLY
    return None
