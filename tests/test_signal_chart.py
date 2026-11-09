from datetime import datetime,timedelta
import copy
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from chanmodel.signal_chart import build_annotations,attach_chart_annotations
from chanmodel.v2.divergence import compare_divergences,make_signals
from chanmodel.v2.recursive import centers_from_units
from chanmodel.model import Candle
from tests.test_recursive_v2 import units


def panel():
    origin=datetime.fromisoformat('2026-01-05T09:35:00+08:00')
    bars=[{'source_index':i,'closed_at':(origin+timedelta(minutes=i*5)).isoformat(),
           'open':10,'high':40,'low':0,'close':10,'is_closed':True} for i in range(125)]
    return {'period':'5m','bars':bars,'all_signals':[],'structure_layers':[],
            'complete_evidence':{'divergence_comparisons':[]}}


def point(**kwargs):
    return {'id':'third:one','class':'third_buy','side':'buy','rank':1,'anchor':10,
            'available_at':20,'price':10,'invalidation':9,'terminal_range':[5,10],
            'state':'awaiting_lower_confirmation',**kwargs}


class SignalChartTests(unittest.TestCase):
    def test_marker_keeps_distinct_endpoint_and_evidence_time(self):
        p=panel();p['all_signals']=[point()];before=copy.deepcopy(p)
        result=build_annotations(p)['objects'][0]
        self.assertEqual((result['anchor'],result['available_at'],result['delay_bars']),(10,20,10))
        self.assertNotEqual(result['anchor_time'],result['available_time'])
        self.assertEqual(result['state'],'candidate');self.assertIsNone(result['confirmed_time'])
        self.assertEqual(p,before)

    def test_all_six_formula_paths_draw_same_side_with_named_references(self):
        classes=set()
        for mirror in [False,True]:
            values=[30,20,28,21,26,14,18,12,17,10,13,9]
            source=units([40-x for x in values] if mirror else values)
            bars=[Candle(i,20,40,0,20) for i in range(125)]
            sign=1 if mirror else -1
            hist=[{'histogram':sign*(2 if 40<=i<=50 else .4 if 80<=i<=90 else .01),'dif':10 if i<51 else 0,'dea':0} for i in range(125)]
            centers=centers_from_units(source,1,125);comps=compare_divergences(source,centers,bars,hist,1,125)
            p=panel();p.update(all_signals=make_signals(source,centers,comps,bars,1,hist),
                              structure_layers=[{'rank':1,'centers':centers,'completed_types':[]}],signal_units=source)
            p['complete_evidence']['divergence_comparisons']=comps
            result=build_annotations(p);self.assertFalse(result['rejected'])
            for obj in result['objects']:
                classes.add(obj['class']);self.assertTrue(obj['references']);self.assertIsNotNone(obj['center'])
                if obj['class'].startswith('first'):self.assertIsNotNone(obj['comparison'])
                if obj['class'].startswith('second'):self.assertIsNotNone(obj['first_context'])
                if obj['class'].startswith('third'):self.assertIn('首次回试',[r['label'] for r in obj['references']])
        self.assertEqual(classes,{'first_buy','second_buy','third_buy','first_sell','second_sell','third_sell'})

    def test_only_matching_witnessed_trigger_can_fill_arrow(self):
        p=panel();p['all_signals']=[point()]
        link={'parent_signal':'third:one','trade_period':'5m','side':'buy','rank':1,'state':'structurally_triggered_under_profile',
              'confirmation':{'id':'lower','available_time':p['bars'][21]['closed_at']},
              'trigger':{'id':'fine','available_time':p['bars'][23]['closed_at']}}
        result=build_annotations(p,[link])['objects'][0]
        self.assertEqual(result['state'],'triggered');self.assertEqual(result['confirmed_index'],23)
        for change in [{'trade_period':'day'},{'rank':2},{'trigger':{}},{'side':'sell'},
                       {'trigger':{'id':'fine','available_time':p['bars'][19]['closed_at']}}]:
            result=build_annotations(p,[{**link,**change}])['objects'][0]
            self.assertEqual(result['state'],'candidate')

    def test_future_availability_and_open_evidence_never_plot(self):
        p=panel();p['all_signals']=[point()]
        result=build_annotations(p,cutoff=p['bars'][19]['closed_at'])
        self.assertEqual(result['objects'],[])
        self.assertEqual(result['rejected'][0]['reason'],'not_available_at_cutoff')
        p['bars'][20]['is_closed']=False;self.assertFalse(build_annotations(p)['objects'])

    def test_finer_trigger_maps_forward_to_parent_close_and_never_backfills(self):
        p=panel();p['all_signals']=[point(state='confirmed')]
        earlier=datetime.fromisoformat(p['bars'][23]['closed_at'])-timedelta(minutes=2)
        link={'parent_signal':'third:one','trade_period':'5m','side':'buy','rank':1,
              'state':'structurally_triggered_under_profile',
              'confirmation':{'id':'lower','available_time':p['bars'][21]['closed_at']},
              'trigger':{'id':'fine','available_time':earlier.isoformat()}}
        self.assertEqual(build_annotations(p)['objects'][0]['state'],'candidate')
        a=build_annotations(p,[link])['objects'][0]
        self.assertEqual(a['confirmed_index'],23)
        b=build_annotations(p,[link],cutoff=p['bars'][22]['closed_at'])['objects'][0]
        self.assertEqual(b['state'],'candidate');self.assertIsNone(b['confirmed_index'])

    def test_invalidated_points_preserved_for_opt_in_history(self):
        p=panel();p['all_signals']=[point(state='invalidated')]
        value=build_annotations(p)['objects'][0];self.assertEqual(value['state'],'invalidated')
        self.assertEqual(value['state_label'],'已失效')

    def test_old_panel_date_labels_resolve_without_changing_time_index(self):
        p=panel()
        for b in p['bars']:
            b['date_cn']=datetime.fromisoformat(b.pop('closed_at')).strftime('%Y-%m-%d %H:%M')
        p['all_signals']=[point()]
        chart={'panels':[p],'as_of':'2026-01-05T20:00:00+08:00'};attach_chart_annotations(chart)
        self.assertEqual(chart['panels'][0]['signal_chart']['objects'][0]['available_at'],20)

    def test_bad_rank_indices_nonfinite_and_side_are_explained(self):
        for change in [{'anchor':-1},{'available_at':False},{'price':float('nan')},{'side':'sell'}]:
            p=panel();p['all_signals']=[point(**change)];result=build_annotations(p)
            self.assertFalse(result['objects']);self.assertEqual(len(result['rejected']),1)
        p=panel();p['all_signals']=[point(center_id='wrong')];p['structure_layers']=[{'centers':[{'id':'wrong','rank':2}]}]
        self.assertEqual(build_annotations(p)['rejected'][0]['reason'],'center_rank_mismatch')

    @unittest.skipUnless(shutil.which('node'),'Node is optional; core algorithm has no Node dependency')
    def test_dense_labels_remain_inside_viewport_and_do_not_overlap(self):
        path=Path(__file__).resolve().parents[1]/'workbench/web/signal-chart.js'
        code='''require(process.argv[1]);const items=Array.from({length:12},(_,i)=>({id:String(i),label:'三买·候选',x:150+i%2,y:100,side:i%2?'buy':'sell'}));const boxes=SignalChart.layoutMarkers(items,360,300);process.stdout.write(JSON.stringify(boxes));'''
        result=subprocess.run(['node','-e',code,str(path)],capture_output=True,text=True,encoding='utf-8',check=True)
        boxes=json.loads(result.stdout);self.assertEqual(len(boxes),12)
        visible=[x['box'] for x in boxes if x['label_visible']];self.assertGreaterEqual(len(visible),8)
        for i,a in enumerate(visible):
            self.assertGreaterEqual(a['x'],0);self.assertLessEqual(a['x']+a['width'],360)
            self.assertGreaterEqual(a['y'],0);self.assertLessEqual(a['y']+a['height'],300)
            for b in visible[i+1:]:
                self.assertFalse(a['x']<b['x']+b['width'] and a['x']+a['width']>b['x'] and a['y']<b['y']+b['height'] and a['y']+a['height']>b['y'])


if __name__=='__main__':unittest.main()
