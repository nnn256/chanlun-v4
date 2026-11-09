"""Complete chart histories with explicit provider coverage and calendar closure."""
from collections import OrderedDict, Counter
from dataclasses import replace
from datetime import datetime, timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path

from chanmodel.model import Candle, Config
from chanmodel.tdx_data import CN, yuan
from chanmodel.tdx_intraday import valid_session_close
from chanmodel.v2.series import Series
from chanmodel.v2.engine import analyze
from chanmodel.structure import merge_bars, fractals_at, strokes_at
from chanmodel.segments import build_segments
from chanmodel.signals import macd, attach_ranges
from chanmodel.v2.recursive import base_units, build_hierarchy, audit_hierarchy
from chanmodel.v2.divergence import compare_divergences, make_signals

NATIVE={'day':'day','1m':'minute1','5m':'minute5','15m':'minute15','30m':'minute30','60m':'hour','week':'week','month':'month','quarter':'quarter','year':'year'}
LABELS={'day':'日K线','5d':'5日K线','1m':'1分钟','5m':'5分钟','15m':'15分钟','30m':'30分钟','60m':'60分钟','week':'周K线','month':'月K线','quarter':'季K线','year':'年K线','timeshare':'历史分时','five_days':'五日分时','auction':'竞价'}
CALENDAR={'week','month','quarter','year','5d'}
SIGNAL_LABELS={'first_buy':'一买候选','second_buy':'二买候选','third_buy':'三买候选','first_sell':'一卖候选','second_sell':'二卖候选','third_sell':'三卖候选'}


def bucket_end(point,period):
    p=point.astimezone(CN)
    if period=='week':return (p+timedelta(days=4-p.weekday())).replace(hour=15,minute=0,second=0,microsecond=0)
    if period=='year':return datetime(p.year+1,1,1,tzinfo=CN)
    month=p.month+1 if period=='month' else (p.month-1)//3*3+4
    return datetime(p.year+(month>12),(month-1)%12+1,1,tzinfo=CN)


def normalize(data,period,cutoff):
    rows=data.get('list',data.get('List',[]));cut=datetime.fromisoformat(cutoff);raw_period=NATIVE[period]
    if data.get('count',data.get('Count',len(rows)))!=len(rows):raise ValueError('provider count mismatch')
    if data.get('meta',{}).get('type',raw_period)!=raw_period:raise ValueError('provider period mismatch')
    if data.get('meta',{}).get('source','tdx')!='tdx':raise ValueError('provider source mismatch')
    unique={}
    for row in rows:
        point=datetime.fromisoformat(row['Time']).astimezone(CN)
        if point>cut:continue
        if period.endswith('m') and period[:-1].isdigit():
            if not valid_session_close(point,int(period[:-1])):raise ValueError('off-grid minute candle')
        elif point.hour!=15 or point.minute!=0:raise ValueError('non-minute candle must have explicit 15:00 label')
        values=[yuan(row[k],k) for k in ('Open','High','Low','Close')]
        vol=float(row['Volume'])*100
        if not math.isfinite(vol) or vol<0 or min(values)<=0:raise ValueError('invalid price/volume')
        closed=point<=cut and (period not in CALENDAR or bucket_end(point,period)<=cut)
        item=Candle(0,*values,point.isoformat(),vol,closed)
        if point in unique and unique[point]!=item:raise ValueError('conflicting duplicate candle')
        unique[point]=item
    return tuple(replace(b,pos=i) for i,(_,b) in enumerate(sorted(unique.items())))


def five_day(bars):
    result=[]
    for start in range(0,len(bars),5):
        part=bars[start:start+5]
        result.append(Candle(len(result),part[0].open,max(b.high for b in part),min(b.low for b in part),part[-1].close,
            part[-1].closed_at,sum(b.volume for b in part),len(part)==5 and all(b.closed for b in part)))
    return tuple(result)


def calendar_analysis(code,period,bars,cutoff,digest):
    from chanmodel.complete_study import analyze_complete
    selected=[b for b in bars if b.closed and datetime.fromisoformat(b.closed_at)<=datetime.fromisoformat(cutoff)]
    if not selected:return None
    return analyze_complete(code,period,bars,cutoff,digest)


def build_panel(period,bars,report,digest,origin='TDX原生周期'):
    points=[{'time':int(datetime.fromisoformat(b.closed_at).timestamp())+8*3600,'source_index':b.pos,
        'date_cn':datetime.fromisoformat(b.closed_at).astimezone(CN).strftime('%Y-%m-%d %H:%M'),
        'open':b.open,'high':b.high,'low':b.low,'close':b.close,'volume':b.volume,'is_closed':b.closed} for b in bars]
    report=report or {'base':{'strokes':[],'segments':[]},'hierarchy':{'levels':[]},'signals':[]}
    centers=report['hierarchy']['levels'][0]['centers'] if report['hierarchy']['levels'] else []
    tail=report['base']['segments'][-1] if report['base']['segments'] else None
    note=('向上' if tail['direction']=='up' else '向下')+('待确认' if tail['state']=='provisional' else '已确认')+f" · {tail['start_price']:.2f} → {tail['end_price']:.2f}" if tail else '结构尚未形成'
    forming=sum(not b.closed for b in bars)
    coverage={'period':period,'label':LABELS[period],'count':len(bars),'closed_count':len(bars)-forming,
        'first':points[0]['date_cn'] if points else None,'last':points[-1]['date_cn'] if points else None,
        'forming_count':forming,'source':origin,'input_sha256':digest,'listing_inception_proven':False}
    return {'period':period,'label':LABELS[period],'visible_start':0,'visible_bars':len(points),'input_bars':len(points),'bars':points,
        'strokes':report['base']['strokes'],'segments':report['base']['segments'],'centers':centers,
        'signals':[{**s,'label':SIGNAL_LABELS.get(s['class'],s['class'])} for s in report['signals'] if s['rank']==1 and s['state']!='invalidated'],
        'note':note,'latest_center':centers[-1] if centers else None,'last_segment':tail,'coverage':coverage,
        'merged':report.get('base',{}).get('merged',[]),'fractals':report.get('base',{}).get('fractals',[]),
        'structure_layers':report['hierarchy']['levels'],'all_signals':report['signals'],
        'complete_evidence':{k:report.get(k,[]) for k in ['feature_sequences','center_evolution','center_monitors','construction_phases','divergence_comparisons']},
        'foot':f"{origin} · 全部{len(points):,}根已载入 · {forming}根形成中（不参与结构） · 历史端点不等于当时已确认"}


def load_capture(folder,code,cutoff,progress=None):
    panels=[];reports={};inputs={};allbars={}
    for period,raw_period in NATIVE.items():
        if progress:progress('analysis',f'正在计算{LABELS[period]}结构',period=period)
        path=folder/'raw'/f'{raw_period}.json.gz'
        if not path.exists():continue
        raw=gzip.decompress(path.read_bytes());digest=hashlib.sha256(raw).hexdigest()
        receipt_path=folder/'receipts'/f'{raw_period}.json'
        if receipt_path.exists():
            receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
            if receipt['response_sha256']!=digest:raise ValueError('capture response hash mismatch')
        bars=normalize(json.loads(raw)['data'],period,cutoff);allbars[period]=bars
        if not bars:continue
        if period in CALENDAR:report=calendar_analysis(code,period,bars,cutoff,digest)
        else:
            series=Series(code,period,'unadjusted',bars,'tdx',digest);inputs[period]=series;report=calendar_analysis(code,period,bars,cutoff,digest)
        reports[period]=report;panels.append(build_panel(period,bars,report,digest))
    if 'day' in allbars:
        bars=five_day(allbars['day']);digest=hashlib.sha256(json.dumps([[b.closed_at,b.open,b.high,b.low,b.close,b.volume] for b in bars]).encode()).hexdigest()
        report=calendar_analysis(code,'5d',bars,cutoff,digest);reports['5d']=report;allbars['5d']=bars
        panels.append(build_panel('5d',bars,report,digest,'日K按起始日每5个交易日聚合（非周线）'))
    return panels,reports,inputs,allbars


def timeshare_panel(data,date):
    rows=data.get('List',[])
    if data.get('date')!=date or data.get('Count')!=len(rows):raise ValueError('time-share date/count mismatch')
    if len(rows) not in (0,240):raise ValueError('time-share session incomplete or unsupported')
    points=[]
    for i,row in enumerate(rows):
        minute=9*60+31+i if i<120 else 13*60+1+i-120
        expected=f'{minute//60:02}:{minute%60:02}'
        if row['Time']!=expected:raise ValueError('time-share timestamp off grid')
        price=yuan(row['Price'],'Price');volume=float(row['Number'])*100
        if price<=0 or not math.isfinite(volume) or volume<0:raise ValueError('time-share invalid value')
        label=f'{date[:4]}-{date[4:6]}-{date[6:]} {expected}'
        at=datetime.fromisoformat(label).replace(tzinfo=CN)
        points.append({'time':int(at.timestamp())+8*3600,'value':price,'volume':volume,'date_cn':label})
    return {'period':'timeshare','label':'历史分时','kind':'line','points':points,'date':date,
            'note':'每分钟价格快照；不含分钟内完整OHLC，不用来补造历史1分钟K线','execution_allowed':False}
