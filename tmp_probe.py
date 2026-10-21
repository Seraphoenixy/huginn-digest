import sys
sys.path.insert(0,'.')
from orchestration.pipeline import split_text
def progress(text,size,prot=()):
    chunks=split_text(text,size,prot)
    assert ''.join(chunks)==text
    return chunks
print('basic', [len(c) for c in progress('abc。def。ghi。',6)])
name='Example-29B-Intent'
t='甲'*8+name+'乙'*4
cs=progress(t,12,[name]); print('straddle',[len(c) for c in cs], max(map(len,cs)))
t='X'*600+'。'+'B'*500
cs=split_text(t,1000,['X'*600]); print('long-protected ok',[len(c) for c in cs])
t='Foo。Bar baz。tail'
cs=split_text(t,10,['Foo。Bar']); print('punct-term',[len(c) for c in cs],cs, ''.join(cs)==t)
