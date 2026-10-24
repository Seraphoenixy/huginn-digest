"""仅修改配置中的指定字段；原子保存并验证其余值不变。"""
import json
import re
import tomllib
from pathlib import Path
from integration.files import write_text

def save_values(path,section,values):
    """把若干键写进指定段，其他段与注释保持原样。

    路径只有一条：读原文、替换该段的行、重新解析比对。任何键解析不出预期值
    就抛错并保留原文件，避免半截写入让服务起不来。
    """
    path=Path(path)
    text=path.read_text(encoding='utf-8')
    before=tomllib.loads(text)
    replacements=dict(values)
    if not replacements:raise ValueError('没有需要保存的配置项')
    match=re.search(rf'^\[{re.escape(section)}\][ \t]*\n(?P<body>.*?)(?=^\[|\Z)',text,re.M|re.S)
    body=match['body'] if match else ''
    for key,value in replacements.items():
        line=f'{key} = {json.dumps(value,ensure_ascii=False)}\n'
        pattern=rf'^{re.escape(key)}[ \t]*=.*(?:\n|$)'
        if re.search(pattern,body,re.M):body=re.sub(pattern,lambda _,line=line:line,body,count=1,flags=re.M)
        else:body=body.rstrip()+'\n'+line
    if match:text=text[:match.start('body')]+body+'\n'+text[match.end('body'):]
    else:text=text.rstrip()+'\n\n['+section+']\n'+body
    expected={k:(dict(v) if isinstance(v,dict) else v) for k,v in before.items()}
    expected.setdefault(section,{}).update(replacements)
    if tomllib.loads(text)!=expected:raise ValueError('配置写入校验失败，原文件保留')
    write_text(path,text)
    path.chmod(0o600)

def save_security(path,password_hash,session_secret):
    save_values(path,'security',{'password_hash':password_hash,'session_secret':session_secret})
