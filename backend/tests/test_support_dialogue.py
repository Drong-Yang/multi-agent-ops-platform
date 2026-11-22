import unittest

from app.fusion.support_dialogue import dialogue_window, resolve_dialogue


def turn(question, route='recommendation', skus=(), *, snapshot='current', **fields):
    return {
        'turn_id': question,
        'question': question,
        'answer_route': route,
        'knowledge_snapshot_id': snapshot,
        'sources': [{'chunk_id': sku, 'kind': 'product', 'sku': sku} for sku in skus],
        **fields,
    }


class SupportDialogueTests(unittest.TestCase):
    def test_bare_why_and_what_do_you_mean_recover_the_recommendation(self):
        original = turn('我需要去晨跑，给我推荐一款耳机', skus=('EW-Buds1',))
        failed = turn('为什么', 'no_evidence')
        for question, intent in [('为什么', 'reason'), ('嗯，什么意思？', 'clarification'),
                                 ('呃，你为什么推荐这款？', 'reason')]:
            with self.subTest(question=question):
                memory = resolve_dialogue(question, [original, failed], 'current')
                self.assertEqual(memory.intent, intent)
                self.assertIs(memory.anchor, original)
                self.assertIs(memory.evidence_turn, original)
                self.assertEqual(memory.topic_query, original['question'])
                self.assertEqual(memory.sku, 'EW-Buds1')
                self.assertEqual(memory.recent, (original,))

    def test_rephrased_reason_remains_in_the_same_recommendation(self):
        original = turn('给我推荐一款符合我气质的耳机', skus=('EW-Buds1',))
        explanation = turn('为什么', 'context_followup', skus=('EW-Buds1',))
        correction = '我问的是为什么你推荐这款耳机'
        for question in (correction, '刚才你为啥选那款耳机', '你推荐这款耳机的理由是什么',
                         '不是，我想问的是你为什么推荐这款耳机'):
            with self.subTest(question=question):
                memory = resolve_dialogue(question, [original, explanation], 'current')
                self.assertEqual(memory.intent, 'reason')
                self.assertIs(memory.anchor, original)
                self.assertEqual(memory.sku, 'EW-Buds1')

        old_misroute = turn(correction, 'model_assist', dialogue_intent='question')
        memory = resolve_dialogue('什么意思', [original, explanation, old_misroute], 'current')
        self.assertIs(memory.anchor, original)
        self.assertIs(memory.evidence_turn, explanation)

    def test_reason_disambiguates_singular_or_ordinal_without_losing_the_recommendation(self):
        original = turn('晨跑耳机推荐', skus=('EW-Buds1', 'EW-Buds2'))
        singular = resolve_dialogue('为什么你推荐这款耳机', [original], 'current')
        self.assertEqual(singular.intent, 'reason')
        self.assertTrue(singular.ambiguous_product_reference)
        self.assertFalse(resolve_dialogue('为什么', [original], 'current').ambiguous_product_reference)
        ordinal = resolve_dialogue('为什么推荐第二款耳机', [original], 'current')
        self.assertEqual(ordinal.intent, 'reason')
        self.assertEqual(ordinal.sku, 'EW-Buds2')

    def test_reason_uses_recommendation_evidence_after_a_parameter_quote(self):
        original = turn('晨跑耳机推荐', skus=('EW-Buds1', 'EW-Buds2'))
        quote = turn('第二款多少钱', 'catalog_quote', skus=('EW-Buds2',))
        memory = resolve_dialogue('为什么你推荐这款耳机', [original, quote], 'current')
        self.assertIs(memory.evidence_turn, original)
        self.assertEqual(memory.sku, 'EW-Buds2')
        why = resolve_dialogue('为什么', [original, quote], 'current')
        self.assertIs(why.evidence_turn, original)
        self.assertIsNone(why.sku)

    def test_only_ambiguous_references_admit_a_semantic_override(self):
        original = turn('晨跑耳机推荐', skus=('EW-Buds1',))
        question = '这款耳机是从哪些角度考虑的？'
        for uncertain in (question, '不是，我是说你选它的依据'):
            with self.subTest(question=uncertain):
                memory = resolve_dialogue(uncertain, [original], 'current')
                self.assertEqual(memory.intent, 'ambiguous')
                self.assertIs(memory.anchor, original)
                override = resolve_dialogue(uncertain, [original], 'current', intent_override='reason')
                self.assertEqual(override.intent, 'reason')
                self.assertIs(override.anchor, original)
        self.assertIsNone(resolve_dialogue(question, [original], 'current', intent_override='new_question').anchor)
        self.assertEqual(resolve_dialogue('我想问的是快递丢了怎么处理', [original], 'current',
                                         intent_override='reason').intent, 'question')
        self.assertEqual(resolve_dialogue('为什么你推荐这款耳机', [original], 'current',
                                         intent_override='new_question').intent, 'reason')
        self.assertEqual(resolve_dialogue(question, [], 'current', intent_override='reason').intent, 'ambiguous')

        new_topic = turn(question, 'model_assist', dialogue_intent='question', dialogue_intent_source='model')
        memory = resolve_dialogue('那为什么呢', [original, new_topic], 'current')
        self.assertIs(memory.anchor, new_topic)
        self.assertIsNone(memory.evidence_turn)

    def test_preference_reply_completes_only_a_pending_recommendation(self):
        original = turn('推荐一款符合我气质的耳机', 'recommendation_clarification', recommendation_pending=True)
        ignored = turn('为什么', 'no_evidence')
        preference = '简约一点，预算三百'
        memory = resolve_dialogue(preference, [original, ignored], 'current')
        self.assertEqual(memory.intent, 'preferences')
        self.assertTrue(memory.recommendation_pending)
        self.assertIs(memory.anchor, original)
        self.assertEqual(memory.topic_query, original['question'])
        self.assertTrue(resolve_dialogue('为什么', [original], 'current').recommendation_pending)
        for question in ('发票怎么开', '简约风键盘推荐', '通勤时跑步需要注意什么'):
            self.assertEqual(resolve_dialogue(question, [original], 'current').intent, 'question')
        self.assertEqual(resolve_dialogue('运动感', [original], 'current').intent, 'preferences')

        combined = '推荐一款符合我气质的耳机。简约一点，预算三百'
        selected = turn(preference, skus=('EW-Buds1',), dialogue_intent='preferences', recommendation_query=combined)
        memory = resolve_dialogue('为什么你推荐这款耳机', [original, selected], 'current')
        self.assertIs(memory.anchor, original)
        self.assertIs(memory.evidence_turn, selected)
        self.assertEqual(memory.topic_query, combined)
        self.assertFalse(memory.recommendation_pending)
        self.assertEqual(resolve_dialogue(preference, [original, selected], 'current').intent, 'question')

    def test_alternatives_stay_in_the_topic_and_keep_already_seen_models(self):
        original = turn('晨跑耳机推荐', skus=('EW-Buds1',))
        follow = turn('还有别的吗', skus=('EW-Buds2',))
        memory = resolve_dialogue('再推荐一款呢', [original, follow], 'current')
        self.assertEqual(memory.intent, 'alternatives')
        self.assertEqual(memory.sku, 'EW-Buds2')
        self.assertEqual(memory.seen_skus, ('EW-Buds1', 'EW-Buds2'))

    def test_pending_bag_recommendation_can_bind_color_use_and_measurements(self):
        pending = turn('给我推荐适合我新买的包包的充电宝', 'clarification', recommendation_pending=True)
        for answer in ('装得下', '我最在意它能放得下', '颜色想配我那个浅米色的包', '选黑的',
                       '我主要看尺寸，小一点', '12 × 8 × 2 cm 的包', '106×70×22mm'):
            with self.subTest(answer=answer):
                reply = resolve_dialogue(answer, [pending], 'current')
                self.assertEqual(reply.intent, 'preferences')
                self.assertIs(reply.anchor, pending)
                self.assertEqual(reply.topic_query, pending['question'])
        for question in ('换个话题，黑色鼠标推荐', '耳机有哪些颜色？', '外观怎么清洁？'):
            with self.subTest(question=question):
                self.assertIsNone(resolve_dialogue(question, [pending], 'current').anchor)

    def test_appearance_followup_uses_the_product_even_when_category_is_said(self):
        original = turn('给我推荐个小包随身的充电宝', skus=('SC-PB05',))
        for question in ('有什么颜色', '这款有银色吗？', '这款充电宝的外观是什么样？', '外观呢',
                         '那有星河银色吗', '那款充电宝是什么材质', '这款磨砂吗'):
            with self.subTest(question=question):
                memory = resolve_dialogue(question, [original], 'current')
                self.assertEqual(memory.intent, 'product_followup')
                self.assertIs(memory.anchor, original)
                self.assertEqual(memory.sku, 'SC-PB05')
        for question in ('SC-PB10 有银色吗', '耳机有什么颜色', '这款充电宝为什么有杂音'):
            with self.subTest(question=question):
                memory = resolve_dialogue(question, [original], 'current')
                self.assertEqual(memory.intent, 'question')
                self.assertIsNone(memory.anchor)

    def test_fit_check_resolves_selected_or_explicit_product_without_assuming_fit(self):
        original = turn('给我推荐几款随身充电宝', skus=('SC-PB05', 'SC-PB10'))
        single = turn('给我推荐一款随身充电宝', skus=('SC-PB05',))
        for question in ('装得下吗', '这款充电宝塞得进我的包吗？', '能放进我的包吗'):
            with self.subTest(question=question):
                memory = resolve_dialogue(question, [single], 'current')
                self.assertEqual(memory.intent, 'fit_check')
                self.assertEqual(memory.sku, 'SC-PB05')

        unresolved = resolve_dialogue('那能装下吗', [original], 'current')
        self.assertEqual(unresolved.intent, 'fit_check')
        self.assertTrue(unresolved.ambiguous_product_reference)
        ordinal = resolve_dialogue('第二款能装进12×8×2cm的小包吗', [original], 'current')
        self.assertEqual(ordinal.intent, 'fit_check')
        self.assertEqual(ordinal.sku, 'SC-PB10')
        self.assertTrue(ordinal.ordinal_reference)
        explicit = resolve_dialogue('SC-PB27 放得进10×7×2cm的包？', [original], 'current')
        self.assertEqual(explicit.intent, 'fit_check')
        self.assertEqual(explicit.requested_sku, 'SC-PB27')
        self.assertIsNone(explicit.anchor)
        self.assertIsNone(explicit.sku)
        new_topic = resolve_dialogue('那手机能放进去吗', [original], 'current')
        self.assertEqual(new_topic.intent, 'question')
        self.assertIsNone(new_topic.anchor)

    def test_ordered_product_reference_selects_the_indicated_product(self):
        original = turn('晨跑耳机推荐', skus=('EW-Buds2', 'EW-Buds1'))
        memory = resolve_dialogue('那第二款多重呢？', [original], 'current')
        self.assertEqual(memory.intent, 'product_followup')
        self.assertEqual(memory.sku, 'EW-Buds1')
        ambiguous = resolve_dialogue('这款呢？', [original], 'current')
        self.assertEqual(ambiguous.intent, 'product_followup')
        self.assertEqual(ambiguous.product_skus, ('EW-Buds2', 'EW-Buds1'))
        self.assertIsNone(ambiguous.sku)
        self.assertTrue(ambiguous.ambiguous_product_reference)
        missing = resolve_dialogue('第三款多少钱？', [original], 'current')
        self.assertIsNone(missing.sku)
        self.assertTrue(missing.ambiguous_product_reference)

    def test_group_followup_uses_the_ordered_recommendations_after_a_single_quote(self):
        first = turn('推荐几款耳机给我', skus=('EW-Buds2', 'EW-Buds1'))
        single = turn('第一款多重', 'catalog_quote', skus=('EW-Buds2',))
        for question in ('这两个分别什么颜色', '刚才推荐那两款耳机有哪些配色', '它们分别多少钱',
                         '这俩各是什么颜色', '那两个分别啥颜色', '这两款有什么区别'):
            with self.subTest(question=question):
                memory = resolve_dialogue(question, [first, single], 'current')
                self.assertEqual(memory.intent, 'product_followup')
                self.assertTrue(memory.group_reference)
                self.assertIs(memory.anchor, first)
                self.assertIs(memory.evidence_turn, first)
                self.assertEqual(memory.product_skus, ('EW-Buds2', 'EW-Buds1'))
                self.assertFalse(memory.ambiguous_product_reference)
                self.assertIsNone(memory.sku)

    def test_group_followup_requires_a_matching_shortlist_in_the_active_topic(self):
        first = turn('推荐几款耳机给我', skus=('EW-Buds2', 'EW-Buds1'))
        wrong_count = resolve_dialogue('这三款分别什么颜色', [first], 'current')
        self.assertEqual(wrong_count.group_count, 3)
        self.assertTrue(wrong_count.ambiguous_product_reference)
        wrong_kind = resolve_dialogue('这两款充电宝分别什么颜色', [first], 'current')
        self.assertTrue(wrong_kind.ambiguous_product_reference)
        self.assertTrue(resolve_dialogue('这两个分别什么颜色', [], 'current').ambiguous_product_reference)
        other = turn('我买的充电宝不见了怎么办', 'model_assist')
        memory = resolve_dialogue('这两个分别什么颜色', [first, other], 'current')
        self.assertIs(memory.anchor, other)
        self.assertEqual(memory.product_skus, ())
        self.assertTrue(memory.ambiguous_product_reference)

    def test_implicit_policy_followup_uses_latest_user_product_category_only(self):
        first = turn('推荐几款耳机给我', skus=('EW-Buds2', 'EW-Buds1'))
        other = turn('我上周在你们家买的充电宝不见了怎么办', 'model_assist', answer='耳机物流信息')
        memory = resolve_dialogue('我拆封了还能退货吗', [first, other], 'current')
        self.assertEqual(memory.intent, 'policy_followup')
        self.assertIs(memory.anchor, other)
        self.assertEqual(memory.policy_subject, '充电宝')
        self.assertIsNone(memory.evidence_turn)
        legacy = turn('我拆封了还能退货吗', 'rag', dialogue_intent='question')
        retry = resolve_dialogue('开封了还能退吗', [first, other, legacy], 'current')
        self.assertIs(retry.anchor, other)
        self.assertEqual(retry.policy_subject, '充电宝')
        for independent in ('换个话题，我拆封了还能退货吗', '耳机拆封了还能退吗', '退货流程怎么走'):
            with self.subTest(independent=independent):
                memory = resolve_dialogue(independent, [first, other], 'current')
                self.assertEqual(memory.intent, 'question')
                self.assertIsNone(memory.anchor)
        for unclear in ([], [turn('快递没到怎么办', 'model_assist')],
                        [first], [turn('如果买了充电宝，第几天发货？', 'model_assist')],
                        [turn('我在同一订单买了充电宝和耳机', 'model_assist')]):
            with self.subTest(unclear=unclear):
                memory = resolve_dialogue('拆封了还能退货吗', unclear, 'current')
                self.assertEqual(memory.intent, 'question')
                self.assertFalse(memory.policy_subject)

        explicit_reference = resolve_dialogue('刚才那款拆开了还能退吗', [first], 'current')
        self.assertEqual(explicit_reference.policy_subject, '耳机')
        invoice = resolve_dialogue('那发票的邮寄费还能退吗', [first, other], 'current')
        self.assertIsNone(invoice.anchor)

    def test_recent_followup_links_keep_a_long_running_topic_without_crossing_a_new_question(self):
        first = turn('推荐几款耳机给我', skus=('EW-Buds2', 'EW-Buds1'))
        continued = [turn('第一款多重', 'catalog_quote', skus=('EW-Buds2',),
                          follow_up_to_turn_id=first['turn_id']) for _ in range(7)]
        active = dialogue_window([first, *continued])
        self.assertEqual(len(active), 7)
        self.assertIs(active[0], first)
        self.assertEqual(dialogue_window(active), active)
        memory = resolve_dialogue('这两个分别什么颜色', active, 'current')
        self.assertIs(memory.anchor, first)
        self.assertEqual(memory.product_skus, ('EW-Buds2', 'EW-Buds1'))
        unrelated = turn('我买的充电宝不见了怎么办', 'model_assist')
        crossed = dialogue_window([first, unrelated, *continued])
        self.assertEqual(len(crossed), 6)
        self.assertNotIn(first, crossed)
        self.assertFalse(resolve_dialogue('这两个分别什么颜色', crossed, 'current').product_skus)

        alternative = turn('还有别的吗', skus=('EW-Buds4', 'EW-Buds3'), follow_up_to_turn_id=first['turn_id'])
        latest = dialogue_window([first, alternative, *continued])
        self.assertEqual(latest[:2], (first, alternative))
        self.assertEqual(dialogue_window(latest), latest)
        memory = resolve_dialogue('这俩分别有什么颜色？', latest, 'current')
        self.assertEqual(memory.product_skus, ('EW-Buds4', 'EW-Buds3'))
        comparison = turn('这俩分别有什么颜色？', 'catalog_quote', skus=memory.product_skus,
                          follow_up_to_turn_id=first['turn_id'])
        ordinal = resolve_dialogue('第二款多重？', [*latest, comparison], 'current')
        self.assertEqual(ordinal.sku, 'EW-Buds3')

    def test_explicit_recommendation_reason_only_resolves_models_from_current_topic(self):
        original = turn('晨跑耳机推荐', skus=('EW-Buds1', 'EW-Buds2'))
        memory = resolve_dialogue('为什么推荐 EW Buds2？', [original], 'current')
        self.assertEqual(memory.intent, 'reason')
        self.assertEqual(memory.sku, 'EW-Buds2')
        missing = resolve_dialogue('为什么推荐 EW-Buds3？', [original], 'current')
        self.assertEqual(missing.requested_sku, 'EW-Buds3')
        self.assertIsNone(missing.sku)
        direct = resolve_dialogue('为什么 EW-Buds3 断连？', [original], 'current')
        self.assertEqual(direct.intent, 'question')

    def test_explicit_topic_does_not_inherit_previous_product(self):
        original = turn('晨跑耳机推荐', skus=('EW-Buds1',))
        for question in ('为什么耳机有杂音', '这款耳机为什么有杂音', '那 SC-PB27 多重？', '那发票怎么开',
                         '换个话题，推荐充电宝', '那明天会下雨吗', '有'):
            with self.subTest(question=question):
                memory = resolve_dialogue(question, [original], 'current')
                self.assertEqual(memory.intent, 'question')
                self.assertIsNone(memory.anchor)
        new_topic = turn('为什么耳机有杂音', 'no_evidence')
        memory = resolve_dialogue('那为什么呢', [original, new_topic], 'current')
        self.assertIs(memory.anchor, new_topic)
        self.assertIsNone(memory.evidence_turn)

    def test_contextual_next_step_uses_unverified_user_question_only(self):
        original = turn('耳机左侧没有声音', 'model_assist', skus=('EW-Buds1',), answer='想象中的规格')
        fillers = [turn('嗯，好吧', 'business_boundary'), turn('好的，谢谢', 'business_boundary')]
        memory = resolve_dialogue('那可以先查什么？', [original, *fillers], 'current')
        self.assertEqual(memory.intent, 'followup')
        self.assertEqual(memory.topic_query, '耳机左侧没有声音')
        self.assertEqual(memory.recent, (original,))
        self.assertIsNone(memory.evidence_turn)
        self.assertIsNone(memory.sku)

    def test_stale_or_retracted_evidence_is_not_carried_forward(self):
        for original in (turn('晨跑耳机推荐', skus=('EW-Buds1',), snapshot='old'),
                         turn('耳机不出声怎么办', 'support_wiki', wiki_asset_id='revoked', skus=('EW-Buds1',)),
                         turn('耳机怎样退换', 'rag', faq_asset_ids=['revoked'], skus=('EW-Buds1',))):
            with self.subTest(question=original['question']):
                memory = resolve_dialogue('为什么', [original], 'current')
                self.assertEqual(memory.topic_query, original['question'])
                self.assertIsNone(memory.evidence_turn)
                self.assertFalse(memory.seen_skus)
        valid = turn('耳机不出声怎么办', 'support_wiki', wiki_asset_id='live', skus=('EW-Buds1',))
        self.assertIs(resolve_dialogue('为什么', [valid], 'current', valid_wiki_ids={'live'}).evidence_turn, valid)

    def test_contextual_reply_can_focus_a_later_reference(self):
        original = turn('晨跑耳机推荐', skus=('EW-Buds1', 'EW-Buds2'))
        explanation = turn('第二款怎么样？', 'context_followup', skus=('EW-Buds2',))
        memory = resolve_dialogue('这款呢？', [original, explanation], 'current')
        self.assertEqual(memory.sku, 'EW-Buds2')
        self.assertIs(memory.evidence_turn, explanation)
        for question in ('第一款多重？', '为什么推荐 EW-Buds1？'):
            with self.subTest(question=question):
                memory = resolve_dialogue(question, [original, explanation], 'current')
                self.assertEqual(memory.sku, 'EW-Buds1')
                self.assertIs(memory.evidence_turn, original)

    def test_legacy_questions_work_and_memory_is_bounded(self):
        old = {'question': '晨跑耳机推荐', 'answer': 'EW-Buds1 不可验证的规格'}
        memory = resolve_dialogue('为什么', [old], 'current')
        self.assertEqual(memory.topic_query, old['question'])
        self.assertIsNone(memory.evidence_turn)
        failed = turn('为什么', 'no_evidence')
        memory = resolve_dialogue('什么意思', [old, *([failed] * 6)], 'current')
        self.assertEqual(memory.intent, 'clarification')
        self.assertIsNone(memory.anchor)
        self.assertEqual(memory.recent, ())
