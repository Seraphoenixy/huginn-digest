"""密码哈希与签名会话；无明文缺省密码。"""
import base64
import hashlib
import hmac
import secrets
from itsdangerous import URLSafeTimedSerializer, BadSignature

def hash_password(password):
    if len(password)<10:raise ValueError('管理密码至少10位')
    salt=secrets.token_bytes(16)
    digest=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1)
    return 'scrypt$'+base64.b64encode(salt).decode()+'$'+base64.b64encode(digest).decode()

def verify_password(password,stored):
    try:
        prefix,salt,expected=stored.split('$')
        if prefix!='scrypt':return False
        digest=hashlib.scrypt(password.encode(),salt=base64.b64decode(salt),n=16384,r=8,p=1)
        return hmac.compare_digest(digest,base64.b64decode(expected))
    except (ValueError,TypeError):return False

class Sessions:
    def __init__(self,secret):
        self.ready=len(secret)>=32
        # 保留历史签名命名空间，项目更名不使已签发的会话失效。
        self.signer=URLSafeTimedSerializer(secret or secrets.token_urlsafe(32),salt='auto-daily-session')
    def issue(self,admin=False):
        return self.signer.dumps({'admin':admin,'csrf':secrets.token_urlsafe(32)})
    def read(self,value,max_age=43200):
        if not self.ready:return {}
        try:return self.signer.loads(value or '',max_age=max_age)
        except BadSignature:return {}
