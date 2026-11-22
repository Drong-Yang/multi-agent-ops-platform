"""Extract source fragments for supported pre-sales product questions."""
import re

_FIELDS = {
    'weight': (r'多重|重量|净重|轻不轻|重不重', r'净重|重量|机身重量'),
    'dimensions': (r'尺寸|体积|长宽|多长|多宽|多厚|多大', r'尺寸|机身尺寸|产品尺寸|规格尺寸|充电盒尺寸|厚度'),
    'colors': (r'颜色|配色|什么色|色系|(?:有|是|选).{0,6}[黑白灰银蓝粉绿红紫棕金](?:色|的)(?=吗|的|款|[？?。]|$)',
               r'颜色|配色'),
    'appearance': (r'外观|样子|长什么样|造型|设计风格|看起来怎么样',
                   r'颜色|配色|形态|外观|造型|尺寸|机身尺寸|产品尺寸|充电盒尺寸'),
    'material': (r'材质|材料|什么做的|外壳|表面|触感|哑光|磨砂|亮面', r'材质|材料|机身材质|外壳|表面'),
    'capacity': (r'容量|多少毫安|多少\s*mah', r'容量|额定容量|电池容量|存储容量|内存容量'),
    'battery_life': (r'续航|能听多久|能播放多久', r'续航|续航时间|电池续航|播放时长'),
    'price': (r'多少钱|价格|售价', r'售价|价格|参考价'),
    'package': (r'套餐|包装|配件|标配|包含什么|送什么', r'包装|包装清单|标配|配件|套装|套餐'),
    'model': (r'型号是什么|型号是多少|什么型号', r'型号|产品型号'),
    'power': (r'输出|功率|多少瓦|快充|支持几瓦', r'输出|输入|功率|额定功率|最大功率|快充|充电功率'),
    'specs': (r'规格|参数|有何配置|配置是什么', ''),
}
_COMPARISON = re.compile(r'对比|比较|区别')
_DEFAULT_COMPARISON = ('capacity', 'weight', 'power', 'price', 'dimensions')
_FIELD_NAMES = {
    'weight': '重量', 'dimensions': '尺寸', 'colors': '配色', 'appearance': '外观',
    'material': '材质', 'capacity': '容量', 'battery_life': '续航', 'price': '价格',
    'package': '包装和配件', 'power': '功率规格',
}
_MIXED = re.compile(r'退货|退款|售后|故障|维修|保修|质保|固件|报错|无法|登机|航空|铁路|运费|'
                    r'兼容|适配|能否|是不是|能不能|顺便|另外|再问|为什么|库存|赠送|多少折')
_MISSING = '当前资料无法核实所问的商品参数，请补充具体型号或查阅对应商品详情。'


def _spec_lines(content):
    return [line.strip().removeprefix('- ') for line in re.split(r'[；;\n]', content)]


def _field_lines(lines, label):
    return [line for line in lines if re.match('(?:' + label + r')\s*[：:]', line)]


def _comparison_fragments(products):
    specs = []
    for product in products:
        lines = _spec_lines(product['content'])
        fields = {}
        for field in _DEFAULT_COMPARISON:
            label = '输出' if field == 'power' else _FIELDS[field][1]
            fields[field] = next(iter(_field_lines(lines, label)), '')
        specs.append(fields)
    common = [field for field in _DEFAULT_COMPARISON if all(spec[field] for spec in specs)][:3]
    return ['\n'.join(spec[field] for field in common) for spec in specs] if common else []


def catalog_quote(message, result, *, voice=False):
    """Returns exact source spans, or a fixed response for missing catalog facts."""
    if _MIXED.search(message):
        return None
    fields = {name: label for name, (query, label) in _FIELDS.items() if re.search(query, message, re.I)}
    comparison = bool(_COMPARISON.search(message))
    if not fields and not comparison:
        return None
    missing = ('当前资料未注明所问的颜色、外观或材质。您可以查看对应型号的商品图和规格说明。'
               if fields.keys() & {'colors', 'appearance', 'material'}
               and result.get('retrieval_mode') == 'catalog_exact'
               else _MISSING)
    if result.get('retrieval_mode') != 'catalog_exact':
        return missing, []
    products = [source for source in result['sources'] if source.get('kind') == 'product' and source.get('sku')]
    if not products or (comparison and len(products) < 2):
        return _MISSING, []
    if comparison and not fields:
        fragments = _comparison_fragments(products)
        if not fragments:
            return _MISSING, []
        answer = '\n\n'.join(source['sku'] + '\n' + fragment for source, fragment in zip(products, fragments))
        return (answer.replace('**', '').replace('\n', '。') if voice else answer), products
    selections = []
    found_field = 'model' in fields or 'specs' in fields
    for source in products:
        prefix = source['sku']
        if 'specs' in fields:
            fragment = source['content']
        elif fields.keys() == {'model'}:
            prefix = source['source_section']
            fragment = ''
        else:
            lines = _spec_lines(source['content'])
            matches = {name: _field_lines(lines, label) for name, label in fields.items() if name != 'model'}
            absent = [_FIELD_NAMES[name] for name, matches_for_field in matches.items() if not matches_for_field]
            if absent and len(products) == 1:
                return missing, []
            chosen = set().union(*matches.values())
            found_field = found_field or bool(chosen)
            fragments = [line for line in lines if line in chosen]
            if absent:
                fragments.append('当前资料未注明' + '、'.join(absent) + '。')
            fragment = '\n'.join(fragments)
        selections.append('\n'.join(part for part in (prefix, fragment) if part))
    if not found_field:
        return missing, []
    answer = '\n\n'.join(selections)
    if voice:
        answer = answer.replace('**', '').replace('\n', '。')
    return answer, products
