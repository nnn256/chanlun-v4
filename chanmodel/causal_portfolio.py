"""Long-only, cash-account A-share simulator driven by immutable signal events.

Fills use the next bar open. T+1, board lots, costs, observed volume, limit-open
blocking and share distributions are explicit. No shorting or leverage.
"""
from dataclasses import dataclass,asdict
from decimal import Decimal,ROUND_HALF_UP
from datetime import datetime,timedelta
import math


@dataclass(frozen=True)
class CostModel:
    commission_rate:float=.0003
    commission_min:float=5.
    slippage_bps:float=5.
    initial_cash:float=100000.
    max_holding_sessions:int=20
    price_limit:float=.20
    max_volume_fraction:float=.01


def tick(x):return float(Decimal(str(x)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))


def charges(side,notional,date,cost):
    commission=max(cost.commission_min,notional*cost.commission_rate)
    stamp=notional*(.001 if date<'2023-08-28' else .0005) if side=='sell' else 0.
    transfer=notional*(.00002 if date<'2022-04-29' else .00001)
    return {'commission':commission,'stamp_duty':stamp,'transfer':transfer,'total':commission+stamp+transfer}


def simulate(raw_bars,adjusted_bars,events,daily_closes,actions=(),cost=CostModel(),period='5m',*,symbol=None):
    if len(raw_bars)!=len(adjusted_bars):raise ValueError('Raw/adjusted times must align')
    by_index={}
    for e in events:by_index.setdefault(e['observed_index'],[]).append(e)
    trading_dates=sorted(daily_closes);date_rank={d:i for i,d in enumerate(trading_dates)}
    action_map={a['ex_date']:a for a in actions};cash=cost.initial_cash;shares=0;position=None;pending=None;equity=[];trades=[];orders=[];corporate=[]
    last_date=None
    for i,(bar,adj) in enumerate(zip(raw_bars,adjusted_bars)):
        closed=datetime.fromisoformat(bar.closed_at).astimezone(__import__('datetime').timezone(timedelta(hours=8)))
        date=closed.date().isoformat();factor=adj.close/bar.close
        opened=closed.replace(hour=9,minute=30) if period=='day' else closed-timedelta(minutes=int(period[:-1]))
        fill_time=opened.isoformat()
        if date!=last_date:
            action=action_map.get(date)
            if action and shares:
                before=shares;shares*=action['share_multiplier'];cash+=before*action.get('cash_per_share',0.)
                corporate.append({'date':date,'shares_before':before,'shares_after':shares,'cash_dividend':before*action.get('cash_per_share',0.)})
            last_date=date
        previous_date=trading_dates[date_rank[date]-1] if date_rank[date]>0 else None
        reference=daily_closes.get(previous_date) if previous_date else None
        if date in action_map and reference is not None:
            reference=(reference-action_map[date].get('cash_per_share',0))/action_map[date]['share_multiplier']
        rate=cost.price_limit
        if symbol:
            rate=.30 if symbol.startswith('bj') else .20 if symbol.startswith('sh688') or symbol.startswith('sz30') and date>='2020-08-24' else .10
        upper=tick(reference*(1+rate)) if reference else math.inf
        lower=tick(reference*(1-rate)) if reference else 0
        # Five IPO sessions have no standard 20% cap on this ChiNext listing.
        limit_active=date_rank.get(date,0)>=5
        if pending and pending['not_before_index']<=i:
            side=pending['side'];reason=None
            if not bar.volume or bar.volume<=0:reason='no_observed_volume'
            if side=='buy' and limit_active and bar.open>=upper-.005:reason='open_at_upper_limit'
            if side=='sell' and limit_active and bar.open<=lower+.005:reason='open_at_lower_limit'
            if side=='sell' and position and date<=position['entry_date']:reason='T_plus_1_locked'
            if side=='buy' and shares:reason='already_in_position'
            if side=='sell' and not shares:reason='no_position'
            if side=='buy' and adj.open<=pending['invalidation']:reason='opening_gap_invalidates_buy'
            price=tick(bar.open*(1+cost.slippage_bps/10000 if side=='buy' else 1-cost.slippage_bps/10000))
            if limit_active:price=min(price,upper) if side=='buy' else max(price,lower)
            # Only prior closed-bar volume may constrain the next-open order.
            prior_volume=raw_bars[i-1].volume if i else 0
            max_shares=math.floor((prior_volume or 0)*cost.max_volume_fraction/100)*100
            if side=='buy':
                qty=min(math.floor(cash/(price*(1+cost.commission_rate+.00002))/100)*100,max_shares)
                while qty>0 and qty*price+charges('buy',qty*price,date,cost)['total']>cash:qty-=100
                if qty<100:reason=reason or 'cash_or_prior_volume_below_one_lot'
            else:
                # A sale can include split-created odd lots, but still respects
                # the prior-bar liquidity cap; incomplete exits remain pending.
                qty=min(shares,max_shares)
                if qty<=0:reason=reason or 'prior_volume_below_one_lot'
            if reason:
                orders.append({'time':bar.closed_at,'side':side,'state':'blocked','reason':reason,'signal_id':pending.get('signal_id')})
                if reason in {'already_in_position','no_position','opening_gap_invalidates_buy'}:pending=None
            else:
                notional=qty*price;fee=charges(side,notional,date,cost)
                order={'time':fill_time,'bar_close_time':bar.closed_at,'side':side,'state':'filled','price':price,'shares':qty,'fees':fee,
                       'signal_id':pending.get('signal_id'),'reason':pending['reason'],'signal_time':pending['signal_time'],
                       'bar_index':i,'price_semantics':'next bar opening print plus declared slippage; volume cap uses prior closed bar'}
                orders.append(order)
                if side=='buy':
                    cash-=notional+fee['total'];shares=qty
                    position={'signal_id':pending['signal_id'],'signal_time':pending['signal_time'],'entry_date':date,'entry_time':fill_time,
                              'entry_index':i,'entry_price':price,'entry_notional':notional,'entry_fees':fee['total'],
                              'entry_cost':notional+fee['total'],'initial_shares':qty,'sale_proceeds':0.,'exit_fees':0.,
                              'invalidation_adjusted':pending['invalidation'],'class':pending['class'],'rank':pending['rank']}
                    pending=None
                else:
                    cash+=notional-fee['total'];shares-=qty;position['sale_proceeds']+=notional-fee['total'];position['exit_fees']+=fee['total']
                    if shares<1e-7:
                        shares=0;trade={**position,'exit_time':fill_time,'exit_index':i,'exit_price':price,
                                       'exit_reason':pending['reason'],'net_profit':position['sale_proceeds']-position['entry_cost'],
                                       'net_return':position['sale_proceeds']/position['entry_cost']-1,
                                       'holding_sessions':date_rank[date]-date_rank[position['entry_date']]}
                        trades.append(trade);position=None;pending=None
        current=by_index.get(i,[])
        # End-of-bar events cannot affect this same bar's opening fill.
        if position:
            boundary=position['invalidation_adjusted'];stop=adj.low<=boundary
            revoked=any(e['signal_id']==position['signal_id'] and e['event'] in {'invalidated','withdrawn'} for e in current)
            sell=next((e for e in current if e['event']=='confirmed' and e['side']=='sell'),None)
            timeout=date_rank[date]-date_rank[position['entry_date']]>=cost.max_holding_sessions
            if pending is None and (stop or revoked or sell or timeout):
                pending={'side':'sell','not_before_index':i+1,'signal_id':position['signal_id'],'signal_time':bar.closed_at,
                         'reason':'structural_boundary_invalidated' if stop else 'confirmation_withdrawn' if revoked else 'confirmed_sell' if sell else 'max_holding_sessions'}
        elif pending is None:
            buy=next((e for e in sorted(current,key=lambda e:(-e.get('rank',0),e['signal_id'])) if e['event']=='confirmed' and e['side']=='buy'),None)
            if buy:pending={'side':'buy','not_before_index':i+1,'signal_id':buy['signal_id'],'signal_time':bar.closed_at,'reason':'confirmed_buy',
                             'invalidation':buy['invalidation'],'class':buy['class'],'rank':buy['rank']}
        if pending and pending['side']=='buy' and any(e['signal_id']==pending['signal_id'] and e['event'] in {'invalidated','withdrawn'} for e in current):
            orders.append({'time':bar.closed_at,'side':'buy','state':'cancelled_before_fill','reason':'signal_revoked','signal_id':pending['signal_id']});pending=None
        equity.append({'index':i,'time':bar.closed_at,'equity':cash+shares*bar.close,'cash':cash,'shares':shares,'close':bar.close})
    peak=cost.initial_cash;maxdd=0
    for p in equity:peak=max(peak,p['equity']);p['drawdown']=p['equity']/peak-1;maxdd=min(maxdd,p['drawdown'])
    wins=sum(t['net_profit']>0 for t in trades);losses=-sum(t['net_profit'] for t in trades if t['net_profit']<0);gains=sum(t['net_profit'] for t in trades if t['net_profit']>0)
    return {'profile':'cash_long_only_next_open_T1_corporate_actions_v1','settings':asdict(cost),'orders':orders,'trades':trades,'equity':equity,
            'corporate_action_credits':corporate,'open_position':position,'pending_order':pending,
            'metrics':{'completed_trades':len(trades),'wins':wins,'win_rate':wins/len(trades) if trades else None,
                       'profit_factor':gains/losses if losses else None,'ending_equity':equity[-1]['equity'] if equity else cost.initial_cash,
                       'total_return':equity[-1]['equity']/cost.initial_cash-1 if equity else 0,'max_drawdown':maxdd,
                       'total_fees':sum(x['fees']['total'] for x in orders if x['state']=='filled'),
                       'confirmed_signal_count':sum(e['event']=='confirmed' for e in events)},
            'execution_allowed':False,'limitations':['OHLC backtest is not exchange queue replay; limit-at-open orders are conservatively blocked.',
                'Only observed volume and prior closed-bar volume are available; fills assume the declared slippage.',
                'Current source may contain historical revisions; point-in-time tick archives are unavailable.',
                'One stock and one fixed rule set are not out-of-sample evidence of future returns.']}
