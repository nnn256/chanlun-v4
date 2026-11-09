"""Per-stock work evidence, visible before a whole stock finishes."""
from datetime import datetime
import json
import time

from chanmodel.tdx_data import atomic_json,now_iso
from .store import STATE


def reporter(job_id,symbol,name=''):
    path=STATE/'jobs'/job_id/'progress'/f'{symbol}.json'
    started=time.monotonic()
    def write(stage,message,**fields):
        atomic_json(path,{'job_id':job_id,'symbol':symbol,'name':name,'stage':stage,'message':message,
                          'elapsed_seconds':round(time.monotonic()-started,1),'updated_at':now_iso(),**fields})
    return write


def read_progress(job_id,symbol):
    path=STATE/'jobs'/job_id/'progress'/f'{symbol}.json'
    try:return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError):return None
