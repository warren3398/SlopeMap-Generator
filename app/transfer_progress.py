"""Track server-sent bytes without buffering native GIS downloads."""
import re,threading,time
LOCK=threading.Lock()
TRANSFERS={}

def valid(token):return isinstance(token,str) and re.fullmatch(r'[a-f0-9]{32}',token) is not None

def start(token,total):
    if not valid(token):return
    now=time.monotonic()
    with LOCK:
        for key in list(TRANSFERS):
            if now-TRANSFERS[key]['updated']>3600:del TRANSFERS[key]
        TRANSFERS[token]={'status':'sending','bytes_sent':0,'total_bytes':int(total) if total else None,'updated':now}

def advance(token,count):
    with LOCK:
        if token in TRANSFERS:TRANSFERS[token]['bytes_sent']+=count;TRANSFERS[token]['updated']=time.monotonic()

def finish(token,failed=False):
    with LOCK:
        if token in TRANSFERS:
            item=TRANSFERS[token]
            incomplete=bool(item['total_bytes'] and item['bytes_sent']!=item['total_bytes'])
            item.update(status='failed' if failed or incomplete else 'complete',updated=time.monotonic())

def state(token):
    with LOCK:r=TRANSFERS.get(token,{}).copy()
    if not r:return {'status':'waiting','bytes_sent':0,'total_bytes':None,'percent':None}
    r.pop('updated',None)
    r['percent']=min(100,int(100*r['bytes_sent']/r['total_bytes'])) if r['total_bytes'] else None
    return r
