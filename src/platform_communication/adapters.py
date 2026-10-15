"""Fixed provider endpoints, no redirects, bounded HTTP, payload-free errors."""
import html
import os
import re
from time import monotonic
import httpx
class DeliveryError(Exception):
    def __init__(self,outcome):
        self.outcome=outcome if outcome in ['retry','dead','uncertain'] else 'uncertain'
        super().__init__('Delivery unavailable')
def render_template(template,body):
    if template=='plain':return body
    if template=='html':return '<p>'+html.escape(body).replace('\n','<br>')+'</p>'
    raise ValueError('Unknown template')
class HTTPAdapter:
    def __init__(self,*,transport=None):self.transport=transport
    def request(self,url,**kwargs):
        if not self.configured:raise DeliveryError('dead')
        deadline=monotonic()+5.0
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0),follow_redirects=False,trust_env=False,transport=self.transport,limits=httpx.Limits(max_connections=2,max_keepalive_connections=0)) as client:
                with client.stream('POST',url,**kwargs) as response:
                    # A 429 explicitly rejects admission; 5xx/timeouts can follow an effect.
                    if response.status_code==429:raise DeliveryError('retry')
                    if response.status_code>=500 or 300<=response.status_code<400:raise DeliveryError('uncertain')
                    if response.status_code>=400:raise DeliveryError('dead')
                    data=bytearray()
                    for chunk in response.iter_bytes():
                        if monotonic()>deadline:raise DeliveryError('uncertain')
                        data.extend(chunk)
                        if len(data)>65536:raise DeliveryError('uncertain')
                    import json
                    return json.loads(data)
        except DeliveryError:raise
        except Exception:raise DeliveryError('uncertain') from None
class ResendAdapter(HTTPAdapter):
    def __init__(self,api_key=None,sender=None,**kwargs):
        super().__init__(**kwargs);self.key=api_key;self.sender=sender
        self.configured=bool(api_key and sender and len(sender)<=254 and '\n' not in sender and '\r' not in sender)
    def send(self,m):
        if not self.configured:raise DeliveryError('dead')
        field='html' if m['template']=='html' else 'text'
        data=self.request('https://api.resend.com/emails',headers={'Authorization':'Bearer '+self.key,'Idempotency-Key':'message/'+str(m['id'])},json={'from':self.sender,'to':[m['recipient']],'subject':'Notification',field:render_template(m['template'],m['body'])})
        if not isinstance(data.get('id'),str):raise DeliveryError('uncertain')
        return data['id']
class TwilioAdapter(HTTPAdapter):
    def __init__(self,account_sid=None,auth_token=None,sender=None,**kwargs):
        super().__init__(**kwargs);self.sid=account_sid;self.token=auth_token;self.sender=sender
        self.configured=bool(account_sid and re.fullmatch(r'AC[0-9a-fA-F]{32}',account_sid) and auth_token and sender and re.fullmatch(r'\+[1-9][0-9]{7,14}',sender))
    def send(self,m):
        if not self.configured:raise DeliveryError('dead')
        data=self.request(f'https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json',auth=(self.sid,self.token),data={'From':self.sender,'To':m['recipient'],'Body':m['body']})
        if not isinstance(data.get('sid'),str):raise DeliveryError('uncertain')
        return data['sid']
class FCMAdapter(HTTPAdapter):
    def __init__(self,project_id=None,access_token=None,**kwargs):
        super().__init__(**kwargs);self.project=project_id;self.token=access_token
        self.configured=bool(project_id and re.fullmatch(r'[a-z][a-z0-9-]{4,62}',project_id) and access_token)
    def send(self,m):
        if not self.configured:raise DeliveryError('dead')
        data=self.request(f'https://fcm.googleapis.com/v1/projects/{self.project}/messages:send',headers={'Authorization':'Bearer '+self.token},json={'message':{'token':m['recipient'],'notification':{'body':m['body']}}})
        if not isinstance(data.get('name'),str):raise DeliveryError('uncertain')
        return data['name']
class TelegramAdapter(HTTPAdapter):
    def __init__(self,bot_token=None,**kwargs):
        super().__init__(**kwargs);self.token=bot_token
        self.configured=bool(bot_token and re.fullmatch(r'[0-9]{5,20}:[A-Za-z0-9_-]{20,100}',bot_token))
    def send(self,m):
        if not self.configured:raise DeliveryError('dead')
        data=self.request(f'https://api.telegram.org/bot{self.token}/sendMessage',json={'chat_id':m['recipient'],'text':m['body']})
        if data.get('ok') is not True or not isinstance(data.get('result'),dict) or type(data['result'].get('message_id')) is not int:raise DeliveryError('uncertain')
        return str(data['result']['message_id'])
def configured_adapters():
    return {'email':ResendAdapter(os.environ.get('RESEND_API_KEY'),os.environ.get('RESEND_FROM')),'sms':TwilioAdapter(os.environ.get('TWILIO_ACCOUNT_SID'),os.environ.get('TWILIO_AUTH_TOKEN'),os.environ.get('TWILIO_FROM')),'push':FCMAdapter(os.environ.get('FCM_PROJECT_ID'),os.environ.get('FCM_ACCESS_TOKEN')),'telegram':TelegramAdapter(os.environ.get('TELEGRAM_BOT_TOKEN'))}
