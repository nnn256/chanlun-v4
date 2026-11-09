import unittest
from datetime import datetime

from chanmodel.tdx_data import CN
from chanmodel.tdx_intraday import normalize_intraday
from chanmodel.tdx_quality import prepare_intraday_response


def row(timestamp, **kwargs):
    return {'Time': timestamp, 'Open':10000, 'High':10000, 'Low':10000,
            'Close':10000, 'Volume':0, 'Amount':0, **kwargs}


def payload(rows):
    return {'count':len(rows),'list':rows,'meta':{'source':'tdx','type':'minute30'}}


class ExplicitQualityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.cutoff=datetime(2026,11,6,2,tzinfo=CN)

    def test_only_flat_zero_activity_offgrid_row_is_quarantined(self):
        original=payload([row('2026-11-05T11:00:00+08:00'),row('2026-11-05T13:00:00+08:00'),row('2026-11-05T13:30:00+08:00')])
        prepared,audit=prepare_intraday_response(original,self.cutoff,'minute30',quarantine_zero_activity=True)
        bars,_,_=normalize_intraday(prepared,self.cutoff,'minute30')
        self.assertEqual([b.closed_at[11:16] for b in bars],['11:00','13:30'])
        self.assertEqual(audit['quarantined_rows'],1)
        self.assertEqual(audit['excluded_rows'][0]['original'],original['list'][1])
        self.assertEqual(original['count'],3)
        self.assertFalse(audit['timestamps_changed'])

    def test_nonzero_nonflat_and_unknown_offgrid_are_not_hidden(self):
        cases=[row('2026-11-05T13:00:00+08:00',Volume=1),row('2026-11-05T13:00:00+08:00',Amount=1),
               row('2026-11-05T13:00:00+08:00',High=11000),row('2026-11-05T12:00:00+08:00')]
        for bad in cases:
            with self.subTest(bad=bad):
                prepared,audit=prepare_intraday_response(payload([bad]),self.cutoff,'minute30',quarantine_zero_activity=True)
                self.assertEqual(audit['quarantined_rows'],0)
                with self.assertRaises(ValueError):normalize_intraday(prepared,self.cutoff,'minute30')

    def test_later_acquisition_does_not_expose_post_cutoff_prices(self):
        data=payload([row('2026-11-05T15:00:00+08:00'),row('2026-11-06T10:00:00+08:00',High=99000)])
        prepared,audit=prepare_intraday_response(data,self.cutoff,'minute30',exclude_after_cutoff=True)
        bars,_,_=normalize_intraday(prepared,self.cutoff,'minute30')
        self.assertEqual(len(bars),1)
        self.assertEqual(audit['after_cutoff_rows'],1)
        self.assertEqual(audit['excluded_rows'][0]['original']['High'],99000)

    def test_default_policy_keeps_strict_error(self):
        prepared,audit=prepare_intraday_response(payload([row('2026-11-05T13:00:00+08:00')]),self.cutoff,'minute30')
        self.assertEqual(audit['quarantined_rows'],0)
        with self.assertRaises(ValueError):normalize_intraday(prepared,self.cutoff,'minute30')

    def test_empty_is_not_changed_into_a_bar(self):
        prepared,audit=prepare_intraday_response({'count':0,'list':None},self.cutoff,'minute30')
        self.assertEqual(prepared['list'],[])
        self.assertEqual(audit['retained_rows'],0)


if __name__=='__main__':unittest.main()
