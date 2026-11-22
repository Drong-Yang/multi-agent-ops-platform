import unittest

from app.fusion.catalog_answers import catalog_quote
from app.fusion.legacy_rag.rag_service import corpus
from app.fusion.support_answers import (
    answer_product_fit,
    answer_return_eligibility,
    combined_return_question,
    explain_recommendation,
    mock_support_answer,
    recommend_product,
    recommendation_needs_preferences,
    recommendation_preference_question,
    return_eligibility_question,
)


class SupportAnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = corpus()['chunks']

    def test_hiking_recommendation_is_one_conditional_choice_with_evidence(self):
        answer, evidence = recommend_product('给我推荐一款登山用的充电宝', self.sources)
        self.assertIn('当天', answer)
        self.assertIn('SC-PB10', answer)
        self.assertIn('215 克', answer)
        self.assertIn('10000 mAh', answer)
        self.assertIn('过夜', answer)
        self.assertEqual([source['sku'] for source in evidence], ['SC-PB10'])
        self.assertNotIn('防水', answer)
        self.assertNotIn('SC-PB10L', answer)

    def test_budget_and_explicit_outdoor_features_only_use_verified_candidates(self):
        answer, evidence = recommend_product('推荐登山充电宝，预算150元以内', self.sources)
        self.assertIn('SC-PB05', answer)
        self.assertIn('129 元', answer)
        self.assertEqual(evidence[0]['sku'], 'SC-PB05')

        answer, evidence = recommend_product('推荐能防泼溅的充电宝', self.sources)
        self.assertIn('IP54 防尘防泼溅', answer)
        self.assertEqual(evidence[0]['sku'], 'SC-PB27')

        answer, evidence = recommend_product('推荐能泡水的充电宝', self.sources)
        self.assertIn('无法确认', answer)
        self.assertEqual(evidence, [])

        answer, evidence = recommend_product('推荐能带上飞机的充电宝', self.sources)
        self.assertIn('无法核验', answer)
        self.assertEqual(evidence, [])

    def test_unspecified_recommendation_asks_one_clarifying_question(self):
        answer, evidence = recommend_product('给我推荐个商品', self.sources)
        self.assertIn('哪类商品', answer)
        self.assertEqual(evidence, [])
        self.assertIsNone(recommend_product('SC-PB27 多重？', self.sources))
        self.assertIsNone(recommend_product('你之前推荐的充电宝能退吗？', self.sources))

    def test_broad_earphone_options_use_distinct_verified_features(self):
        question = '嗯，有没有耳机推荐'
        answer, evidence = recommend_product(question, list(reversed(self.sources)))
        self.assertEqual([source['sku'] for source in evidence], ['EW-Buds1', 'EW-Buds2'])
        self.assertRegex(answer, r'轻便.*EW-Buds1.*半入耳.*通勤.*主动降噪.*EW-Buds2')
        self.assertIn('您更偏向哪种用途？', answer)

        explanation, cited = explain_recommendation(question, evidence)
        self.assertEqual(cited, evidence)
        self.assertRegex(explanation, r'EW-Buds1.*3\.8 克.*试戴.*EW-Buds2.*地铁.*通透模式')
        legacy, legacy_sources = explain_recommendation(question, evidence[:1])
        self.assertEqual(legacy_sources, evidence[:1])
        self.assertRegex(legacy, r'半入耳.*3\.8 克.*轻|半入耳.*3\.8 克.*佩戴')
        self.assertNotIn('EW-Buds2', legacy)

        _, three = recommend_product('推荐三款耳机', self.sources)
        self.assertEqual([source['sku'] for source in three], ['EW-Buds1', 'EW-Buds2', 'EW-Buds3'])
        one = '给我推荐一款耳机'
        self.assertTrue(recommendation_needs_preferences(one))
        self.assertEqual(recommend_product(one, self.sources), (recommendation_preference_question(one), []))

    def test_explicit_noise_requirement_never_uses_call_noise_reduction_as_anc(self):
        _, evidence = recommend_product('推荐能主动降噪的耳机，预算 200 元', self.sources)
        self.assertFalse(evidence)
        _, evidence = recommend_product('推荐能主动降噪的耳机，预算 300 元', self.sources)
        self.assertEqual([source['sku'] for source in evidence], ['EW-Buds2'])

    def test_subjective_recommendation_asks_what_the_customer_prefers(self):
        question = '给我推荐一款符合我气质的耳机'
        self.assertTrue(recommendation_needs_preferences(question))
        self.assertTrue(recommendation_needs_preferences(question + '，预算三百元'))
        self.assertFalse(recommendation_needs_preferences('为什么你推荐这款符合我气质的耳机'))
        answer, evidence = recommend_product(question, self.sources)
        self.assertIn('喜欢', answer)
        self.assertIn('风格', answer)
        self.assertNotIn('EW-Buds', answer)
        self.assertEqual(evidence, [])

    def test_resolved_style_uses_documented_colors_commute_features_and_budget(self):
        question = '简约一点，通勤用，三百以内。给我推荐一款符合我气质的耳机'
        self.assertFalse(recommendation_needs_preferences(question))
        answer, evidence = recommend_product(question, self.sources)
        self.assertEqual([source['sku'] for source in evidence], ['EW-Buds2'])
        self.assertIn('299 元', answer)
        self.assertIn('黑', answer)
        self.assertIn('白', answer)
        self.assertIn('主动降噪', answer)
        self.assertIn('通勤', answer)
        self.assertIn('商品图', answer)
        explanation, cited = explain_recommendation(question, evidence)
        self.assertIn('通透模式', explanation)
        self.assertIn('黑', explanation)
        self.assertEqual(cited, evidence)

        answer, evidence = recommend_product(
            '通勤用，两百五十元以内。给我推荐一款简约的耳机。', self.sources,
        )
        self.assertIn('159 元', answer)
        self.assertNotIn('主动降噪', answer)
        self.assertEqual([source['sku'] for source in evidence], ['EW-Buds1'])

        _, evidence = recommend_product('通勤耳机推荐，一百以内', self.sources)
        self.assertEqual(evidence, [])

    def test_legacy_style_explanation_limits_the_conclusion_to_verified_weight(self):
        earlier = [source for source in self.sources if source.get('sku') == 'EW-Buds1']
        answer, evidence = explain_recommendation('给我推荐一款符合我气质的耳机', earlier)
        self.assertIn('EW-Buds1', answer)
        self.assertIn('3.8 克', answer)
        self.assertIn('轻便', answer)
        self.assertIn('喜欢', answer)
        self.assertIn('风格', answer)
        self.assertIn('？', answer)
        self.assertEqual(evidence, earlier)

    def test_bag_recommendation_clarifies_size_or_look_before_choosing(self):
        question = '给我推荐适合我新买的包包的充电宝'
        self.assertTrue(recommendation_needs_preferences(question))
        answer, sources = recommend_product(question, self.sources)
        self.assertIn('放进包里', answer)
        self.assertIn('颜色搭配', answer)
        self.assertEqual(sources, [])
        self.assertNotIn('SC-PB05', answer)

        for preference, expected in (('想放得下', '可用厚度'), ('我更在意颜色搭配', '您的包是什么颜色')):
            query = f'{preference}。{question}'
            self.assertTrue(recommendation_needs_preferences(query))
            self.assertIn(expected, recommendation_preference_question(query))

        previous = [source for source in self.sources if source.get('sku') == 'SC-PB05']
        explanation, evidence = explain_recommendation(question, previous)
        self.assertEqual(evidence, previous)
        self.assertIn('常用夹层', explanation)
        self.assertIn('机身约 96 × 63 × 11.8 毫米', explanation)
        self.assertIn('曜石黑', explanation)
        self.assertIn('颜色搭配', explanation)

    def test_bag_recommendation_filters_measured_shape_or_known_colors(self):
        question = '给我推荐搭配包包的充电宝'
        size = f'包内尺寸10×7×2厘米。{question}'
        answer, sources = recommend_product(size, self.sources)
        self.assertEqual([source['sku'] for source in sources], ['SC-PB05'])
        self.assertIn('96 × 63 × 11.8 毫米', answer)
        self.assertIn('余量', answer)

        answer, sources = recommend_product(f'只想要小巧轻便。{question}', self.sources)
        self.assertEqual([source['sku'] for source in sources], ['SC-PB05'])
        self.assertIn('内袋', answer)

        answer, sources = recommend_product(f'希望是黑色。{question}', self.sources)
        self.assertEqual([source['sku'] for source in sources], ['SC-PB05'])
        self.assertIn('曜石黑', answer)
        self.assertIn('实拍图', answer)
        self.assertNotIn('星河银', answer)

        answer, sources = recommend_product(f'包是红色，充电宝想要黑色。{question}', self.sources)
        self.assertEqual([source['sku'] for source in sources], ['SC-PB05'])
        self.assertIn('曜石黑', answer)

        _, sources = recommend_product(f'包内尺寸10×7×1厘米。{question}', self.sources)
        self.assertEqual(sources, [])

    def test_fit_check_quotes_product_size_and_distinguishes_missing_thickness(self):
        previous = [source for source in self.sources if source.get('sku') == 'SC-PB05']
        unknown = answer_product_fit('放得进去吗？', previous)
        self.assertIn('96 × 63 × 11.8 毫米', unknown)
        self.assertIn('包口', unknown)
        self.assertIn('可用厚度', unknown)
        partial = answer_product_fit('包内尺寸10×7厘米，能放下吗？', previous)
        self.assertIn('长和宽有空间', partial)
        self.assertIn('厚度', partial)
        self.assertIn('余量', answer_product_fit('包里10×7×2厘米', previous))
        self.assertIn('余量', answer_product_fit('包里长10厘米、宽7厘米、厚20毫米', previous))
        self.assertIn('余量', answer_product_fit('SC-PB05 放得进10×7×2cm的包吗', previous))
        self.assertIn('空间不足', answer_product_fit('包里10×7×1厘米', previous))
        self.assertIn('可用厚度', answer_product_fit('这款机身96×63×11.8毫米，放得下吗？', previous))

    def test_morning_run_recommends_sport_fit_or_lighter_options(self):
        short_answer, short_evidence = recommend_product('晨跑耳机推荐', self.sources)
        self.assertEqual([source['sku'] for source in short_evidence], ['EW-Buds3'])
        self.assertIn('耳挂式', short_answer)

        question = '我需要去晨跑，给我推荐几款耳机'
        answer, evidence = recommend_product(question, self.sources)
        self.assertEqual([source['sku'] for source in evidence], ['EW-Buds3', 'EW-Buds1'])
        self.assertIn('单耳约 6.2 克', answer)
        self.assertIn('单耳约 3.8 克', answer)
        self.assertIn('更在意重量', answer)
        self.assertNotIn('不会掉', answer)

        lower_budget, evidence = recommend_product('晨跑耳机推荐，预算180元', self.sources)
        self.assertEqual([source['sku'] for source in evidence], ['EW-Buds1'])
        self.assertIn('防汗防泼溅', lower_budget)

    def test_recommendation_follow_up_uses_only_previously_shown_products(self):
        question = '晨跑给我推荐几款耳机'
        _, shown = recommend_product(question, self.sources)
        answer, sources = explain_recommendation(question, shown)
        self.assertEqual(sources, shown)
        self.assertIn('晨跑', answer)
        self.assertIn('耳挂', answer)
        self.assertIn('更在意重量', answer)
        self.assertNotIn('EW-Buds2', answer)
        meaning, sources = explain_recommendation(question, shown, clarify=True)
        self.assertEqual(sources, shown)
        self.assertIn('简单说', meaning)
        self.assertNotEqual(meaning, answer)
        self.assertIsNone(explain_recommendation(question, []))

    def test_alternatives_skip_previously_shown_products_and_limit_long_lists(self):
        question = '晨跑给我推荐五款耳机'
        _, original = recommend_product(question, self.sources)
        self.assertEqual(len(original), 3)
        excluded = [source['sku'] for source in original]
        answer, alternatives = recommend_product(question, self.sources, excluded_skus=excluded)
        self.assertEqual(len(alternatives), 1)
        self.assertNotIn(alternatives[0]['sku'], excluded)
        self.assertTrue(all(sku not in answer for sku in excluded))

    def test_compact_comparison_uses_same_exact_spec_fields_for_each_product(self):
        products = [{**source, 'source_section': source['heading']} for source in self.sources
                    if source.get('sku') in {'SC-PB20', 'SC-PB27'}]
        result = {'retrieval_mode': 'catalog_exact', 'sources': products}
        question = 'SC-PB20 和 SC-PB27 有什么区别？'
        for voice in (False, True):
            answer, evidence = catalog_quote(question, result, voice=voice)
            self.assertEqual(evidence, products)
            self.assertEqual(answer.count('电池容量'), 2)
            self.assertEqual(answer.count('净重'), 2)
            self.assertEqual(answer.count('输出'), 2)
            self.assertNotIn('包装', answer)
            self.assertNotIn('颜色', answer)
            self.assertIn('415g', answer)
            self.assertIn('568g', answer)
            if voice:
                self.assertNotIn('**', answer)

    def test_group_catalog_fields_follow_the_order_of_verified_products(self):
        products_by_sku = {source['sku']: source for source in self.sources if source.get('sku')}
        products = [products_by_sku[sku] for sku in ('EW-Buds2', 'EW-Buds1')]
        result = {'retrieval_mode': 'catalog_exact', 'sources': products}
        answer, evidence = catalog_quote('这两个分别什么颜色', result)
        self.assertEqual(evidence, products)
        self.assertEqual(answer.split('\n\n'), ['EW-Buds2\n颜色：白 / 黑', 'EW-Buds1\n颜色：白 / 黑 / 粉'])

        speech, evidence = catalog_quote('两款续航分别多久？', result, voice=True)
        self.assertEqual(evidence, products)
        self.assertTrue(speech.startswith('EW-Buds2。续航：降噪开单次 5 小时 / 关降噪 7 小时'))
        self.assertIn('EW-Buds1。续航：单次 6 小时，搭配充电盒共 28 小时', speech)
        self.assertNotIn('\n', speech)

    def test_group_catalog_quote_preserves_known_fields_when_one_item_lacks_a_field(self):
        products = [source for source in self.sources if source.get('sku') in {'EW-Buds2', 'EW-Buds3'}]
        result = {'retrieval_mode': 'catalog_exact', 'sources': products}
        answer, evidence = catalog_quote('这两款分别是什么颜色，充电盒尺寸呢？', result)
        self.assertEqual(evidence, products)
        buds2, buds3 = answer.split('\n\n')
        self.assertIn('充电盒尺寸：60 × 48 × 26mm', buds2)
        self.assertNotIn('未注明', buds2)
        self.assertIn('颜色：荧光绿 / 黑', buds3)
        self.assertIn('当前资料未注明尺寸', buds3)

        answer, evidence = catalog_quote('这两款各是什么材质？', result)
        self.assertIn('未注明', answer)
        self.assertEqual(evidence, [])
        self.assertIsNone(catalog_quote('这两款分别什么颜色，拆封后还能退货吗？', result))

    def test_mock_returns_answers_current_policy_with_only_its_evidence(self):
        answer, evidence = mock_support_answer('耳机拆封后能退吗？', self.sources)
        self.assertIn('必要查验', answer)
        self.assertIn('商品完好', answer)
        self.assertIn('对应订单和平台公示', answer)
        self.assertIn('无理由退货', evidence[0]['heading'])
        self.assertNotIn('发票', answer)
        self.assertLess(len(answer), 150)

    def test_return_answer_varies_with_product_condition(self):
        cases = [('我开封激活了还能退吗', '使用程度'), ('我用了两天还能退吗', '使用程度'),
                 ('充电线丢了还能退吗', '缺少哪些配件'), ('还没拆封可以退吗', '包装还没拆'),
                 ('我只是拆封看了一下还能退吗', '查验')]
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertTrue(return_eligibility_question(question))
                answer, evidence = answer_return_eligibility(question, self.sources)
                self.assertIn(expected, answer)
                self.assertNotIn('？', answer)
                self.assertEqual([item['id'] for item in evidence], ['ecommerce_demo_v1_0_9'])

        invoice = [item for item in self.sources if '发票邮寄费' in item['heading']]
        answer, sources = answer_return_eligibility('我拆封了还能退吗', invoice)
        self.assertEqual(sources, [])
        self.assertIn('订单页', answer)
        for question in ('发票邮寄费还能退吗', '退款多久到账', '问一下颜色，拆封后能退吗？'):
            with self.subTest(question=question):
                self.assertFalse(return_eligibility_question(question))
                self.assertFalse(combined_return_question(question))

    def test_mock_policy_without_matching_source_does_not_repeat_other_evidence(self):
        invoice = [source for source in self.sources if '发票内容开错' in source['heading']]
        answer, evidence = mock_support_answer('耳机拆封后能退吗？', invoice)
        self.assertIn('无法确认', answer)
        self.assertNotIn('发票', answer)
        self.assertEqual(evidence, [])

    def test_mock_quality_returns_freight_chooses_seller_rule(self):
        answer, evidence = mock_support_answer('质量问题退货的运费谁承担？', self.sources)
        self.assertIn('卖家承担', answer)
        self.assertIn('质量问题退货的运费', evidence[0]['heading'])
