"""Web仅调用编排门面，公开阅读与管理会话分离。"""
import asyncio
import hmac
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from interaction.request_limits import RequestBodyLimitMiddleware
from fastapi.templating import Jinja2Templates
from config import Settings, ROOT
from orchestration.service import DailyService

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
    templates.env.filters['status_label']=lambda status:{'queued':'等待处理','running':'正在处理','done':'已完成','failed':'等待重试'}.get(status,status)
    templates.env.filters['stage_label']=lambda stage:{'acquire':'获取正文','correct':'校对转写','summarize':'生成摘要','finish':'处理完成'}.get(stage,stage)

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
        return templates.TemplateResponse(request=request,name=name,context={'site':settings.site_name,'nav_url':settings.site_nav_url,'canonical_url':canonical,'request':request,'authenticated':authenticated,'web_limits':limits,**values})

    def admin(request):
        session=service.session(request.cookies.get('daily_session'))
        if not session.get('admin'):raise HTTPException(401,'请先登录管理页')
        return session

    def csrf(request,value,require_admin=True):
        session=admin(request) if require_admin else service.session(request.cookies.get('daily_session'))
        if not value or not hmac.compare_digest(session.get('csrf',''),value):raise HTTPException(403,'表单已过期，请刷新后重试')

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
    def home(request:Request):return render(request,'daily.html',report=service.report(),dates=service.dates())

    @app.get('/daily/{day}')
    def daily(request:Request,day:str):
        report=service.report(day)
        if not report:raise HTTPException(404,'没有该日期的已发布日报')
        return render(request,'daily.html',report=report,dates=service.dates())

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
    def dashboard(request:Request):
        session=admin(request)
        return render(request,'admin.html',csrf=session['csrf'],admin_refresh_seconds=settings.admin_refresh_seconds,**service.dashboard())

    @app.post('/admin/logout')
    def logout(request:Request,token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        service.logout(request.cookies.get('daily_session'))
        response=RedirectResponse('/',status_code=303)
        response.delete_cookie('daily_session',path='/')
        response.delete_cookie('daily_session',path='/admin')
        return response

    @app.post('/admin/sources')
    def add_source(request:Request,kind:str=Form(max_length=limits['source_kind_chars']),target:str=Form(max_length=limits['target_chars']),name:str=Form('',max_length=limits['source_name_chars']),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.add_source(kind,target,name)
        except Exception:raise HTTPException(400,'来源无效或已存在，请检查公开地址或MID') from None
        return RedirectResponse('/admin',status_code=303)

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
    def source_action(request:Request,source_id:int,action:str,token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.source_action(source_id,action)
        except ValueError:raise HTTPException(400,'未知操作') from None
        return RedirectResponse('/admin',status_code=303)

    @app.post('/admin/video')
    def video(request:Request,target:str=Form(max_length=limits['target_chars']),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:service.submit_video(target)
        except Exception:raise HTTPException(400,'视频提交失败，请检查BV号、Cookie或网络') from None
        return RedirectResponse('/admin',status_code=303)

    @app.post('/admin/tasks/{task_id}/retry')
    def retry(request:Request,task_id:int,token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token);service.retry(task_id)
        return RedirectResponse('/admin',status_code=303)

    @app.post('/admin/rebuild')
    def rebuild(request:Request,day:str=Form(max_length=limits['day_chars']),token:str=Form(max_length=limits['csrf_token_chars'])):
        csrf(request,token)
        try:queued=service.request_rebuild(day)
        except Exception:raise HTTPException(400,'日期无效或重建请求提交失败，既有日报保留') from None
        if not queued:
            raise HTTPException(409,'该日期已有排队或进行中的重建任务，请等待完成')
        return RedirectResponse('/admin',status_code=303)
    return app
