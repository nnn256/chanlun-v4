"""A-share session contracts with identity, cadence and OHLCV reconciliation."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import math
import re

from ..model import Candle, time_value

CN = timezone(timedelta(hours=8))
PERIODS = {'1m':1, '5m':5, '15m':15, '30m':30, '60m':60, 'day':240}


@dataclass(frozen=True)
class Series:
    symbol: str
    period: str
    adjustment: str
    bars: tuple[Candle, ...]
    source: str
    input_sha256: str = ''
    calendar: str = 'CN_A'

    def __post_init__(self):
        if not re.fullmatch(r'(sh|sz|bj)\d{6}', self.symbol):
            raise ValueError('symbol requires an explicit exchange prefix')
        if self.period not in PERIODS or self.calendar != 'CN_A':
            raise ValueError('v2 currently supports explicit CN A-share session periods only')
        if self.adjustment not in {'unadjusted','qfq','hfq'} or not self.source:
            raise ValueError('explicit adjustment and source required')

    def digest(self):
        payload=[[b.pos,b.closed_at,b.open,b.high,b.low,b.close,b.volume,b.closed] for b in self.bars]
        return hashlib.sha256(json.dumps(payload,allow_nan=False,separators=(',',':')).encode()).hexdigest()

    def prefix(self, cutoff):
        cut=time_value(cutoff)
        return Series(self.symbol,self.period,self.adjustment,
                      tuple(b for b in self.bars if b.closed and time_value(b.closed_at)<=cut),
                      self.source,self.input_sha256,self.calendar)


def session_closes(date, period):
    if period=='day':
        return [datetime.combine(date,time(15),CN)]
    minutes=PERIODS[period];result=[]
    for start,end in [(time(9,30),time(11,30)),(time(13),time(15))]:
        cursor=datetime.combine(date,start,CN)+timedelta(minutes=minutes)
        last=datetime.combine(date,end,CN)
        while cursor<=last:
            result.append(cursor);cursor+=timedelta(minutes=minutes)
    return result


def validate_series(series: Series, cutoff: str):
    cut=time_value(cutoff);last=None;closed=[]
    for index,bar in enumerate(series.bars):
        if bar.pos!=index or bar.closed_at is None:
            raise ValueError('contiguous indices and explicit close timestamps required')
        instant=time_value(bar.closed_at)
        if last is not None and instant<=last:raise ValueError('timestamps must strictly increase')
        last=instant
        if not bar.closed and index!=len(series.bars)-1:
            raise ValueError('only the current last candle may be open')
        if not bar.closed or instant>cut:continue
        if min(bar.open,bar.high,bar.low,bar.close)<=0:
            raise ValueError('CN A-share OHLC prices must be positive')
        local=instant.astimezone(CN)
        if local not in session_closes(local.date(),series.period):
            raise ValueError(f'{series.period}: off-grid close {bar.closed_at}')
        if series.period!='day' and local.weekday()>=5:
            raise ValueError('modern minute source contains weekend bars')
        closed.append(bar)
    if not closed:raise ValueError('no completed bars at cutoff')
    # Grid divisibility alone is not enough: daily 15:00 values also sit on a
    # minute grid. Require evidence of the advertised native minute cadence.
    native_pairs=0
    if series.period!='day':
        delta=timedelta(minutes=PERIODS[series.period])
        native_pairs=sum(time_value(b.closed_at)-time_value(a.closed_at)==delta for a,b in zip(closed,closed[1:]))
        if len(closed)>=2 and native_pairs==0:
            raise ValueError(f'{series.period}: no observations at the declared native cadence')
    return {'symbol':series.symbol,'period':series.period,'adjustment':series.adjustment,'source':series.source,
            'calendar':series.calendar,'bars_used':len(closed),'native_interval_pairs':native_pairs,
            'first_close':closed[0].closed_at,'last_close':closed[-1].closed_at,
            'input_sha256':series.input_sha256,'effective_data_sha256':series.prefix(cutoff).digest(),
            'state':'verified_grid' if series.period=='day' or native_pairs else 'insufficient_cadence_history'}


def child_closes(parent_close, parent_period, child_period):
    date=parent_close.astimezone(CN).date()
    if parent_period=='day':return session_closes(date,child_period)
    lower=parent_close-timedelta(minutes=PERIODS[parent_period])
    return [point for point in session_closes(date,child_period) if lower<point<=parent_close]


def reconcile(parent: Series, child: Series, cutoff: str, *, price_tolerance=0.005, volume_tolerance=1.0):
    if any(not math.isfinite(x) or x<0 for x in [price_tolerance,volume_tolerance]):
        raise ValueError('nonnegative finite reconciliation tolerances required')
    if parent.symbol!=child.symbol or parent.adjustment!=child.adjustment or parent.calendar!=child.calendar:
        raise ValueError('parent/child symbol, adjustment and calendar must agree')
    if PERIODS[parent.period]<=PERIODS[child.period] or PERIODS[parent.period]%PERIODS[child.period]:
        raise ValueError('child period must strictly divide parent period')
    pp,cc=parent.prefix(cutoff),child.prefix(cutoff)
    by_time={time_value(b.closed_at):b for b in cc.bars}
    if not by_time:raise ValueError('child history empty')
    lower=min(by_time);upper=max(by_time);comparisons=[];gaps=[];mismatches=[]
    parent_times={time_value(b.closed_at) for b in pp.bars}
    for date in sorted({point.astimezone(CN).date() for point in by_time}):
        for expected_parent in session_closes(date,parent.period):
            expected_child=child_closes(expected_parent,parent.period,child.period)
            if expected_parent not in parent_times and expected_child and all(point in by_time for point in expected_child):
                gaps.append({'parent_close':expected_parent.isoformat(),'missing_child_closes':[],
                             'reason':'complete_child_bucket_has_no_parent_observation'})
    for bar in pp.bars:
        end=time_value(bar.closed_at);expected=child_closes(end,parent.period,child.period)
        if not expected or expected[-1]<lower or expected[0]>upper:continue
        missing=[p.isoformat() for p in expected if p not in by_time]
        if missing:
            gaps.append({'parent_close':bar.closed_at,'missing_child_closes':missing});continue
        parts=[by_time[p] for p in expected]
        values=[parts[0].open,max(b.high for b in parts),min(b.low for b in parts),parts[-1].close]
        price_differences={key:abs(value-getattr(bar,key)) for key,value in zip(['open','high','low','close'],values)}
        volume_available=bar.volume is not None and all(b.volume is not None for b in parts)
        volume_difference=abs(math.fsum(b.volume for b in parts)-bar.volume) if volume_available else None
        good=all(v<=price_tolerance for v in price_differences.values()) and volume_available and volume_difference<=volume_tolerance
        item={'parent_close':bar.closed_at,'child_start':parts[0].closed_at,'child_end':parts[-1].closed_at,
              'child_count':len(parts),'price_differences':price_differences,'volume_difference':volume_difference,
              'volume_checked':volume_available,'state':'matched' if good else 'missing_volume' if not volume_available else 'mismatch'}
        comparisons.append(item)
        if not good:mismatches.append(item)
    matched=[x for x in comparisons if x['state']=='matched']
    return {'parent_period':parent.period,'child_period':child.period,'symbol':parent.symbol,
            'state':'mismatch' if mismatches else 'insufficient_overlap' if not matched else 'verified_with_gaps' if gaps else 'verified',
            'price_tolerance':price_tolerance,'volume_tolerance':volume_tolerance,
            'matched_parent_bars':len(matched),'mismatched_parent_bars':len(mismatches),
            'matched_start':matched[0]['parent_close'] if matched else None,
            'matched_end':matched[-1]['parent_close'] if matched else None,
            'incomplete_parent_bars':len(gaps),'comparisons':comparisons,'gaps':gaps,'mismatches':mismatches,
            'outside_overlap_not_proven':True}


def validate_multiframe(series: dict[str,Series], roles: tuple[str,str,str], cutoff: str, **tolerances):
    if len(set(roles))!=3 or any(role not in series for role in roles):raise ValueError('three distinct supplied roles required')
    selected=[series[role] for role in roles]
    if len({s.symbol for s in selected})!=1 or len({s.adjustment for s in selected})!=1:
        raise ValueError('multi-timeframe security and adjustment mismatch')
    if not PERIODS[selected[0].period]>PERIODS[selected[1].period]>PERIODS[selected[2].period]:
        raise ValueError('trade > confirm > trigger periods required')
    audits={role:validate_series(series[role],cutoff) for role in roles}
    stamps=[tuple(b.closed_at for b in s.prefix(cutoff).bars) for s in selected]
    if len(set(stamps))!=len(stamps):raise ValueError('identical observation times cannot provide independent timeframe evidence')
    pairs=[reconcile(selected[i],selected[i+1],cutoff,**tolerances) for i in range(2)]
    verified=all(a['state']=='verified_grid' for a in audits.values()) and all(p['state'] in {'verified','verified_with_gaps'} for p in pairs)
    return {'state':'verified' if verified else 'blocked','series':audits,'pairs':pairs,
            'as_of':cutoff,'roles':list(roles),'identity_verified':True,
            'hierarchy_is_timeframe_not_theory_level':True}


def derive_series(child:Series, parent_period:str, cutoff:str):
    if parent_period not in PERIODS or PERIODS[parent_period]<=PERIODS[child.period] or PERIODS[parent_period]%PERIODS[child.period]:
        raise ValueError('unsupported aggregation ratio')
    validate_series(child,cutoff);usable=child.prefix(cutoff)
    lookup={time_value(b.closed_at):b for b in usable.bars};result=[]
    for date in sorted({p.astimezone(CN).date() for p in lookup}):
        for end in session_closes(date,parent_period):
            expected=child_closes(end,parent_period,child.period)
            if not expected or not all(point in lookup for point in expected):continue
            parts=[lookup[point] for point in expected]
            volume=math.fsum(p.volume for p in parts) if all(p.volume is not None for p in parts) else None
            result.append(Candle(len(result),parts[0].open,max(p.high for p in parts),min(p.low for p in parts),parts[-1].close,end.isoformat(),volume,True))
    return Series(child.symbol,parent_period,child.adjustment,tuple(result),f'aggregated_from_{child.period}:{child.source}',child.digest(),child.calendar)
