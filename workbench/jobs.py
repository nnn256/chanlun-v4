"""Durable, bounded parallel task runner; a pause drains current stocks."""
import argparse
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
import json
import os
import traceback

from chanmodel.tdx_data import now_iso
from .compute import process_stock
from .store import connect, decode, publish_result


def update_counts(db, job_id):
    counts = dict(db.execute('SELECT state,COUNT(*) FROM items WHERE job_id=? GROUP BY state',(job_id,)).fetchall())
    success, failed = counts.get('success',0), counts.get('failed',0)
    db.execute('UPDATE jobs SET done=?,success=?,failed=?,updated=? WHERE id=?',
               (success+failed,success,failed,now_iso(),job_id))


def run(job_id):
    with connect() as db:
        job = decode(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
        if not job or job['state']!='starting':
            return
        db.execute("UPDATE items SET state='pending' WHERE job_id=? AND state='running'",(job_id,))
        db.execute("UPDATE jobs SET state='running',pid=?,started=COALESCE(started,?),updated=? WHERE id=?",(os.getpid(),now_iso(),now_iso(),job_id))
    settings = job['settings']; workers = settings['workers']
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {}
        while True:
            with connect() as db:
                current = db.execute('SELECT state FROM jobs WHERE id=?',(job_id,)).fetchone()[0]
                if current=='running':
                    rows = db.execute("SELECT u.* FROM items i JOIN universe u USING(symbol) WHERE i.job_id=? AND i.state='pending' ORDER BY u.symbol LIMIT ?",(job_id,workers-len(futures))).fetchall()
                    for row in rows:
                        record = dict(row)
                        db.execute("UPDATE items SET state='running',updated=? WHERE job_id=? AND symbol=?",(now_iso(),job_id,record['symbol']))
                        futures[executor.submit(process_stock,record,settings,job_id)] = record['symbol']
                if not futures:
                    update_counts(db,job_id)
                    end = 'paused' if current=='pausing' else 'cancelled' if current=='cancelling' else 'completed'
                    db.execute('UPDATE jobs SET state=?,finished=?,updated=?,pid=NULL WHERE id=?',(end,now_iso(),now_iso(),job_id))
                    break
            done, _ = wait(futures,timeout=1,return_when=FIRST_COMPLETED)
            for future in done:
                code = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = {'state':'failed','error':f'Worker error: {type(exc).__name__}: {exc}'}
                with connect() as db:
                    summary = json.dumps(result.get('summary'),ensure_ascii=False) if result.get('summary') else None
                    db.execute('UPDATE items SET state=?,summary=?,error=?,artifact=?,updated=? WHERE job_id=? AND symbol=?',
                        (result['state'],summary,result.get('error'),result.get('artifact'),now_iso(),job_id,code))
                    if result['state']=='success':
                        publish_result(db,code,settings['mode'],job_id,now_iso(),summary,result['artifact'])
                    update_counts(db,job_id)


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--job',required=True)
    args = parser.parse_args()
    try:
        run(args.job)
    except Exception as exc:
        traceback.print_exc()
        with connect() as db:
            db.execute("UPDATE jobs SET state='interrupted',message=?,updated=?,pid=NULL WHERE id=?",(str(exc),now_iso(),args.job))
