"""唯一处理图：获取→纠错→摘要→完成。state只含ID和路径。"""
import json
import hashlib
import shutil
import time
from pathlib import Path
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from config import ROOT
from integration.files import read_text, write_text
from integration.sources.bili import BiliVideoSource, extract_bvid_from_url
from integration.sources.rss import extract
from model.asr import CloudASR
from model.text import TextModel
from integration.runtime_log import log

STAGE_LABELS={'acquire':'获取正文/转写','correct':'校对转写','summarize':'生成摘要','finish':'保存结果'}

def split_text(text,size):
    """优先沿段落/句末分片，保留全部字符；超长单句才按容量截取。"""
    if size<=0:raise ValueError('分片容量必须为正数')
    import re
    chunks=[]
    while len(text)>size:
        candidates=[m.end() for m in re.finditer(r'\n|[。！？!?；;]',text[:size]) if m.end()>=size//2]
        end=candidates[-1] if candidates else size
        chunks.append(text[:end]);text=text[end:]
    if text:chunks.append(text)
    return chunks

class State(TypedDict, total=False):
    task_id: int
    raw_path: str
    corrected_path: str
    summary_path: str

class ContentPipeline:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.asr, self.llm = CloudASR(settings), TextModel(settings)
        graph = StateGraph(State)
        for name in ('acquire','correct','summarize','finish'):
            graph.add_node(name,self.traced(name))
        graph.add_edge(START,'acquire')
        graph.add_edge('acquire','correct')
        graph.add_edge('correct','summarize')
        graph.add_edge('summarize','finish')
        graph.add_edge('finish',END)
        self.graph = graph.compile()

    def traced(self, name):
        def node(state):
            task_id=state['task_id']
            self.store.update(task_id,stage=name)
            self.store.event(task_id,name,'started')
            started=time.perf_counter()
            log(f'任务 {task_id}：开始{STAGE_LABELS[name]}')
            try:
                result=getattr(self,name)(state)
                self.store.event(task_id,name,'ok')
                log(f'任务 {task_id}：{STAGE_LABELS[name]}完成，耗时 {time.perf_counter()-started:.1f} 秒')
                return result
            except Exception as exc:
                self.store.event(task_id,name,type(exc).__name__)
                log(f'任务 {task_id}：{STAGE_LABELS[name]}失败（{type(exc).__name__}），耗时 {time.perf_counter()-started:.1f} 秒')
                raise
        return node

    def folder(self,state):
        path=self.settings.content_dir/str(state['task_id'])
        path.mkdir(exist_ok=True)
        return path

    def prompt(self,name):
        return read_text(ROOT/'knowledge'/'prompts'/f'{name}.md')

    def acquire(self,state):
        task=self.store.task(state['task_id'])
        path=self.folder(state)/'raw.txt'
        if not path.exists():
            if task['kind']=='bili':
                bvid=extract_bvid_from_url(task['url'])
                if not bvid: raise ValueError('无效BV号')
                source=BiliVideoSource(cookie_file=self.settings.cookie_file or None,download_cache_dir=str(self.settings.cache_dir/'downloads'),auto_refresh=self.settings.bili_auto_refresh,refresh_check_seconds=self.settings.bili_refresh_check_seconds,refresh_retry_seconds=self.settings.bili_refresh_retry_seconds)
                try:
                    pages,_=source.get_video_pages(bvid)
                    if not pages: raise ValueError('视频不可访问或没有分P')
                    texts=[]
                    for page in pages:
                        number=page['page']
                        part=self.folder(state)/f'part_{number}.txt'
                        if not part.exists():
                            log(f'任务 {task["id"]}：开始下载分P {number}，共 {len(pages)} 个分P')
                            media=source.download_media(bvid,number)
                            log(f'任务 {task["id"]}：分P {number} 下载完成，开始ASR')
                            write_text(part,self.asr.transcribe(media))
                        else:log(f'任务 {task["id"]}：复用分P {number} 已有转写')
                        texts.append(read_text(part))
                    write_text(path,'\n\n'.join(texts))
                finally:
                    shutil.rmtree(self.settings.cache_dir/'downloads'/bvid,ignore_errors=True)
                    source.session.close()
            else:
                seed=self.folder(state)/'feed.txt'
                text,warning=extract(task['url'],read_text(seed) if seed.exists() else '')
                write_text(path,text)
                if warning:self.store.update(task['id'],warning=warning)
                if warning:log(f'任务 {task["id"]}：正文获取降级，使用RSS内容')
        else:log(f'任务 {task["id"]}：复用已有原稿')
        self.store.update(task['id'],raw_path=str(path))
        return {'raw_path':str(path)}

    def correct(self,state):
        task=self.store.task(state['task_id'])
        path=self.folder(state)/'corrected.txt'
        if path.exists():
            log(f'任务 {task["id"]}：复用已有纠错稿')
            return {'corrected_path':str(path)}
        if task['kind']!='bili' or not self.settings.get('pipeline','correction',True):
            log(f'任务 {task["id"]}：无需校对，使用原始正文')
            return {'corrected_path':state['raw_path']}
        try:
            text=read_text(state['raw_path'])
            size=self.settings.get('llm','chunk_chars',12000)
            parts=[]
            chunks=split_text(text,size)
            for index,chunk in enumerate(chunks):
                part=self.folder(state)/f'correction_{index}.txt'
                if not part.exists():
                    log(f'任务 {task["id"]}：请求云端纠错，第 {index+1}/{len(chunks)} 段')
                    result=self.llm.call(self.prompt('correct'),chunk)
                    if not result.strip():raise ValueError('纠错为空')
                    write_text(part,result)
                parts.append(read_text(part))
            write_text(path,'\n'.join(parts))
            self.store.update(task['id'],corrected_path=str(path))
            return {'corrected_path':str(path)}
        except Exception:
            log(f'任务 {task["id"]}：纠错失败，降级使用原始转写继续摘要')
            self.store.update(task['id'],warning='纠错失败，使用原始转写生成摘要')
            return {'corrected_path':state['raw_path']}

    def summarize(self,state):
        path=self.folder(state)/'summary.json'
        manifest=self.folder(state)/'summary_manifest.json'
        fingerprint=self.summary_fingerprint(state['corrected_path'])
        if (path.exists() or any(self.folder(state).glob('summary_part_*.json'))) and not self.summary_current(state['corrected_path'],manifest):
            archive=self.folder(state)/f'previous_summary_{time.time_ns()}'
            archive.mkdir()
            for cached in [path,manifest,*self.folder(state).glob('summary_part_*.json')]:
                if cached.exists():cached.replace(archive/cached.name)
        if not path.exists():
            write_text(manifest,json.dumps({'fingerprint':fingerprint}))
            text=read_text(state['corrected_path'])
            size=self.settings.get('llm','chunk_chars',12000)
            partials=[]
            chunks=split_text(text,size)
            for index,chunk in enumerate(chunks):
                part=self.folder(state)/f'summary_part_{index}.json'
                if not part.exists():
                    log(f'任务 {state["task_id"]}：请求云端摘要，第 {index+1}/{len(chunks)} 段')
                    result=self.parse_summary(self.llm.call(self.prompt('summarize'),chunk,True))
                    write_text(part,json.dumps(result,ensure_ascii=False))
                partials.append(json.loads(read_text(part)))
            if len(partials)==1:result=partials[0]
            else:
                log(f'任务 {state["task_id"]}：汇总 {len(partials)} 段摘要')
                result=self.parse_summary(self.llm.call(self.prompt('summarize'),json.dumps(partials,ensure_ascii=False),True))
            write_text(path,json.dumps(result,ensure_ascii=False))
            write_text(manifest,json.dumps({'fingerprint':fingerprint}))
        else:log(f'任务 {state["task_id"]}：复用已有摘要')
        self.store.update(state['task_id'],summary_path=str(path))
        return {'summary_path':str(path)}

    @staticmethod
    def parse_summary(text):
        result=json.loads(text)
        if not isinstance(result,dict) or result.get('topic') not in {'技术','行业','产品','研究','其他'}:
            raise ValueError('摘要主题无效')
        bullets=result.get('bullets')
        if not isinstance(bullets,list) or not 1<=len(bullets)<=64 or any(not isinstance(b,str) or not b.strip() or len(b)>500 for b in bullets):
            raise ValueError('摘要要点无效')
        return {'topic':result['topic'],'bullets':bullets}

    def summary_fingerprint(self,input_path):
        material={'input':read_text(input_path),'prompt':self.prompt('summarize'),'model':self.settings.get('llm','model'),'endpoint':self.settings.get('llm','base_url'),'chunk_chars':self.settings.get('llm','chunk_chars',12000),'version':2}
        return hashlib.sha256(json.dumps(material,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def summary_current(self,input_path,manifest):
        try:return json.loads(read_text(manifest))['fingerprint']==self.summary_fingerprint(input_path)
        except (OSError,ValueError,KeyError,TypeError):return False

    def finish(self,state):
        self.parse_summary(read_text(state['summary_path']))
        self.store.update(state['task_id'],status='done',error='')
        return {}

    def run(self,task):
        started=time.perf_counter()
        log(f'任务 {task["id"]}（{task["kind"]}）：开始处理，第 {self.store.task(task["id"])["attempts"]} 次尝试')
        try:
            self.graph.invoke({'task_id':task['id']})
            log(f'任务 {task["id"]}：处理完成，总耗时 {time.perf_counter()-started:.1f} 秒，等待日报发布')
        except Exception as exc:
            current=self.store.task(task['id'])
            maximum=self.settings.get('worker','max_attempts',3)
            self.store.update(task['id'],status='queued' if current['attempts']<maximum else 'failed',available=time.time()+60*max(1,current['attempts']),error=f"{current['stage']}: {type(exc).__name__}")
            action=f'{60*max(1,current["attempts"])} 秒后重试' if current['attempts']<maximum else '达到重试上限，请在管理页重试'
            log(f'任务 {task["id"]}：失败（{type(exc).__name__}），{action}；总耗时 {time.perf_counter()-started:.1f} 秒')

    def repair_needed(self,task):
        """复核产物，移除空/损坏的派生缓存，使真实节点按缺失步骤补齐。"""
        folder=self.folder({'task_id':task['id']})
        for path in folder.iterdir():
            if not path.is_file() or path.name=='feed.txt':continue
            if path.name not in {'raw.txt','corrected.txt','summary.json'} and not path.name.startswith(('part_','correction_','summary_part_')):continue
            try:
                text=read_text(path)
                if not text.strip():raise ValueError('empty')
                if path.suffix=='.json':self.parse_summary(text)
            except (OSError,ValueError,UnicodeError):path.unlink(missing_ok=True)
        required=['raw.txt','summary.json']
        if task['kind']=='bili' and self.settings.get('pipeline','correction',True):required.append('corrected.txt')
        if any(not (folder/name).is_file() for name in required):return True
        input_path=folder/('corrected.txt' if 'corrected.txt' in required else 'raw.txt')
        return not self.summary_current(input_path,folder/'summary_manifest.json')
