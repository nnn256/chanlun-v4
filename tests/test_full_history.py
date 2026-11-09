from datetime import datetime,timedelta
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from chanmodel.model import Candle
from chanmodel.v2.series import Series,CN
from chanmodel.v2.engine import analyze
from workbench.compute import make_chart
from workbench.history import normalize,five_day,calendar_analysis,build_panel,timeshare_panel,bucket_end,load_capture


class FullHistoryTests(unittest.TestCase):
    def daily(self,count=400):
        start=datetime(2021,2,26,15,tzinfo=CN)
        return tuple(Candle(i,10+i%6,12+i%6,9+i%6,11+i%6,(start+timedelta(days=i)).isoformat(),1000,True) for i in range(count))

    def test_chart_loads_early_history_and_all_overlays(self):
        bars=self.daily();series=Series('sz300949','day','unadjusted',bars,'fixture');cutoff=bars[-1].closed_at
        report=analyze(series,cutoff)
        chart,summary=make_chart({'symbol':'sz300949','code':'300949','name':'test','exchange':'sz'},{'day':series},{'day':report},cutoff)
        panel=chart['panels'][0]
        self.assertEqual(len(panel['bars']),400)
        self.assertEqual(panel['bars'][0]['date_cn'],'2021-02-26 15:00')
        self.assertEqual(panel['visible_start'],0)
        self.assertEqual(panel['strokes'],report['base']['strokes'])

    def test_calendar_forming_candle_excluded_and_week_closed_friday(self):
        def row(date):return {'Time':date+'T15:00:00+08:00','Open':10000,'High':12000,'Low':9000,'Close':11000,'Volume':10}
        month=normalize({'list':[row('2026-10-30'),row('2026-11-06')]},'month','2026-11-08T00:00:00+08:00')
        self.assertEqual([b.closed for b in month],[True,False])
        report=calendar_analysis('sz300949','month',month,'2026-11-08T00:00:00+08:00','hash')
        self.assertEqual(report['input_audit']['bars_used'],1)
        week=normalize({'list':[row('2026-11-06')]},'week','2026-11-08T00:00:00+08:00')
        self.assertTrue(week[0].closed)

    def test_cutoff_does_not_leak_future_prices(self):
        rows=[{'Time':d+'T15:00:00+08:00','Open':10000,'High':12000,'Low':9000,'Close':11000,'Volume':10} for d in ['2021-02-26','2026-11-06']]
        bars=normalize({'list':rows},'day','2021-03-01T15:00:00+08:00')
        self.assertEqual(len(bars),1)

    def test_five_day_group_not_calendar_week(self):
        bars=self.daily(11);groups=five_day(bars)
        self.assertEqual(len(groups),3)
        self.assertEqual(groups[0].open,bars[0].open)
        self.assertEqual(groups[0].close,bars[4].close)
        self.assertEqual(groups[0].volume,5000)
        self.assertFalse(groups[-1].closed)

    def test_invalid_native_minutes_rejected(self):
        row={'Time':'2026-11-06T12:05:00+08:00','Open':10000,'High':12000,'Low':9000,'Close':11000,'Volume':10}
        with self.assertRaisesRegex(ValueError,'off-grid'):normalize({'list':[row]},'5m','2026-11-08T00:00:00+08:00')

    def test_changed_capture_is_rejected_before_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'raw').mkdir();(root/'receipts').mkdir()
            (root/'raw/day.json.gz').write_bytes(gzip.compress(b'{"data":{"list":[]}}'))
            (root/'receipts/day.json').write_text(json.dumps({'response_sha256':'wrong'}),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'hash mismatch'):load_capture(root,'sz300949','2026-11-08T00:00:00+08:00')

    def test_timeshare_is_line_never_fabricated_ohlc(self):
        rows=[]
        for i in range(240):
            minute=571+i if i<120 else 781+i-120
            rows.append({'Time':f'{minute//60:02}:{minute%60:02}','Price':10000,'Number':2})
        panel=timeshare_panel({'date':'20210226','Count':240,'List':rows},'20210226')
        self.assertEqual(panel['points'][0]['value'],10)
        self.assertNotIn('open',panel['points'][0])
        self.assertEqual(panel['points'][-1]['date_cn'],'2021-02-26 15:00')
        rows[0]['Time']='09:25'
        with self.assertRaisesRegex(ValueError,'off grid'):timeshare_panel({'date':'20210226','Count':240,'List':rows},'20210226')


if __name__=='__main__':unittest.main()
