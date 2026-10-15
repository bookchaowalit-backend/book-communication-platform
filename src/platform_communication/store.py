import hashlib
import json
import re
from uuid import UUID,uuid4
from psycopg.rows import dict_row
from .migrations import MIGRATIONS
from .adapters import render_template
class Conflict(Exception):pass
class QueueFull(Exception):pass
PUBLIC='id,channel,recipient,body,template,status,attempts,max_attempts,run_at,created_at'
class OutboxStore:
    def __init__(self,database,*,max_pending=10000):
        if type(max_pending) is not int or not 1<=max_pending<=10000:raise ValueError('Invalid capacity')
        self.db=database;self.max_pending=max_pending
    def create(self,workspace,key,channel,recipient,body,template,max_attempts=5):
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}',key):raise ValueError('Invalid key')
        patterns={'email':r'[A-Za-z0-9.!#$%&*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}','sms':r'\+[1-9][0-9]{7,14}','push':r'[A-Za-z0-9_:.-]{10,512}','telegram':r'-?[0-9]{1,20}'}
        if channel not in patterns or not isinstance(recipient,str) or len(recipient)>512 or not re.fullmatch(patterns[channel],recipient):raise ValueError('Invalid recipient')
        if not isinstance(body,str) or not 1<=len(body)<=4096 or any(ord(x)<32 and x not in '\n\t' for x in body):raise ValueError('Invalid body')
        render_template(template,body)
        if template=='html' and channel!='email':raise ValueError('Invalid template')
        if type(max_attempts) is not int or not 1<=max_attempts<=10:raise ValueError('Invalid attempts')
        fingerprint=hashlib.sha256(json.dumps([channel,recipient,body,template,max_attempts],separators=(',',':')).encode()).hexdigest()
        with self.db.transaction(workspace) as c:
            c.row_factory=dict_row
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",('message-capacity:'+str(workspace),))
            c.execute('INSERT INTO message_outbox(workspace_id,id,event_key,fingerprint,channel,recipient,body,template,max_attempts) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,event_key) DO NOTHING',(workspace,uuid4(),key,fingerprint,channel,recipient,body,template,max_attempts))
            row=c.execute(f'SELECT {PUBLIC},fingerprint FROM message_outbox WHERE event_key=%s',(key,)).fetchone()
            if row.pop('fingerprint')!=fingerprint:raise Conflict()
            if c.execute("SELECT count(*) FROM message_outbox WHERE status IN ('queued','leased','sending','uncertain')").fetchone()['count']>self.max_pending:raise QueueFull()
            return row
    def get(self,workspace,id):
        with self.db.transaction(workspace) as c:
            c.row_factory=dict_row
            return c.execute(f'SELECT {PUBLIC} FROM message_outbox WHERE id=%s',(id,)).fetchone()
    def list(self,workspace,limit=100,after=None):
        if not 1<=limit<=100:raise ValueError('Invalid limit')
        with self.db.transaction(workspace) as c:
            c.row_factory=dict_row
            return c.execute(f'SELECT {PUBLIC} FROM message_outbox WHERE (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s',(after,after,limit)).fetchall()
    def claim(self,workspace,lease_seconds=30):
        if type(lease_seconds) is not int or not 10<=lease_seconds<=300:raise ValueError('Invalid lease')
        with self.db.transaction(workspace) as c:
            c.row_factory=dict_row
            c.execute("UPDATE message_outbox SET status='uncertain',lease_token=NULL WHERE status='sending' AND lease_until<=clock_timestamp()")
            c.execute("UPDATE message_outbox SET status='dead',lease_token=NULL WHERE status='leased' AND lease_until<=clock_timestamp() AND attempts>=max_attempts")
            return c.execute("""WITH candidate AS (SELECT id FROM message_outbox WHERE attempts<max_attempts AND ((status='queued' AND run_at<=clock_timestamp()) OR (status='leased' AND lease_until<=clock_timestamp())) ORDER BY run_at,id FOR UPDATE SKIP LOCKED LIMIT 1) UPDATE message_outbox m SET status='leased',attempts=attempts+1,lease_token=%s,lease_until=clock_timestamp()+%s*interval '1 second' FROM candidate WHERE m.id=candidate.id RETURNING m.*""",(uuid4(),lease_seconds)).fetchone()
    def mark_sending(self,workspace,id,token):
        with self.db.transaction(workspace) as c:
            return bool(c.execute("UPDATE message_outbox SET status='sending' WHERE id=%s AND lease_token=%s AND status='leased' AND lease_until>clock_timestamp() RETURNING id",(id,token)).fetchone())
    def finish(self,workspace,id,token,outcome,provider_id=None):
        if outcome not in ['delivered','retry','dead','uncertain']:raise ValueError('Invalid outcome')
        if provider_id is not None and (not isinstance(provider_id,str) or len(provider_id)>256):provider_id=None
        with self.db.transaction(workspace) as c:
            return bool(c.execute("""UPDATE message_outbox SET status=CASE WHEN %s='retry' THEN CASE WHEN attempts>=max_attempts THEN 'dead' ELSE 'queued' END ELSE %s END,run_at=clock_timestamp()+least(3600,power(2,attempts)::int)*interval '1 second',provider_id=%s,lease_token=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s AND status='sending' AND lease_until>clock_timestamp() RETURNING id""",(outcome,outcome,provider_id,id,token)).fetchone())
    def ready(self):
        with self.db.transaction(UUID(int=0)) as c:
            history=c.execute('SELECT version,checksum FROM platform_schema_migrations WHERE namespace=%s ORDER BY version',('communication',)).fetchall()
            if history!=[(m.version,m.checksum) for m in MIGRATIONS]:return False
            row=c.execute("SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE oid=to_regclass('message_outbox')").fetchone()
            c.execute('SELECT status,lease_token,fingerprint FROM message_outbox LIMIT 0')
            return bool(row and row[0])
