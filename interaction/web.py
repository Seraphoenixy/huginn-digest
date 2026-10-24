"""Web仅调用编排门面，公开阅读与管理会话分离。"""
import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from interaction.request_limits import RequestBodyLimitMiddleware
from fastapi.templating import Jinja2Templates
from config import Settings, ROOT
from orchestration.service import DailyService
from orchestration.status import duration_text, explain_error, rebuild_label, stage_label, status_label

TASK_VIEWS={'all','active','failed','done','canceled','retracted'}
# “重建记录”与任务视图共用同一面板，所以它也是合法视图：漏掉它会被折叠成“全部任务”。
PAGE_VIEWS=TASK_VIEWS|{'rebuilds'}

def create_app(settings=None, *, run_background=True):
    settings=settings or Settings()
    service=DailyService(settings)
    @asynccontextmanager
    async def lifespan(app):
        if not run_background:
            service.check_media_tools(required=False)
            yield
            return
        worker = service.background_worker()
        await asyncio.to_thread(worker.__enter__)
        try:
            yield
        finally:
            await asyncio.to_thread(worker.__exit__, None, None, None)
    app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.service=service
    limits=settings.web_limits
    app.add_middleware(RequestBodyLimitMiddleware,max_bytes=limits['request_body_bytes'])

    @app.exception_handler(RequestValidationError)
    async def invalid_fields(request, error):
        # 默认校验响应可能包含输入值，不能把密码或令牌回显给客户端或代理日志。
        return JSONResponse({'detail':'请求字段缺失、格式错误或超过长度限制'},status_code=422,
                            headers={'Cache-Control':'no-store'})
    templates=Jinja2Templates(directory=str(ROOT/'interaction'/'templates'))
    templates.env.filters['beijing_time']=lambda stamp:datetime.fromtimestamp(float(stamp),timezone(timedelta(hours=8))).strftime('%m-%d %H:%M')
    templates.env.filters['beijing_datetime']=lambda stamp:datetime.fromtimestamp(float(stamp),timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S')
    templates.env.filters['status_label']=status_label
    templates.env.filters['stage_label']=stage_label
    templates.env.filters['rebuild_label']=rebuild_label
    templates.env.filters['duration']=duration_text
    templates.env.filters['error_text']=explain_error
    templates.env.filters['ago']=lambda stamp:duration_text(time.time()-float(stamp))+'前' if stamp else '—'

    @app.middleware('http')
    async def headers(request,call_next):
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='strict-origin-when-cross-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
        if request.url.path.startswith('/admin') or request.cookies.get('daily_session'):
            response.headers['Cache-Control']='no-store'
        response.headers['Vary']='Cookie'
        return response

    def render(request,name,**values):
        authenticated=bool(service.session(request.cookies.get('daily_session')).get('admin'))
        public_url=settings.site_public_url.rstrip('/')
        canonical=(public_url+request.url.path) if public_url else ''
        return templates.TemplateResponse(request=request,name=name,context={'site':settings.site_name,'canonical_url':canonical,'page_title':settings.site_name,'description':'','og_type':'website','request':request,'authenticated':authenticated,'web_limits':limits,**values})

    def describe(report):
        """分享用的摘要：只取标题，不含正文与任何凭据。"""
        if not report:return f'{settings.site_name}：来自公开来源的每日资讯摘要。'
        entries=report.get('entries',[])
        titles=[str(entry.get('title') or '').strip() for entry in entries][:3]
        titles=[title for title in titles if title]
        text=f'{report["day"]} 收录 {len(entries)} 条：'+' · '.join(titles)
        return text[:120]

    def missing_day(request,day,latest):
        """不存在的日期渲染页面而不是 JSON：用户多从收藏或旧链接进来。"""
        response=render(request,'not_found.html',day=day,latest=latest,
                        page_title=f'没有 {day} 的日报 · {settings.site_name}',
                        description=f'{day} 没有已发布的日报，可以查看最新一期。')
        response.status_code=404
        return response

    def admin(request):
        session=service.session(request.cookies.get('daily_session'))
        if not session.get('admin'):raise HTTPException(401,'请先登录管理页')
        return session

    def csrf(request,value,require_admin=True):
        session=admin(request) if require_admin else service.session(request.cookies.get('daily_session'))
        if not value or not hmac.compare_digest(session.get('csrf',''),value):raise HTTPException(403,'表单已过期，请刷新后重试')

    def back(view,fallback):
        """操作完成后回到用户当前视图，避免视图被服务端默认值改写。"""
        target=view if view in PAGE_VIEWS else fallback
        return RedirectResponse(f'/admin?view={target}' if target!='all' else '/admin',status_code=303)

    def cookie(response,token):
        # 删除旧版窄路径Cookie，避免同名Cookie相互遮蔽。
        response.delete_cookie('daily_session',path='/admin')
        response.set_cookie('daily_session',token,httponly=True,secure=settings.secure_cookie,samesite='strict',max_age=43200,path='/')
        return response

    @app.get('/health')
    def health():return {'ok':True}

    @app.get('/static/source-order.js')
    def source_order_script():return FileResponse(ROOT/'interaction'/'static'/'source-order.js',media_type='text/javascript')

    @app.get('/static/admin-refresh.js')
    def admin_refresh_script():return FileResponse(ROOT/'interaction'/'static'/'admin-refresh.js',media_type='text/javascript')

    @app.get('/static/login.js')
    def login_script():return FileResponse(ROOT/'interaction'/'static'/'login.js',media_type='text/javascript')

    @app.get('/')
    def home(request:Request):
        model=service.briefing()
        report=model['report']
        return render(request,'daily.html',**model,
                      page_title=f'{report["day"]} · {settings.site_name}' if report else settings.site_name,
                      description=describe(report))

    @app.get('/daily/{day}')
    def daily(request:Request,day:str):
        day=day.strip()
        if len(day)!=10 or not day[4]=='-' or not day[7]=='-':
            return missing_day(request,day[:10],service.briefing()['latest'])
        model=service.briefing(day)
        if not model['report']:
            return missing_day(request,day,model['latest'])
        return render(request,'daily.html',**model,
                      page_title=f'{model["report"]["day"]} · {settings.site_name}',
                      description=describe(model['report']),og_type='article')

    @app.get('/api/public/preview')
    def preview():return service.preview()

    @app.get('/api/public/days')
    def days():return service.dates()

    @app.get('/api/public/daily/{day}')
    def public_daily(day:str):
        report=service.report(day)
        if not report:raise HTTPException(404)
        return report

    @app.get('/admin/login')
    def login_page(request:Request):
        if not service.sessions.ready or not settings.admin_hash:raise HTTPException(503,'管理入口尚未配置')
        existing=request.cookies.get('daily_session')
        if service.session(existing).get('admin'):
            return cookie(RedirectResponse('/admin',status_code=303),existing)
        token=service.new_session()
        response=render(request,'login.html',csrf=service.session(token)['csrf'],error='')
        return cookie(response,token)

    def login_error(request,message,status):
        if not service.sessions.ready or not settings.admin_hash:
            response=render(request,'login.html',csrf='',error='管理入口尚未配置，请在服务器先设置管理密码。')
            response.status_code=503
            return response
        existing=request.cookies.get('daily_session')
        session=service.session(existing)
        token=existing if session.get('csrf') else service.new_session()
        response=render(request,'login.html',csrf=service.session(token)['csrf'],error=message)
        response.status_code=status
        return cookie(response,token)

    @app.post('/admin/login')
    def login(request:Request,password:str=Form(max_length=limits['password_chars']),token:str=Form(max_length=limits['csrf_token_chars'])):
        try:csrf(request,token,False)
        except HTTPException:
            return login_error(request,'登录表单已过期，请重新输入密码。',403)
        ip=request.client.host if request.client else 'unknown'
        if not service.login(password,ip):return login_error(request,'登录失败或尝试次数过多，请检查密码或稍后重试。',401)
        return cookie(RedirectResponse('/admin',status_code=303),service.new_session(True))

    @app.get('/admin')
    def dashboard(request:Request,view:str='all',day:str='',events:int=1,tasks:int=1):
        session=admin(request)
        view=view if view in PAGE_VIEWS else 'all'
        day=day.strip()
        if day:
            try:day=datetime.strptime(day,'%Y-%m-%d').date().isoformat()
            except ValueError:raise HTTPException(400,'日期格式应为 YYYY-MM-DD') from None
        return render(request,'admin.html',csrf=session['csrf'],admin_refresh_seconds=settings.admin_refresh_seconds,
                      **service.dashboard(view,day or None,events,tasks))

    @app.get('/admin/status')
    def admin_status(request:Request):
        """管理页高频轮询的实时状态摘要：只读元数据，不触发采集或模型调用。"""
        admin(request)
        return JSONResponse(service.activity_status(),headers={'Cache-Control':'no-store'})

    @app.post('/admin/config/reload')
    def reload_config(request:Request,view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        changed,state,deferred=service.reload_model_config()
        if not changed and state['error']:
            raise HTTPException(409,f'配置未生效：{state["error"]}；仍使用上一版')
        # 处理中只登记待应用，不做半途切换；页面通过 llm.pending 显示。
        return back(view,'all')

    @app.post('/admin/config/models')
    def save_models(request:Request,
                    llm_base_url:str=Form('',max_length=limits['target_chars']),
                    llm_model:str=Form('',max_length=limits['source_name_chars']),
                    llm_key:str=Form('',max_length=limits['target_chars']),
                    asr_model:str=Form('',max_length=limits['source_name_chars']),
                    asr_base_url:str=Form('',max_length=limits['target_chars']),
                    asr_key:str=Form('',max_length=limits['target_chars']),
                    view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        """保存模型与密钥：只写 config.toml，密钥既不回显也不进入任务库与日志。"""
        csrf(request,token)
        try:
            result=service.configure_models(llm_key=llm_key,llm_base_url=llm_base_url,llm_model=llm_model,
                                            asr_key=asr_key,asr_model=asr_model,asr_base_url=asr_base_url)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        except Exception:
            raise HTTPException(400,'模型配置保存失败，原配置保留') from None
        state=result['llm']
        if result['llm_saved'] and state.get('error') and not state.get('pending'):
            raise HTTPException(409,f'已保存但未生效：{state["error"]}；仍使用上一版')
        return back(view,'all')

    @app.post('/admin/cleanup')
    def cleanup_now(request:Request,view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        """手动执行一次空间清理；与定时任务共用同一实现。"""
        csrf(request,token)
        result=service.run_cleanup()
        if result is None:
            raise HTTPException(409,'清理未完成，请查看服务日志；已有内容未受影响')
        return back(view,'all')

    @app.post('/admin/logout')
    def logout(request:Request,token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        service.logout(request.cookies.get('daily_session'))
        response=RedirectResponse('/',status_code=303)
        response.delete_cookie('daily_session',path='/')
        response.delete_cookie('daily_session',path='/admin')
        return response

    @app.post('/admin/sources')
    def add_source(request:Request,kind:str=Form(max_length=limits['source_kind_chars']),target:str=Form(max_length=limits['target_chars']),name:str=Form('',max_length=limits['source_name_chars']),view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.add_source(kind,target,name)
        except Exception:raise HTTPException(400,'来源无效或已存在，请检查公开地址或MID') from None
        return back(view,'all')

    @app.get('/admin/sources/{source_id}/edit')
    def edit_source_page(request:Request,source_id:int):
        session=admin(request)
        try:source=service.get_source(source_id)
        except ValueError:raise HTTPException(404,'来源不存在') from None
        return render(request,'source_edit.html',source=source,csrf=session['csrf'])

    @app.post('/admin/sources/order')
    def source_order(request:Request,order:str=Form(max_length=limits['source_order_chars']),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.reorder_sources([int(value) for value in order.split(',') if value])
        except ValueError:raise HTTPException(409,'来源列表已变化或顺序无效，请刷新后重试') from None
        return {'ok':True}

    @app.post('/admin/sources/{source_id}/edit')
    def edit_source(request:Request,source_id:int,kind:str=Form(max_length=limits['source_kind_chars']),target:str=Form(max_length=limits['target_chars']),name:str=Form('',max_length=limits['source_name_chars']),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.get_source(source_id)
        except ValueError:raise HTTPException(404,'来源不存在') from None
        try:service.edit_source(source_id,kind,target,name)
        except Exception:raise HTTPException(400,'来源无效或目标已存在，原设置已保留') from None
        return RedirectResponse('/admin',status_code=303)

    @app.post('/admin/sources/{source_id}/{action}')
    def source_action(request:Request,source_id:int,action:str,view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.source_action(source_id,action)
        except ValueError:raise HTTPException(400,'未知操作') from None
        return back(view,'all')

    @app.post('/admin/video')
    def video(request:Request,target:str=Form(max_length=limits['target_chars']),view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.submit_video(target)
        except Exception:raise HTTPException(400,'视频提交失败，请检查BV号、Cookie或网络') from None
        return back(view,'all')

    @app.post('/admin/tasks/{task_id}/retry')
    def retry(request:Request,task_id:int,view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.retry(task_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from None
        return back(view,'all')

    @app.post('/admin/tasks/{task_id}/retract')
    def retract(request:Request,task_id:int,reason:str=Form('',max_length=limits['source_name_chars']),view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        """撤回/恢复已发布条目：与其他写操作共用管理员会话与CSRF校验。"""
        csrf(request,token)
        try:service.retract_entry(task_id,reason)
        except ValueError as exc:raise HTTPException(409,str(exc)) from None
        return back(view,'done')

    @app.post('/admin/tasks/{task_id}/restore')
    def restore(request:Request,task_id:int,view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.restore_entry(task_id)
        except ValueError as exc:raise HTTPException(409,str(exc)) from None
        return back(view,'retracted')

    @app.post('/admin/rebuild/range')
    def rebuild_range(request:Request,days:str=Form(max_length=4),view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        """重建最近 N 天；按日展开入队，复用既有每日重建流程。"""
        csrf(request,token)
        try:
            numeric=int(days)
        except (TypeError,ValueError):
            raise HTTPException(400,'重建天数无效') from None
        try:
            queued=service.request_rebuild_range(numeric)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None
        if not queued:
            raise HTTPException(409,'该区间没有可重建的内容，或这些日期已在队列中')
        return back(view,'rebuilds')

    @app.post('/admin/rebuild')
    def rebuild(request:Request,day:str=Form(max_length=limits['day_chars']),view:str=Form(''),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:queued=service.request_rebuild(day)
        except Exception:raise HTTPException(400,'日期无效或重建请求提交失败，既有日报保留') from None
        if not queued:
            raise HTTPException(409,'该日期已有排队或进行中的重建任务，请等待完成')
        return back(view,'all')
    return app
