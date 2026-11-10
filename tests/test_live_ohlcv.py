from datetime import datetime, timedelta
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chanmodel.tdx_data import CN
from workbench import live, reference_service


def response(today=True):
    dates=[]; date=datetime(2026,11,9,tzinfo=CN)
    while len(dates)<80:
        if date.weekday()<5:dates.append(date)
        date-=timedelta(days=1)
    rows=[]
    for i,date in enumerate(reversed(dates)):
        price=10000+int(1000*math.sin(i/6))
        rows.append(dict(Time=date.replace(hour=15).isoformat(),Open=price,High=price+150,
                         Low=price-100,Close=price+50,Volume=1234))
    if today:rows.append(dict(Time='2026-11-10T15:00:00+08:00',Open=11000,High=11200,Low=10900,Close=11100,Volume=900))
    return {'count':len(rows),'list':rows,'meta':{'source':'tdx','type':'day'}}


class LiveOHLCV(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.scope=patch.multiple(live,STATE=Path(self.tmp.name));self.scope.start();self.addCleanup(self.scope.stop)
        self.ref=patch.object(reference_service,'STATE',Path(self.tmp.name));self.ref.start();self.addCleanup(self.ref.stop)

    def acquire(self, data=None, observed='2026-11-10T11:20:00+08:00', received=None, period='day'):
        data=copy.deepcopy(data if data is not None else response())
        raw=json.dumps({'code':0,'data':data}).encode()
        receipt={'requested_at':observed,'received_at':received or observed,
                 'response_sha256':hashlib.sha256(raw).hexdigest()}
        with patch.object(live,'TdxClient') as client:
            client.return_value.request.return_value=(data,receipt,raw)
            result=live.acquire('bj920010',period)
            client.return_value.request.assert_called_once_with('/api/kline-all/tdx',{'code':'bj920010','type':live.PERIODS[period]})
            client.return_value.quotes.assert_not_called()
        return result

    def bundle(self, acquired):
        series,receipt=acquired
        return live.build_live({'symbol':'bj920010','code':'920010','name':'测试','exchange':'bj'},
                               {'day':series},{'day':receipt})

    def test_november_10_forming_candle_is_visible_with_actual_ohlcv(self):
        bundle=self.bundle(self.acquire()); panel=bundle['chart']['panels'][0]
        self.assertEqual(bundle['summary']['last_date'],'2026-11-10')
        self.assertEqual([panel['bars'][-1][k] for k in ['open','high','low','close','volume']], [11,11.2,10.9,11.1,90000])
        self.assertFalse(panel['bars'][-1]['is_closed'])
        self.assertTrue(bundle['summary']['live']['forming'])
        self.assertEqual(panel['confirmed_through'],'2026-11-09T15:00:00+08:00')
        self.assertEqual(live.audit_live_panel(panel)['compared_rows'],81)

    def test_intrabar_price_change_cannot_change_confirmed_engine_or_cache_identity(self):
        before=self.bundle(self.acquire())
        data=response();data['list'][-1].update(High=1000000,Close=900000)
        after=self.bundle(self.acquire(data,observed='2026-11-10T11:25:00+08:00'))
        a=before['evidence']['reference_reports']['day'];b=after['evidence']['reference_reports']['day']
        self.assertEqual(a['input_sha256'],b['input_sha256'])
        self.assertEqual(a['base'],b['base']);self.assertEqual(a['signals'],b['signals'])
        self.assertNotEqual(before['summary']['price'],after['summary']['price'])
        self.assertEqual(a['input_audit']['bars_used'],80)
        self.assertTrue(all(s['available_at']<80 for s in b['signals']))

    def test_closed_bar_enters_engine_only_after_a_post_close_request(self):
        lag=self.bundle(self.acquire(observed='2026-11-10T14:59:59+08:00',received='2026-11-10T15:00:02+08:00'))
        self.assertTrue(lag['summary']['live']['forming'])
        closed=self.bundle(self.acquire(observed='2026-11-10T15:00:02+08:00'))
        self.assertFalse(closed['summary']['live']['forming'])
        self.assertEqual(closed['evidence']['reference_reports']['day']['input_audit']['bars_used'],81)

    def test_missing_today_or_weekend_never_creates_a_synthetic_bar(self):
        for observed in ['2026-11-10T11:20:00+08:00','2026-11-14T10:00:00+08:00','2026-11-10T00:01:00+08:00']:
            bundle=self.bundle(self.acquire(response(False),observed))
            self.assertEqual(bundle['summary']['last_date'],'2026-11-09')
            self.assertFalse(bundle['summary']['live']['latest_session_is_today'])
            self.assertEqual(len(bundle['chart']['panels'][0]['bars']),80)

    def test_future_session_is_rejected(self):
        data=response();data['list'][-1]['Time']='2026-11-11T15:00:00+08:00'
        with self.assertRaisesRegex(ValueError,'future session'):self.acquire(data)

    def test_forming_minute_bar_preserves_ohlc_but_future_interval_is_rejected(self):
        data=response(False);data['meta']['type']='minute30'
        data['list']=[dict(data['list'][-1],Time='2026-11-10T10:00:00+08:00'),
                      dict(data['list'][-1],Time='2026-11-10T10:30:00+08:00')];data['count']=2
        series,_=self.acquire(data,observed='2026-11-10T10:21:00+08:00',period='30m')
        self.assertEqual([b.closed for b in series.bars],[True,False])
        data['list'][-1]['Time']='2026-11-10T11:00:00+08:00'
        with self.assertRaisesRegex(ValueError,'尚未开始'):self.acquire(data,observed='2026-11-10T10:21:00+08:00',period='30m')

    def test_raw_receipt_corruption_fails_audit(self):
        bundle=self.bundle(self.acquire());panel=bundle['chart']['panels'][0]
        panel['bars'][-1]['close']=11.15
        with self.assertRaisesRegex(ValueError,'不一致'):live.audit_live_panel(panel)


if __name__=='__main__':unittest.main()
