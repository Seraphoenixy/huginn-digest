"""进程内实时活动状态：只描述“此刻在做什么”，不作为持久进度源。

持久状态仍以 SQLite 任务/重建记录为准；本对象由同一进程的 Worker 线程写入，
Web 读取快照。这样长任务期间的阶段、分片和耗时无需等待下一次整轮 tick。
"""
import threading
import time
from orchestration.status import stage_label


class RuntimeState:
    def __init__(self):
        self._lock=threading.Lock()
        self._worker_alive=False
        self._last=None
        self._state=self._blank('stopped','后台未启动','等待 Worker 启动')

    @staticmethod
    def _blank(kind,label,detail):
        now=time.time()
        return {'kind':kind,'label':label,'detail':detail,'task_id':None,'title':'','day':None,
                'parent':'','stage':'','current':None,'total':None,'started':now,'updated':now}

    def start(self):
        with self._lock:
            self._worker_alive=True
            self._state=self._blank('starting','后台启动','检查运行依赖与任务锁')

    def stop(self):
        with self._lock:
            self._worker_alive=False
            if self._state['kind'] not in ('idle','stopped'):
                self._last=self._summary()
            self._state=self._blank('stopped','后台已停止','未完成任务将在下次启动时恢复')

    def begin(self,kind,label,*,detail='',task_id=None,title='',day=None,parent='',stage='',current=None,total=None):
        now=time.time()
        with self._lock:
            previous=self._state
            same=(previous['kind']==kind and previous.get('task_id')==task_id
                  and previous.get('day')==day and previous.get('parent')==parent)
            started=previous['started'] if same else now
            self._state={'kind':kind,'label':label,'detail':detail,'task_id':task_id,'title':title,
                         'day':day,'parent':parent,'stage':stage,'current':current,'total':total,
                         'started':started,'updated':now}

    def update(self,**fields):
        allowed={'detail','task_id','title','day','parent','stage','current','total'}
        if set(fields)-allowed:
            raise ValueError('非法活动字段')
        with self._lock:
            # 空闲或停止时不存在“当前动作”，进度只能通过 begin 开启新的活动。
            if self._state['kind'] in ('idle','stopped'):
                return False
            self._state.update(fields)
            self._state['updated']=time.time()
            return True

    def finish(self,label='空闲',detail='等待下一轮调度'):
        with self._lock:
            if self._worker_alive:
                self._last=self._summary()
            self._state=self._blank('idle',label,detail)

    def _summary(self):
        state=self._state
        return {'kind':state['kind'],'label':state['label'],'detail':state['detail'],
                'task_id':state['task_id'],'day':state['day'],'at':time.time()}

    def snapshot(self):
        now=time.time()
        with self._lock:
            state=dict(self._state)
            last=dict(self._last) if self._last else None
            alive=self._worker_alive
        current=state['current']
        total=state['total']
        state['worker_alive']=alive
        state['elapsed']=max(0,now-state['started']) if state['kind'] not in ('idle','stopped') else 0
        state['stage_label']=stage_label(state.get('stage'))
        state['progress']=round(100*current/total) if isinstance(current,int) and isinstance(total,int) and total>0 else None
        state['last']=last
        return state
