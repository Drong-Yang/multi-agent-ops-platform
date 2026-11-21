"""Fusion behavior contracts, written before the new graph implementation."""
import os
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

os.environ['MOCK_MODE'] = 'true'

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from app.fusion.workflow import build_fusion_graph, initial_fusion_state, merge_check_route


class FusionWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_consultation_evidence_survives_select_rewrite_and_approval_without_images(self):
        graph = build_fusion_graph(InMemorySaver())
        state = initial_fusion_state(
            task_id='task-contract', owner_id='owner', topic='SC-PB27 重量与尺寸',
            content_type='faq', snapshot_id='snapshot-contract',
            evidence=[{'chunk_id':'sku-pb27','sku':'SC-PB27','content':'SC-PB27 重量568g，尺寸153×80×33mm。',
                       'source_id':'products','knowledge_snapshot_id':'snapshot-contract'}],
            content_brief={'audience':'选购者','min_chars':50,'max_chars':1500},
        )
        cfg={'configurable':{'thread_id':'fusion-contract'}}
        await graph.ainvoke(state,cfg)
        snap=await graph.aget_state(cfg,subgraphs=True)
        self.assertTrue(snap.tasks)
        topics=snap.values.get('generated_topics',[])
        # A graph interrupt must expose topics at parent state, not lose them in a child.
        self.assertTrue(topics)
        await graph.ainvoke(Command(resume={'selected_topic':topics[0]}),cfg)
        snap=await graph.aget_state(cfg)
        self.assertEqual(snap.values['knowledge_snapshot_id'],'snapshot-contract')
        self.assertIn('568g',snap.values['article_content'])
        self.assertTrue(snap.values['fact_check_report']['passed'],snap.values['fact_check_report'])
        first=snap.values['article_versions'][-1]['version_id']
        await graph.ainvoke(Command(resume={'action':'reject','feedback':'说明参数以资料为准'}),cfg)
        snap=await graph.aget_state(cfg)
        self.assertNotEqual(first,snap.values['article_versions'][-1]['version_id'])
        self.assertEqual(snap.values['article_versions'][-1]['parent_version_id'],first)
        self.assertEqual(snap.values['evidence_refs'][0]['chunk_id'],'sku-pb27')
        await graph.ainvoke(Command(resume={'action':'approve'}),cfg)
        snap=await graph.aget_state(cfg)
        self.assertEqual(snap.values['status'],'text_approved')
        self.assertFalse(snap.next)
        self.assertEqual(snap.values.get('image_urls',[]),[])

    async def test_fact_failure_routes_to_bounded_revision_even_when_format_passes(self):
        state={'quality_report':{'status':'passed','checks':[]},
               'fact_check_report':{'passed':False,'issues':['重量错误']},
               'quality_policy':{'max_auto_per_review':1,'max_auto_total':3,'token_budget':30000},
               'auto_revisions_this_review':0,'auto_revision_count':0,'quality_tokens_used':0,
               'article_content':'SC-PB27 重量668g','selected_topic':'重量','content_brief':{}}
        result=merge_check_route(state)
        self.assertEqual(result['pending_revision_source'],'auto')
        self.assertIn('重量错误',result['auto_feedback'])
        state.update(result,auto_revisions_this_review=1)
        self.assertEqual(merge_check_route(state)['pending_revision_source'],'')

    async def test_approval_does_not_bypass_failed_fact_report(self):
        # This guard belongs to the graph as well as the eventual publish transaction.
        from app.fusion.workflow import fusion_human_review
        with patch('app.fusion.workflow.interrupt',return_value={'action':'approve'}):
            result=await fusion_human_review({'article_content':'wrong','fact_check_report':{'passed':False}})
        self.assertNotEqual(result.update.get('status'),'text_approved')


if __name__ == '__main__': unittest.main()
