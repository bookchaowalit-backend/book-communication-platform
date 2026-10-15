from uuid import UUID
from fastapi import Depends,Header,HTTPException,Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel,ConfigDict,Field
from platform_backend_core import create_app as base_app,require_scope
from .store import OutboxStore,Conflict,QueueFull
from .adapters import configured_adapters
class Message(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    channel:str=Field(max_length=16)
    recipient:str=Field(min_length=1,max_length=512)
    body:str=Field(min_length=1,max_length=4096)
    template:str=Field(default='plain',max_length=16)
    max_attempts:int=Field(default=5,ge=1,le=10)
def create_app(database,verifier,*,adapters=None):
    app=base_app('communication',database,verifier,max_body_bytes=32768);store=OutboxStore(database)
    adapters=configured_adapters() if adapters is None else dict(adapters)
    app.state.adapters=adapters
    app.state.readiness_checks['outbox_schema']=store.ready
    app.state.readiness_checks['delivery_adapters']=lambda: any(x.configured for x in adapters.values())
    @app.exception_handler(QueueFull)
    async def full(request,exc):return JSONResponse({'detail':'Outbox capacity reached'},429)
    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse({'detail':'Invalid message'},422)
    @app.exception_handler(Conflict)
    async def conflict(request,exc):return JSONResponse({'detail':'Idempotency conflict'},409)
    @app.post('/v1/messages',status_code=202)
    def create(body:Message,key:str=Header(alias='Idempotency-Key',max_length=128),p=Depends(require_scope('messages:write'))):
        adapter=adapters.get(body.channel)
        if adapter is None or not adapter.configured:raise HTTPException(503,'Delivery unavailable')
        return store.create(p.workspace,key,body.channel,body.recipient,body.body,body.template,body.max_attempts)
    @app.get('/v1/messages')
    def listing(limit:int=Query(100,ge=1,le=100),after:UUID|None=None,p=Depends(require_scope('messages:read'))):return store.list(p.workspace,limit,after)
    @app.get('/v1/messages/{id}')
    def get(id:UUID,p=Depends(require_scope('messages:read'))):
        row=store.get(p.workspace,id)
        if row is None:raise HTTPException(404,'Not found')
        return row
    return app
