"""仅修改配置中的指定字段；原子保存并验证其余值不变。"""
import json
import re
import tomllib
from pathlib import Path
from integration.files import write_text

def save_security(path,password_hash,session_secret):
    path=Path(path)
    text=path.read_text(encoding='utf-8')
    before=tomllib.loads(text)
    replacements={'password_hash':password_hash,'session_secret':session_secret}
    match=re.search(r'^\[security\][ \t]*\n(?P<body>.*?)(?=^\[|\Z)',text,re.M|re.S)
    body=match['body'] if match else ''
    for key,value in replacements.items():
        line=f'{key} = {json.dumps(value,ensure_ascii=False)}\n'
        pattern=rf'^{key}[ \t]*=.*(?:\n|$)'
        if re.search(pattern,body,re.M):body=re.sub(pattern,lambda _:line,body,count=1,flags=re.M)
        else:body=body.rstrip()+'\n'+line
    if match:text=text[:match.start('body')]+body+'\n'+text[match.end('body'):]
    else:text=text.rstrip()+'\n\n[security]\n'+body
    expected={k:(dict(v) if isinstance(v,dict) else v) for k,v in before.items()}
    expected.setdefault('security',{}).update(replacements)
    if tomllib.loads(text)!=expected:raise ValueError('配置写入校验失败，原文件保留')
    write_text(path,text)
    path.chmod(0o600)
