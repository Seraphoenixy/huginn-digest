"""仅应用配置中经人工确认、绑定单一视频的术语；不做模糊匹配。"""
import re


def term_pattern(words):
    words=sorted(set(words),key=len,reverse=True)
    # 中文相邻可匹配，英文单词/型号内部不可部分替换。
    return re.compile(r'(?<![A-Za-z0-9_])(?:'+'|'.join(re.escape(word) for word in words)+r')(?![A-Za-z0-9_])',re.I)


def apply_terms(text,terms):
    replacements={word.casefold():term['name'] for term in terms for word in [term['name'],*term['aliases']]}
    if not replacements:return text
    return term_pattern(replacements).sub(lambda match:replacements[match.group().casefold()],text)


def check_terms(original,result,terms):
    for term in terms:
        pattern=term_pattern([term['name']])
        if len(pattern.findall(original))!=len(pattern.findall(result)):
            raise ValueError('纠错结果改变了已确认术语')
