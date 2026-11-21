"""Extract source fragments for supported pre-sales product questions."""
import re

_FIELDS = {
    'weight': (r'多重|重量|净重|轻不轻|重不重', r'净重|重量|机身重量'),
    'dimensions': (r'尺寸|体积|长宽|多长|多宽|多厚|多大', r'尺寸|机身尺寸|产品尺寸|规格尺寸|厚度'),
    'capacity': (r'容量|多少毫安|多少\s*mah', r'容量|额定容量|电池容量|存储容量|内存容量'),
    'price': (r'多少钱|价格|售价', r'售价|价格|参考价'),
    'package': (r'套餐|包装|配件|标配|包含什么|送什么', r'包装|包装清单|标配|配件|套装|套餐'),
    'model': (r'型号是什么|型号是多少|什么型号', r'型号|产品型号'),
    'power': (r'输出|功率|多少瓦|快充|支持几瓦', r'输出|输入|功率|额定功率|最大功率|快充|充电功率'),
    'specs': (r'规格|参数|有何配置|配置是什么', ''),
}
_COMPARISON = re.compile(r'对比|比较|区别')
_MIXED = re.compile(r'退货|退款|售后|故障|维修|保修|质保|固件|报错|无法|登机|航空|铁路|运费|'
                    r'兼容|适配|能否|是不是|能不能|顺便|另外|再问|为什么|库存|赠送|多少折')
_MISSING = '当前资料无法核实所问的商品参数，请补充具体型号或查阅对应商品详情。'


def catalog_quote(message, result, *, voice=False):
    """Returns exact source spans, or a fixed response for missing catalog facts."""
    if _MIXED.search(message):
        return None
    fields = {name for name, (query, _) in _FIELDS.items() if re.search(query, message, re.I)}
    comparison = bool(_COMPARISON.search(message))
    if not fields and not comparison:
        return None
    if result.get('retrieval_mode') != 'catalog_exact':
        return _MISSING, []
    products = [source for source in result['sources'] if source.get('kind') == 'product' and source.get('sku')]
    if not products or (comparison and len(products) < 2):
        return _MISSING, []
    selections = []
    for source in products:
        if 'specs' in fields or comparison and not fields:
            fragment = source['content']
        else:
            lines = [line.strip().removeprefix('- ') for line in re.split(r'[；\n]', source['content'])]
            matches = {name: {line for line in lines if re.match('(?:' + _FIELDS[name][1] + r')\s*[：:]', line)}
                       for name in fields - {'model'}}
            if not all(matches.values()):
                return _MISSING, []
            chosen = set().union(*matches.values())
            fragment = '\n'.join(line for line in lines if line in chosen)
        prefix = source['source_section'] if fields == {'model'} else source['sku']
        selections.append('\n'.join(part for part in (prefix, fragment) if part))
    answer = '\n\n'.join(selections)
    if voice:
        answer = answer.replace('**', '').replace('\n', '。')
    return answer, products
