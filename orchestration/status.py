"""状态词表与展示规则唯一实现；Web、日志和监控接口共用，避免标签漂移。"""
import time

STATUS_LABELS={'queued':'等待处理','running':'正在处理','done':'已完成','failed':'处理失败',
               'canceled':'已取消'}
STAGE_LABELS={'acquire':'获取正文','correct':'校对转写','summarize':'生成摘要','finish':'处理完成',
              # 重建与发布阶段：仅出现在活动状态里，不对应流水线节点。
              'check':'核对产物','process':'补齐内容','publish':'发布日报',
              # 清理与撤回不是流水线阶段，只出现在活动状态或事件时间线里。
              'cleanup':'清理存储','retract':'撤回条目'}
# 只有 ASR 转写稿需要校对；RSS 拿到的是已发布的文字，纠错既没用也浪费调用。
# 同一个 correct 阶段因此有两种说法，按任务类型取。
STAGE_LABELS_BY_KIND={'rss':{'correct':'跳过校对'}}
# 主题的唯一权威定义：默认词表覆盖 AI、开源、时事、娱乐等多条内容线。
# 展示顺序是编辑决定的，不能交给模板或数据库排序（中文字符串排序没有语义）。
# 站点可通过 config.toml 的 [topics] order 覆盖主题列表；兜底项固定为“其他”，
# 不在词表里，始终排在最后，避免“末项即兜底”这种隐式约定把正常主题吞掉。
TOPIC_FALLBACK='其他'
TOPIC_ORDER=('AI 与大模型','开发与开源','产品与应用','行业与公司','政策与监管','安全与隐私',
             '时事','游戏','动漫','影视','科技硬件','研究')
DEFAULT_TOPIC_ORDER=TOPIC_ORDER


def topic_order(configured=None):
    """完整展示顺序：配置的主题列表 + 末位兜底项（已含则不再重复）。"""
    values=tuple(configured) if configured else TOPIC_ORDER
    return values if TOPIC_FALLBACK in values else values+(TOPIC_FALLBACK,)


def topic_name(value,configured=None):
    """未知或缺失主题折叠到“其他”，与页面分节、统计保持同一规则。"""
    return value if value in topic_order(configured) else TOPIC_FALLBACK


# 终端日志沿用更具体的阶段名，页面使用更短的动词短语。
STAGE_LOG_LABELS={'acquire':'获取正文/转写','correct':'校对转写','summarize':'生成摘要','finish':'保存结果'}
REBUILD_STATUS_LABELS={'queued':'等待处理','running':'处理中','done':'已完成','failed':'未全部完成'}
# 阶段记录按“轮次”分组展示：一次任务的全部阶段合成一组，避免逐行流水。
EVENT_GROUP_LABELS={'ok':'完成','started':'开始','failed':'失败'}


# 只映射异常类型，不回显远端响应或异常正文；与流水线“阶段: 异常类型”的存储格式对应。
ERROR_HINTS={
    'CloudError':'云端模型调用失败',
    'BiliSourceError':'B站请求失败',
    'RequestsError':'网络请求失败',
    'ConnectionError':'网络连接失败',
    'TimeoutError':'请求超时',
    'ValueError':'内容或配置校验未通过',
    'SummaryValidationError':'摘要格式校验未通过，请查看阶段记录中的分段和具体原因',
    'OSError':'文件读写失败',
    'UnicodeError':'文本编码异常',
    'JSONDecodeError':'模型输出不是有效JSON',
}


def stage_label(stage,kind=None):
    """阶段名；RSS 没有校对这回事，同一阶段按任务类型换说法。"""
    overrides=STAGE_LABELS_BY_KIND.get(kind or '',{})
    return overrides.get(stage) or STAGE_LABELS.get(stage,stage or '—')


def status_label(status):
    return STATUS_LABELS.get(status,status or '—')


def rebuild_label(status):
    return REBUILD_STATUS_LABELS.get(status,status or '—')


def task_state(task,now=None):
    """把数据库状态与可重试时间折算成一个明确的展示结论。"""
    now=time.time() if now is None else now
    status=task.get('status') or 'queued'
    if status=='done':
        # 撤回是发布层的否决：任务本身仍处理成功，但不再出现在日报里。
        if task.get('retracted'):
            return {'state_label':'已撤回','state_class':'muted','state_note':'处理完成，但未包含在日报中'}
        return {'state_label':'已完成','state_class':'ok','state_note':''}
    if status=='canceled':
        return {'state_label':'已取消','state_class':'muted','state_note':'来源已删除，不再处理'}
    if status=='running':
        return {'state_label':'正在处理','state_class':'run','state_note':''}
    if status=='failed':
        if task.get('exhausted'):
            return {'state_label':'处理失败','state_class':'danger',
                    'state_note':f'已达终身尝试上限，不再重试（共 {task.get("attempts",0)} 次）'}
        return {'state_label':'处理失败','state_class':'danger','state_note':'可手动重试或重建该日'}
    available=float(task.get('available') or 0)
    if available>now:
        return {'state_label':'等待重试','state_class':'warn','state_note':f'{max(1,int(available-now+0.999))} 秒后自动重试'}
    return {'state_label':'等待处理','state_class':'warn','state_note':'等待后台认领'}


def rebuild_state(job,now=None):
    now=time.time() if now is None else now
    status=job.get('status') or 'queued'
    created=float(job.get('created') or job.get('updated') or 0)
    started=float(job.get('started') or 0)
    updated=float(job.get('updated') or 0)
    if status=='running':
        elapsed=max(0,now-(started or updated or now))
    elif started and updated:
        elapsed=max(0,updated-started)
    else:
        elapsed=0
    return {'state_label':rebuild_label(status),'state_class':{'done':'ok','failed':'danger','running':'run'}.get(status,'warn') or 'warn',
            'created':created,'started':started,'elapsed':elapsed,'elapsed_text':duration_text(elapsed) if elapsed else '—'}


def explain_error(error):
    """任务错误只保存“阶段: 异常类型”，这里补成可读结论；重建提示原样返回。"""
    value=str(error or '').strip()
    if not value:
        return ''
    stage,separator,exception=value.partition(':')
    stage=stage.strip()
    exception=exception.strip()
    if exception=='SourceDeleted':
        # 取消任务的原因不是故障，不复用“处理异常”文案。
        return '来源已删除，任务已取消'
    if separator and stage in STAGE_LABELS:
        hint=ERROR_HINTS.get(exception,'处理异常')
        suffix=f'（{exception}）' if exception and exception not in STAGE_LABELS else ''
        return f'{stage_label(stage)} · {hint}{suffix}'
    return value


def duration_text(seconds):
    seconds=max(0,int(seconds))
    if seconds<1:
        return '不足 1 秒'
    if seconds<60:
        return f'{seconds} 秒'
    minutes,remain=divmod(seconds,60)
    if minutes<60:
        return f'{minutes} 分 {remain:02d} 秒'
    hours,minutes=divmod(minutes,60)
    return f'{hours} 小时 {minutes:02d} 分'
