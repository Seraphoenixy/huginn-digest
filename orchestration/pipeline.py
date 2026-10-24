"""唯一处理图：获取→纠错→摘要→完成。state只含ID和路径。"""
import json
import hashlib
import shutil
import time
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from config import ROOT
from integration.files import read_text, write_text
from integration.sources.bili import BiliVideoSource, extract_bvid_from_url
from integration.sources.rss import extract
from model.asr import CloudASR
from model.text import TextModel
from integration.runtime_log import log
from orchestration.terminology import apply_terms,check_terms,term_pattern
from orchestration.status import STAGE_LOG_LABELS, TOPIC_ORDER


class SourceDeleted(RuntimeError):
    """来源在处理途中被删除；任务应终止并标记取消，而不是重试。"""


def split_text(text,size,protected=()):
    """优先沿段落/句末分片，保留全部字符；超长单句才按容量截取。"""
    if size<=0:raise ValueError('分片容量必须为正数')
    import re
    protected_pattern=term_pattern(protected) if protected else None
    longest=max(map(len,protected),default=0)
    if longest>size:raise ValueError('受保护术语超过分片容量')
    chunks=[]
    while len(text)>size:
        candidates=[m.end() for m in re.finditer(r'\n|[。！？!?；;]',text[:size]) if m.end()>=size//2]
        end=candidates[-1] if candidates else size
        if protected_pattern:
            for match in protected_pattern.finditer(text[:size+longest]):
                if match.start()<end<match.end():
                    end=match.start()
                    break
        chunks.append(text[:end]);text=text[end:]
    if text:chunks.append(text)
    return chunks

class State(TypedDict, total=False):
    task_id: int
    raw_path: str
    corrected_path: str
    summary_path: str


class SourceDeleted(RuntimeError):
    """来源在处理途中被删除；任务应终止并标记取消，而不是重试。"""


class ContentPipeline:
    def __init__(self, settings, store, progress=None):
        self.settings, self.store = settings, store
        self.progress_callback=progress
        self.active_task=None
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

    def report(self,stage,detail='',current=None,total=None):
        """向同进程运行状态上报进度；上报失败绝不能中断内容处理。"""
        callback=self.progress_callback
        if callback is None:return
        try:callback(self.active_task,stage,detail,current,total)
        except Exception as exc:log(f'任务进度上报失败（{type(exc).__name__}）')

    def traced(self, name):
        def node(state):
            task_id=state['task_id']
            self.store.update(task_id,stage=name)
            self.store.event(task_id,name,'started',STAGE_LOG_LABELS[name])
            self.report(name,f'开始{STAGE_LOG_LABELS[name]}')
            started=time.perf_counter()
            log(f'任务 {task_id}：开始{STAGE_LOG_LABELS[name]}')
            try:
                result=getattr(self,name)(state)
                elapsed=time.perf_counter()-started
                self.store.event(task_id,name,'ok',f'耗时 {elapsed:.1f} 秒')
                log(f'任务 {task_id}：{STAGE_LOG_LABELS[name]}完成，耗时 {elapsed:.1f} 秒')
                return result
            except Exception as exc:
                elapsed=time.perf_counter()-started
                self.store.event(task_id,name,type(exc).__name__,f'耗时 {elapsed:.1f} 秒')
                log(f'任务 {task_id}：{STAGE_LOG_LABELS[name]}失败（{type(exc).__name__}），耗时 {elapsed:.1f} 秒')
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
        if task['source_id'] is not None and not self.store.source_exists(task['source_id']):
            # 认领后、抓取前来源被删除：立即停止，不访问已删除来源的地址。
            raise SourceDeleted('来源已删除，任务取消')
        path=self.folder(state)/'raw.txt'
        if not path.exists():
            if task['kind']=='bili':
                bvid=extract_bvid_from_url(task['url'])
                if not bvid: raise ValueError('无效BV号')
                source=BiliVideoSource(cookie_file=self.settings.cookie_file or None,download_cache_dir=str(self.settings.cache_dir/'downloads'),auto_refresh=self.settings.bili_auto_refresh,refresh_check_seconds=self.settings.bili_refresh_check_seconds,refresh_retry_seconds=self.settings.bili_refresh_retry_seconds)
                try:
                    self.report('acquire','读取视频分P信息')
                    pages,_=source.get_video_pages(bvid)
                    if not pages: raise ValueError('视频不可访问或没有分P')
                    texts=[]
                    for index,page in enumerate(pages,1):
                        number=page['page']
                        part=self.folder(state)/f'part_{number}.txt'
                        if not part.exists():
                            log(f'任务 {task["id"]}：开始下载分P {number}，共 {len(pages)} 个分P')
                            self.report('acquire',f'下载分P {index}/{len(pages)}',index,len(pages))
                            media=source.download_media(bvid,number)
                            log(f'任务 {task["id"]}：分P {number} 下载完成，开始ASR')
                            self.report('acquire',f'转写分P {index}/{len(pages)}',index,len(pages))
                            write_text(part,self.asr.transcribe(media))
                        else:
                            log(f'任务 {task["id"]}：复用分P {number} 已有转写')
                            self.report('acquire',f'复用分P {index}/{len(pages)}',index,len(pages))
                        texts.append(read_text(part))
                    write_text(path,'\n\n'.join(texts))
                finally:
                    shutil.rmtree(self.settings.cache_dir/'downloads'/bvid,ignore_errors=True)
                    source.session.close()
            else:
                self.report('acquire','获取RSS正文')
                seed=self.folder(state)/'feed.txt'
                text,warning=extract(task['url'],read_text(seed) if seed.exists() else '')
                write_text(path,text)
                if warning:self.store.update(task['id'],warning=warning)
                if warning:log(f'任务 {task["id"]}：正文获取降级，使用RSS内容')
        else:log(f'任务 {task["id"]}：复用已有原稿')
        self.store.update(task['id'],raw_path=str(path))
        return {'raw_path':str(path)}

    def confirmed_terms(self,task):
        video=extract_bvid_from_url(task['url'])
        return [term for term in self.settings.correction_terms if term['video']==video]

    def correction_fingerprint(self,text,title,terms=()):
        context=self.settings.get('correction','context_chars',600)
        material={'input':text,'title':title[:context],'prompt':self.prompt('correct'),
                  'model':self.settings.get('llm','model'),'endpoint':self.settings.get('llm','base_url'),
                  'chunk_chars':self.settings.get('correction','chunk_chars',4000),'context_chars':context,
                  'min_length_ratio':self.settings.correction_min_length_ratio,'terms':terms,'version':4}
        return hashlib.sha256(json.dumps(material,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def validate_correction(self,original,result):
        import re
        if not isinstance(result,str) or not result.strip() or result.strip().startswith('```'):
            raise ValueError('纠错结果格式无效')
        compact=lambda value:re.sub(r'\s+|【待核】','',value)
        if len(compact(result))<len(compact(original))*self.settings.correction_min_length_ratio:
            raise ValueError('纠错结果疑似遗漏正文')
        if self.correction_numbers(original)!=self.correction_numbers(result):raise ValueError('纠错结果改变了数字')
        return result

    @staticmethod
    def correction_numbers(text):
        """只归一明确的数字书写差异；型号中的连字符不视为负号。"""
        import re
        import unicodedata
        text=unicodedata.normalize('NFKC',text).replace('【待核】','').replace('−','-')
        digits={'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
        def chinese(match):
            token=match.group()
            if '十' in token:
                left,right=token.split('十')
                return str((digits[left] if left else 1)*10+(digits[right] if right else 0))
            return str(digits[token])
        # 不把“一致”等普通词里的汉字当成数值，也不猜测复杂中文数字。
        text=re.sub(r'(?<![零〇一二两三四五六七八九十百千万亿])(?:[一二两三四五六七八九]?十[一二三四五六七八九]?|[零〇一二两三四五六七八九])(?=[万亿千百年月日天个次倍半%])',chinese,text)
        number=r'\d+(?:[.,]\d+)*(?:%|[万亿千百]|[KMBT](?![A-Z]))?'
        return re.findall(r'(?<![A-Z0-9])[+-]?'+number+'|'+number,text.upper())

    def validate_confirmed(self,original,result,terms):
        if not isinstance(result,str):raise ValueError('纠错结果格式无效')
        result=apply_terms(result,terms)
        result=self.validate_correction(original,result)
        check_terms(original,result,terms)
        return result

    def correction_part(self,path,original,terms=()):
        try:return self.validate_confirmed(original,read_text(path),terms)
        except (OSError,ValueError,UnicodeError):return None

    def correction_current(self,raw_path,task):
        folder=self.folder({'task_id':task['id']})
        try:
            manifest=json.loads(read_text(folder/'correction_manifest.json'))
            text=read_text(raw_path)
            terms=self.confirmed_terms(task)
            result=read_text(folder/'corrected.txt')
            return (manifest.get('complete') is True
                    and manifest.get('fingerprint')==self.correction_fingerprint(text,task['title'],terms)
                    and manifest.get('output_sha256')==hashlib.sha256(result.encode()).hexdigest()
                    and bool(self.validate_confirmed(apply_terms(text,terms),result,terms)))
        except (OSError,ValueError,KeyError,TypeError,AttributeError,UnicodeError):return False

    def correction_warning(self,task_id,message):
        previous=self.store.task(task_id)['warning']
        if previous.startswith(('纠错失败','纠错部分失败','专名复核失败','纠错稿含')):previous=''
        self.store.update(task_id,warning='；'.join(value for value in (previous,message) if value))

    def correct(self,state):
        task=self.store.task(state['task_id'])
        folder=self.folder(state)
        path=folder/'corrected.txt'
        if task['kind']!='bili' or not self.settings.pipeline_correction:
            log(f'任务 {task["id"]}：无需校对，使用原始正文')
            self.store.update(task['id'],corrected_path=state['raw_path'])
            return {'corrected_path':state['raw_path']}
        try:
            text=read_text(state['raw_path'])
            original=text
            terms=self.confirmed_terms(task)
            if not text.strip():raise ValueError('转写为空')
            if self.correction_current(state['raw_path'],task):
                log(f'任务 {task["id"]}：复用校验一致的纠错稿')
                self.store.update(task['id'],corrected_path=str(path))
                return {'corrected_path':str(path)}
            fingerprint=self.correction_fingerprint(text,task['title'],terms)
            text=apply_terms(text,terms)
            manifest=folder/'correction_manifest.json'
            try:reuse=json.loads(read_text(manifest)).get('fingerprint')==fingerprint
            except (OSError,ValueError,AttributeError):reuse=False
            if not reuse:
                cached=[item for item in [path,manifest,*folder.glob('correction_*.txt')] if item.exists()]
                if cached:
                    archive=folder/f'previous_correction_{time.time_ns()}'
                    archive.mkdir()
                    for item in cached:item.replace(archive/item.name)
                write_text(manifest,json.dumps({'fingerprint':fingerprint,'complete':False}))
            chunks=split_text(text,self.settings.get('correction','chunk_chars',4000),[term['name'] for term in terms])
            context=self.settings.get('correction','context_chars',600)
            self.report('correct',f'共 {len(chunks)} 段待校对',0,len(chunks))
            parts=[]
            offset=0
            failed_chunks=0
            for index,chunk in enumerate(chunks):
                data={'video_title':task['title'][:context],
                      'context_before':text[max(0,offset-context):offset],
                      'text':chunk,'context_after':text[offset+len(chunk):offset+len(chunk)+context],
                      'confirmed_terms':[term['name'] for term in terms if term['name'] in chunk]}
                part_path=folder/f'correction_{index}.txt'
                result=self.correction_part(part_path,chunk,terms)
                if result is None:
                    try:
                        self.report('correct',f'校对第 {index+1}/{len(chunks)} 段',index+1,len(chunks))
                        log(f'任务 {task["id"]}：请求云端纠错，第 {index+1}/{len(chunks)} 段')
                        result=self.validate_confirmed(chunk,self.llm.call(self.prompt('correct'),json.dumps(data,ensure_ascii=False)),terms)
                        write_text(part_path,result)
                    except Exception:
                        failed_chunks+=1
                        separator='' if not index or (text[offset-1].isalnum() and chunk[0].isalnum()) else '\n'
                        parts.append(separator+chunk)
                        offset+=len(chunk)
                        log(f'任务 {task["id"]}：本段纠错未通过，仅保留本段输入')
                        continue
                else:self.report('correct',f'复用校对第 {index+1}/{len(chunks)} 段',index+1,len(chunks))
                separator='' if not index or (text[offset-1].isalnum() and chunk[0].isalnum()) else '\n'
                parts.append(separator+result)
                offset+=len(chunk)
            self.report('correct','保存纠错稿')
            result=self.validate_confirmed(text,''.join(parts),terms)
            write_text(path,result)
            write_text(manifest,json.dumps({'fingerprint':fingerprint,'complete':failed_chunks==0,
                                          'output_sha256':hashlib.sha256(result.encode()).hexdigest()}))
            uncertain=result.count('【待核】')
            warning=''
            if failed_chunks:
                fallback='转写及已确认术语' if text!=original else '原始转写'
                failure=f'纠错失败，使用{fallback}生成摘要' if failed_chunks==len(chunks) else f'纠错部分失败，{failed_chunks} 段使用{fallback}'
                warning='；'.join(value for value in (failure,warning) if value)
            if uncertain:
                warning=('；'.join(value for value in (warning,f'纠错稿含 {uncertain} 处待核词，请对照音频确认') if value))
            self.correction_warning(task['id'],warning)
            selected=state['raw_path'] if failed_chunks==len(chunks) and text==original else str(path)
            self.store.update(task['id'],corrected_path=selected)
            return {'corrected_path':selected}
        except Exception:
            log(f'任务 {task["id"]}：纠错失败，降级使用原始转写继续摘要')
            self.correction_warning(task['id'],'纠错失败，使用原始转写生成摘要')
            self.store.update(task['id'],corrected_path=state['raw_path'])
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
            self.report('summarize',f'共 {len(chunks)} 段待摘要',0,len(chunks))
            for index,chunk in enumerate(chunks):
                part=self.folder(state)/f'summary_part_{index}.json'
                if not part.exists():
                    self.report('summarize',f'摘要第 {index+1}/{len(chunks)} 段',index+1,len(chunks))
                    log(f'任务 {state["task_id"]}：请求云端摘要，第 {index+1}/{len(chunks)} 段')
                    result=self.parse_summary(self.llm.call(self.prompt('summarize'),chunk,True),require_title=True)
                    write_text(part,json.dumps(result,ensure_ascii=False))
                else:self.report('summarize',f'复用摘要第 {index+1}/{len(chunks)} 段',index+1,len(chunks))
                partials.append(json.loads(read_text(part)))
            if len(partials)==1:result=partials[0]
            else:
                self.report('summarize',f'汇总 {len(partials)} 段摘要',len(partials),len(partials))
                log(f'任务 {state["task_id"]}：汇总 {len(partials)} 段摘要')
                result=self.parse_summary(self.llm.call(self.prompt('summarize'),json.dumps(partials,ensure_ascii=False),True),require_title=True)
            write_text(path,json.dumps(result,ensure_ascii=False))
            write_text(manifest,json.dumps({'fingerprint':fingerprint}))
        else:
            self.report('summarize','复用已有摘要')
            log(f'任务 {state["task_id"]}：复用已有摘要')
        self.store.update(state['task_id'],summary_path=str(path))
        return {'summary_path':str(path)}

    @staticmethod
    def parse_summary(text,require_title=False):
        """校验摘要JSON。

        标题只在解析新模型输出时强制：磁盘上的历史摘要没有该字段，
        复核已有产物时若一并要求标题，会把它们判为损坏并触发重算。
        """
        result=json.loads(text)
        if not isinstance(result,dict) or result.get('topic') not in TOPIC_ORDER:
            raise ValueError('摘要主题无效')
        bullets=result.get('bullets')
        if not isinstance(bullets,list) or not 1<=len(bullets)<=64 or any(not isinstance(b,str) or not b.strip() or len(b)>500 for b in bullets):
            raise ValueError('摘要要点无效')
        parsed={'topic':result['topic'],'bullets':bullets}
        title=result.get('title')
        if isinstance(title,str):
            title=title.strip()
            if 1<=len(title)<=40 and '\n' not in title and '\r' not in title:
                parsed['title']=title
            elif require_title:
                raise ValueError('摘要标题无效')
        elif require_title:
            raise ValueError('摘要标题无效')
        return parsed

    def summary_fingerprint(self,input_path):
        # version 3：摘要开始带模型标题，旧缓存的要点文本仍可用但与新输出不同源，必须重建。
        material={'input':read_text(input_path),'prompt':self.prompt('summarize'),'model':self.settings.get('llm','model'),'endpoint':self.settings.get('llm','base_url'),'chunk_chars':self.settings.get('llm','chunk_chars',12000),'version':3}
        return hashlib.sha256(json.dumps(material,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def summary_current(self,input_path,manifest):
        try:return json.loads(read_text(manifest))['fingerprint']==self.summary_fingerprint(input_path)
        except (OSError,ValueError,KeyError,TypeError):return False

    def finish(self,state):
        self.report('finish','校验并保存摘要')
        self.parse_summary(read_text(state['summary_path']))
        self.store.update(state['task_id'],status='done',error='')
        return {}

    def run(self,task):
        started=time.perf_counter()
        self.active_task=task['id']
        log(f'任务 {task["id"]}（{task["kind"]}）：开始处理，第 {self.store.task(task["id"])["attempts"]} 次尝试')
        try:
            self.graph.invoke({'task_id':task['id']})
            log(f'任务 {task["id"]}：处理完成，总耗时 {time.perf_counter()-started:.1f} 秒，等待日报发布')
        except SourceDeleted:
            # 来源已删除不是故障，不进入重试队列。
            current=self.store.task(task['id'])
            self.store.update(task['id'],status='canceled',available=0,error='source: SourceDeleted')
            log(f'任务 {task["id"]}：来源已删除，任务取消；总耗时 {time.perf_counter()-started:.1f} 秒')
        except Exception as exc:
            current=self.store.task(task['id'])
            maximum=self.settings.get('worker','max_attempts',3)
            total_limit=self.settings.get('worker','max_total_attempts',10)
            # 两种上限一起看：单轮内允许自动重试到maximum次；手动重试不重置历史，
            # 终身累计到total_limit后不再排队，避免反复点重试形成无限循环。
            exhausted=current['attempts']>=maximum
            over_total=current['attempts']>=total_limit
            if exhausted or over_total:
                self.store.update(task['id'],status='failed',available=0,error=f"{current['stage']}: {type(exc).__name__}")
                reason='已达自动重试上限' if exhausted and not over_total else f'已达终身尝试上限（{total_limit} 次）'
                log(f'任务 {task["id"]}：失败（{type(exc).__name__}），{reason}，不再自动重试；总耗时 {time.perf_counter()-started:.1f} 秒')
            else:
                wait=60*max(1,current['attempts'])
                self.store.update(task['id'],status='queued',available=time.time()+wait,
                                  error=f"{current['stage']}: {type(exc).__name__}")
                log(f'任务 {task["id"]}：失败（{type(exc).__name__}），{wait} 秒后重试；总耗时 {time.perf_counter()-started:.1f} 秒')
        finally:
            self.active_task=None

    def repair_needed(self,task):
        """复核产物，移除空/损坏的派生缓存，使真实节点按缺失步骤补齐。"""
        folder=self.folder({'task_id':task['id']})
        for path in folder.iterdir():
            if not path.is_file() or path.name in {'feed.txt','correction_manifest.json'}:continue
            if path.name not in {'raw.txt','corrected.txt','summary.json'} and not path.name.startswith(('part_','correction_','summary_part_')):continue
            try:
                text=read_text(path)
                if not text.strip():raise ValueError('empty')
                if path.suffix=='.json':self.parse_summary(text)
            except (OSError,ValueError,UnicodeError):path.unlink(missing_ok=True)
        required=['raw.txt','summary.json']
        if task['kind']=='bili' and self.settings.pipeline_correction:required.append('corrected.txt')
        if any(not (folder/name).is_file() for name in required):return True
        if 'corrected.txt' in required and not self.correction_current(folder/'raw.txt',task):return True
        input_path=folder/('corrected.txt' if 'corrected.txt' in required else 'raw.txt')
        return not self.summary_current(input_path,folder/'summary_manifest.json')
