"""Hindsight outcome labels for research events; not an order/fill simulator."""
from __future__ import annotations

import statistics
import math
from .model import Candle


def evaluate_events(bars: list[Candle], events: list[dict], horizons=(5,20,60), round_trip_bps=20.0) -> dict:
    if not horizons or any(not isinstance(h,int) or h<1 for h in horizons):
        raise ValueError("horizons must be positive integer bar counts")
    if not math.isfinite(round_trip_bps) or round_trip_bps < 0:
        raise ValueError("round_trip_bps must be nonnegative")
    seen, labels = set(), []
    for event in events:
        point = event.get('value',{})
        if event.get('kind')!='signal' or point.get('status')!='candidate' or event['id'] in seen:
            continue
        seen.add(event['id'])
        entry = event['observed_at']+1
        for horizon in horizons:
            exit_index = entry+horizon-1
            row = {'signal_id':event['id'],'signal_class':point['signal_class'],'side':point['side'],
                   'observed_at':event['observed_at'],'entry_index':entry,'exit_index':exit_index,
                   'horizon_bars':horizon,'definition_mode':point['definition_mode']}
            if exit_index>=len(bars) or not bars[exit_index].closed:
                row['status']='insufficient_future_for_label'
            elif bars[entry].open<=0:
                row['status']='nonpositive_entry_percentage_undefined'
            elif bars[entry].volume==0:
                row['status']='zero_volume_entry'
            else:
                sign=1 if point['side']=='buy' else -1
                gross=sign*(bars[exit_index].close/bars[entry].open-1)
                row.update(status='labelled',entry_price=bars[entry].open,exit_price=bars[exit_index].close,
                           gross_directional_return=gross,hypothetical_return_after_constant_cost=gross-round_trip_bps/10000)
            labels.append(row)
    summary=[]
    for horizon in horizons:
        for side in ['buy','sell']:
            values=[r['gross_directional_return'] for r in labels if r['status']=='labelled' and r['horizon_bars']==horizon and r['side']==side]
            summary.append({'horizon_bars':horizon,'side':side,'n':len(values),
                            'mean_directional_return':statistics.fmean(values) if values else None,
                            'positive_fraction':sum(v>0 for v in values)/len(values) if values else None})
    return {'evaluation_mode':'ex_post_fixed_horizon_event_labels','horizon_unit':'bars','round_trip_bps':round_trip_bps,
            'labels':labels,'summary':summary,'execution_allowed':False,
            'limitations':['Next-bar-open fill is hypothetical; price limits, suspensions, T+1, lending and slippage are not simulated.',
                           'Signals can overlap; these are event labels, not portfolio returns or evidence of profitable performance.',
                           'Future bars are used only here to label outcomes; signal generation uses prefix inputs.']}
