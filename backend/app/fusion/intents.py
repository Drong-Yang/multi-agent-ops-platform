"""Finite selection tags: no autonomous task creation from customer prompts."""
def intent_tags(text):
    return [tag for tag,words in {
        'weight':['重量','多重','重不重','轻不轻'], 'dimensions':['尺寸','多大','长宽','多长','多厚'],
        'capacity':['容量','毫安','mah'], 'warranty':['保修','质保'],
        'returns':['退货','退款','拆封','无理由','退换','能退'], 'price':['价格','多少钱'],
        'portability':['携带','便携','出差'], 'air_travel':['登机','飞机','航空'],
        'charging':['充电','快充','充多久'], 'compatibility':['兼容','适配'], 'comparison':['区别','对比','比较']
    }.items() if any(word in (text.lower().replace('充电宝','移动电源') if tag=='charging' else text.lower()) for word in words)]
