from datetime import datetime, timedelta
import unittest

from chanmodel.model import Candle
from chanmodel.study_profile import analyze_period, filter_signals
from chanmodel.tdx_data import CN


def sample():
    import math
    bars = []
    point = datetime(2026, 1, 5, 15, tzinfo=CN)
    while len(bars) < 160:
        if point.weekday() < 5:
            i = len(bars)
            c = 20 + 3 * math.sin(i / 10) + math.sin(i / 2)
            bars.append(Candle(i, c, c+.2, c-.2, c, point.isoformat(), 100))
        point += timedelta(days=1)
    return bars


class StudyProfileTests(unittest.TestCase):
    def test_future_data_does_not_change_earlier_cutoff(self):
        bars = sample(); cutoff = bars[99].closed_at
        a = analyze_period('sz300949', 'day', bars, cutoff, 'fixture')
        b = analyze_period('sz300949', 'day', bars[:100], cutoff, 'fixture')
        self.assertEqual(a['base'], b['base'])
        self.assertEqual(a['hierarchy'], b['hierarchy'])
        self.assertEqual(a['signals'], b['signals'])

    def test_forming_last_bar_excluded_from_calendar_structure(self):
        bars = [Candle(0, 10, 11, 9, 10, '2025-12-31T15:00:00+08:00', 100),
                Candle(1, 10, 100, 1, 90, '2026-11-06T15:00:00+08:00', 100, False)]
        result = analyze_period('sz300949', 'year', bars, '2026-11-08T00:00:00+08:00', 'fixture')
        self.assertEqual(result['input_audit']['closed_count'], 1)
        self.assertEqual(result['base']['merged'][0]['high'], 11)

    def test_unfinished_retest_does_not_become_conservative_second(self):
        points = [{'class':'second_buy','retest_mode':'weak_retest_101'},
                  {'class':'second_sell','retest_mode':'held_first_extreme'}, {'class':'third_buy'}]
        self.assertEqual([p['class'] for p in filter_signals(points)], ['second_sell','third_buy'])

    def test_reject_duplicate_time(self):
        b = sample()[0]
        with self.assertRaises(ValueError):
            analyze_period('sz300949','day',[b,Candle(1,b.open,b.high,b.low,b.close,b.closed_at,100)],b.closed_at,'fixture')

    def test_signal_availability_and_no_execution(self):
        bars = sample()
        result = analyze_period('sz300949','day',bars,bars[-1].closed_at,'fixture')
        self.assertEqual(result['config']['stroke_mode'], 'strict')
        self.assertEqual(result['certificate_audit']['errors'], [])
        self.assertFalse(result['execution_allowed'])
        for point in result['signals']:
            self.assertLessEqual(point['event_time'], point['available_time'])
            self.assertEqual(point['lower_level_trigger'], 'missing')


if __name__ == '__main__':
    unittest.main()
