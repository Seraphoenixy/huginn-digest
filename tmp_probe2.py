import sys, re
sys.path.insert(0,'.')
from orchestration.terminology import term_pattern

def split_capped(text,size,protected=(),cap=20):
    """Replica of split_text with iteration cap to detect non-progress."""
    pp=term_pattern(protected) if protected else None
    longest=max(map(len,protected),default=0)
    chunks=[];iters=0
    while len(text)>size:
        iters+=1
        if iters>cap:return 'NON_PROGRESS',chunks
        candidates=[m.end() for m in re.finditer(r'\n|[。！？!?；;]',text[:size]) if m.end()>=size//2]
        end=candidates[-1] if candidates else size
        if pp:
            for match in pp.finditer(text[:size+longest]):
                if match.start()<end<match.end():
                    end=match.start();break
        chunks.append(text[:end]);text=text[end:]
    if text:chunks.append(text)
    return 'ok',chunks

term='A'*12+'。'+'Z'*5
print('term len',len(term))
print(split_capped(term+'B'*100,20,[term]))
