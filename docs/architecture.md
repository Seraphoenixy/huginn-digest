# 架构与迁移映射

强制分层与安全约束以[AGENTS](../AGENTS.md)为准；本页只维护模块迁移及实现机制。启动、配置、部署与用户操作见[README](../README.md)，验证证据见[validation](validation.md)。新项目不依赖旧项目运行目录。

| 原实现 | 新项目 | 决策 |
|---|---|---|
| integration/sources/bili.py | 同路径 | 迁移复用，向实例显式传缓存路径及Cookie文件；投稿失败改为脱敏异常，不用旧config |
| integration/http/siliconflow_asr.py | 同路径 | 复用超时、三次重试、错误脱敏 |
| integration/media.py | integration/media.py | 保留16kHz单声道600秒分片，去除Windows路径依赖，探测失败明确报错 |
| model/asr/siliconflow.py | model/asr.py | 复用串行分片上传，云端适配，不加载本地权重 |
| LangGraph流水线 | orchestration/pipeline.py | 硬依赖，只走图；获取→纠错→摘要→发布 |
| OpenAI兼容调用 | integration/http/llm.py | 保留集中传输，增加显式关闭思考、finish_reason校验，禁止空正文冒充成功 |
| checkpoint / manifest | integration/store.py | SQLite持久任务状态为唯一进度源，文件产物按步骤复用；无第二份LangGraph检查点 |
| tracing | integration/store.py | 任务阶段、错误类型、时间与降级信息落库，不保存请求凭据 |
| pywebview | interaction/web.py | 替换为Linux HTTP服务，浏览器只调用编排门面 |

订阅发现与处理分离。单视频任务不访问UP主列表。Worker持有进程锁，持久任务原子认领；意外退出后重置running，已经落盘的步骤继续复用。正文存储边界遵循AGENTS。

公开页面仅显示已发布摘要和来源链接。原始转写、纠错稿、任务错误和Cookie不公开。公开摘要JSON为后续导航预览准备；Nav_page首版只跳转，不依赖日报可用性。

前端为纯CSS的Fluent 2布局：设计令牌与组件样式集中在base.html内联style，深色模式跟随prefers-color-scheme，不引入外部字体或第三方静态资源；仅使用三个同源脚本（source-order.js、admin-refresh.js、login.js），由CSP限定为'self'。页面结构为归档导航、正文列与信息卡三栏，窄屏按1200/900/640三档降级；正文卡片默认折叠，用原生details/summary展开，summary内保留首条要点的单行预览，不依赖脚本。

归档导航按年→月两级折叠，同样用原生details/summary实现，无需脚本：DailyService.archive只读dates()，把扁平日期串组织成{year, months:[{month, days}]}树，不改动/api/public/days的扁平数组契约。仅当前查看日期所在的年与月带open属性，其余默认收起，因此归档累积多年后侧栏不会一次铺开成几十行；年份与月份旁的count表示该组天数，日期链接只显示日号并用title/aria-label给出完整日期。archive__body限制最大高度并独立滚动，避免长列表撑破sticky侧栏；窄屏≤900px放弃原先的横向胶囊排布，改为可折叠的竖列表，因为年→月层级无法在横向滚动里表达。每个details的summary必须作为第一个子元素，否则浏览器不把它当折叠头，测试用HTML解析器断言了这一点。

日报页的视图模型由DailyService.briefing(day)一次给出：sections按TOPIC_ORDER（技术／产品／行业／研究／其他）分节，缺失或未知主题归入“其他”；stats给出条数、来源数、主题数、内容时段与归档天数；neighbors给出前一日与后一日；archive复用同一份日期列表，避免一次请求里反复查dates()。分组与排序不放在模板：Jinja的groupby会先按键排序，条目缺topic时None与字符串比较直接抛TypeError让整页500；顺序也不交给字符串排序，中文按码位排出的“产品／其他／技术／研究／行业”没有编辑含义。主题词表只在orchestration/status.py定义一次，pipeline的摘要校验、页面分节与统计共用topic_name折叠规则。不存在的日期由daily路由渲染not_found.html并返回404状态码，不改全局HTTPException处理器，管理页写接口的错误响应仍是JSON。base.html的title、description与og标签由路由按日期传入，标题保持日期而不是某条内容（没有重要性信号，按时间挑一条当标题等于编造编辑判断），公开接口/api/public/days与/api/public/daily/{day}的结构不变。

摘要JSON在topic与bullets之外增加title，由同一次summary模型调用一起生成：标题要求不超过24字、直接陈述主体与动作、不用悬念或夸张词、不写来源名与日期。parse_summary的require_title只在解析新的模型输出时为真，复核磁盘历史产物时仍按宽松规则，否则旧摘要会被判损坏并在repair_needed里被删除、触发整批重算。publish优先用摘要标题，历史摘要缺该字段时回退到任务保存的来源标题，因此旧日期不被改写；来源原始标题（尤其是B站视频标题）常与正文不符，公开页不再直接展示。summary_fingerprint的version升到3，且提示词全文本就计入指纹，改动会让旧摘要缓存失效并在下次重建时重新调用模型。

纠错与摘要沿句末或换行分片，超长单句才硬截，原文字符完整保留。视频纠错使用独立的[correction]分片和上下文设置：纠错输入JSON包含本段原稿、同一视频标题及相邻原稿；名称仅有发音相似时保留原词并标【待核】，不调用画面处理或额外采集。每段只执行一次模型纠错，不存在额外的模型复核。上下文仅供参考，不拼入输出，禁止依据常识更改新闻事实。输出检查非空、无代码块包裹及正文长度下限；数字检查归一全角、有限的汉字数字等价表达，并区分英文型号连字符与负号，同时保留数值顺序、正负号、百分号及数量级单位约束，不证明全部语义正确。纠错分片失败仅回退该片原稿，不缓存失败稿为成功。未完成清单触发后续补做，已成功分片复用。待核标记只在正文保留，日志与管理警告仅显示数量，摘要保留对应的不确定性。

人工确认术语由config.py读取 `[correction].terms`，每项强制绑定单个BV号并记录名称、字面别名与确认依据，数量、字段长度及冲突别名受配置校验。编排层只选择当前视频的条目，按词边界、长别名优先一次替换，不进行跨视频模糊替换或自动学习。原始ASR文件不变；数字校验基线为执行明确映射后的正文，数值例外仅来自该映射，其他数值继续保护。确认后的名称不会在分片边界切断，传给模型的名称列表仅包含本段已出现的名称；输出再次规范别名并校验名称出现次数，拒绝模型遗失或替换已确认名称。模型失败保留该段输入（含已确认术语），不会撤销人工修正。术语依据是人工配置，程序不据此声称已联网核实。

correction_manifest.json只保存输入/标题、纠错提示、模型、接口、当前视频术语及纠错设置的联合哈希、完成标记和输出哈希，不记录正文或凭据。匹配且所有分片校对完成才复用聚合稿；未完成流程复用通过校验的纠错分片，旧提示或无校验记录的稿件与分片归档至previous_correction_*。repair_needed纳入纠错校验，提示或规则变化后重建会重新纠错，原始ASR复用。完成任务的旧纠错归档沿已有intermediate_days与容量规则清理，当前纠错稿及校验清单保留。

摘要要求分别覆盖独立主题及文末内容，允许1–64条要点；这只是输出约束，不代表已经验证语义完整。summary_manifest.json记录输入、提示词、模型、接口和分片容量的哈希；重建时摘要校验不一致或缺记录则重新生成，旧摘要与分片归档至previous_summary_*，一致则支持续跑和复用。完成状态仍表示产物格式校验通过，不能当作事实准确或无遗漏的保证。

终端进度日志统一经integration/runtime_log.py输出，编排层记录任务和来源阶段，模型层记录ASR分片进度，采集层（B站设备Cookie、投稿详情、RSS跳过非法链接）同样经此出口，不使用裸print；时区固定北京时间，立即flush。仅记录计数、阶段、耗时和脱敏异常类型，不记录正文或凭据；数据库events仍是持久阶段记录。

管理员会话Cookie作用于本站根路径，公开页只用已验证签名展示登录导航，不授予额外公开数据访问；带会话响应禁止缓存。登录页已有有效管理员会话时直接返回后台，不生成匿名会话；签发/退出时清理旧/admin路径Cookie。跨域导航不共享Cookie。

重建日报为独立持久命令队列rebuilds：Web仅入队，Worker按指定日期检查启用来源并补入新条目，再经ContentPipeline.repair_needed检查产物，缺失任务由同一LangGraph流水线补齐，最终复用publish。来源有界检查保持单轮20条、B站10页上限；超限或失败不冒充完整成功。claim(day=...)限定处理范围，其他日期不被本次补缺认领。重建日期先归一为ISO格式；同日期活跃请求去重（重复提交返回409），崩溃恢复running命令。数据库扩展使用CREATE TABLE/INDEX IF NOT EXISTS，并为versions补充active列及“每天最多一个活动版本”的唯一索引；旧库按每天最大id迁移为活动版本。

范围重建（最近N天）不引入新的任务类型，而是把区间展开成多条按日的rebuilds行共用batch字段：Store.request_rebuild_range在单事务内批量INSERT OR IGNORE，因此沿用按日唯一索引的去重、逐日失败隔离与逐日发布，某天失败不牵连其他天。DailyService.request_rebuild_range先经Store.days_with_content筛掉既无任务又无版本的空白日期，避免入队必然失败的记录。整批来源只扫描一次：_rebuild_inspection在批次首个任务时以区间起止调用poll(day=start,end_day=end)，并在meta写入rebuild_scan:<batch>标记，同批后续任务直接返回空结果；批次最后一个任务完成时清除该标记。Store.recover在崩溃恢复时也清除全部rebuild_scan:*标记，避免重启后沿用崩溃前的一次扫描结果而漏掉其间的新内容。poll的区间模式把候选一次取回后按published落天，不再要求逐日重复请求，因此B站10页上限不会被多日重复消耗。

B站下载Cookie交接唯一出口为BiliVideoSource._populate_download_cookies：通过yt-dlp内存CookieJar传递，仅限HTTPS B站域，不放http_headers，不写临时凭据文件；会话中非B站域和无域Cookie不转交。

缓存清理由integration/cleanup.py唯一执行，DailyService.tick在串行处理及发布后每小时调用，生产入口沿用Worker文件锁。仅扫描cache与done任务的已聚合分片及previous_summary归档，跳过链接与数据目录外路径，逐文件删除，保留主产物及数据库。同时清理reports下未被versions引用的历史快照：活动版本与仍被数据库引用的非活动版本在保留期内不删，供回滚切换。容量预算只计可清理文件，不是磁盘配额；重建与清理不并行。events按30天保留期在清理时裁剪。

转写原文与纠错稿由prune_transcripts按 `[storage].transcript_days`（默认30，0为永久）单独按年龄清理，唯一实现在cleanup.py。清理范围是raw.txt、corrected.txt、feed.txt、part_*.txt、correction_*.txt、previous_correction_*与correction_manifest.json；同时删除清单可避免“清单记录完整但正文已删”的不一致。这些文件体积最大（实测单条约90KB，而日报快照每条约1.6KB），但日报展示只依赖summary.json与versions快照，删除后首页与历史日报照常渲染，实测仍返回200。删除只按年龄、不受cache_max_mb容量压力驱动，因为重新生成要再次付出下载与ASR成本；该日重建时repair_needed会判定缺少原稿并重新采集，属预期行为。仅处理done任务，未完成任务与summary.json、summary_manifest.json、versions快照不受影响。

日报保留上限由 `[storage].report_days`（默认1095，0为永久）控制，在prune_versions入口先调用expired_days列出到期日期，再对每个日期执行_drop_day：删除该日versions行与磁盘快照、整个reports/<day>目录、各任务产物目录，并删除tasks与retractions中该日的行。截止日按北京时间计算，与归档口径一致；判定依据是日期本身而非文件mtime，重新发布旧内容不会延长旧日报的保留期，也不会让它因重新写入而获得新生。任务行必须一并删除，否则publish_dirty会把该日重新发布，删除等于无效；代价是失去该URL的去重记忆，若该条仍在来源时间窗内会被当作新内容重新处理，这一点在README中明确说明。删除不可恢复，需要长期留档时应关闭或调大该设置。

存储可观测性由cleanup.py的disk_usage提供：只读遍历数据目录，按与清理逻辑一致的分类统计转写、摘要、日报快照、缓存与数据库占用；数据库匹配主文件及-WAL/-SHM，因为WAL模式下数据可能不在主文件，只匹配主文件会算成0。统计不跟随符号链接，数据目录缺失时返回全零而不报错。DailyService.run_cleanup是清理的唯一执行入口，定时任务与Web手动POST /admin/cleanup共用：它记录cleanup_at、cleanup_files、cleanup_bytes、cleanup_usage与cleanup_error到meta，失败只记异常类型并返回None（HTTP以409提示），不中断后续轮次。storage_state汇总保留期设置与上述记录供管理页渲染。

来源顺序持久化于sources.position，旧库初始化按原id倒序迁移。reorder_sources在事务内核对完整ID集合并批量更新，拒绝重复、缺失及新增/删除后的过期列表；HTTP排序入口沿用管理员认证及CSRF。前端使用同源外部脚本，CSP仅允许同源脚本。

日报条目发布时保存source_name来源名称快照，前端卡片标题下显示来源（折叠时可见），正文保留原文链接。快照按“该任务已发布过的名字优先”解析：publish在上一条活动版本中读取同任务ID的source_name，仅当任务从未发布过或旧版本缺字段时才用当前订阅名推导。这样订阅被删除或改名后重新发布（重建、摘要重新生成、当日新增条目触发重算），历史条目仍显示发布当时的名字，不会被降级为“已删除的订阅来源”或被新名字改写；同一天新增的条目属于新内容，使用当前名字。旧日报整体缺字段时由report门面关联任务和订阅补充展示，不改写历史文件；订阅已删除与单独提交均有明确回退说明。

发布状态以versions.active活动指针为准，不再用最大id推断当前版本：每天最多一个活动版本，由publish_version在同一事务内切换。publish逐任务隔离，缺失或损坏的摘要只跳过该条并记日志，不阻断整轮；失败、重试中或摘要损坏的任务沿用上一次成功条目，公开日报不会因处理失败而缩水。内容回到曾经发布过的指纹时只切换活动指针、不写新文件，避免孤儿快照。DailyService.publish_dirty按(天数, id和, 最新更新时间)的廉价签名判断是否需要重算，签名未变的空闲轮次不读取历史摘要；单日发布异常不阻断其他日期，且保留该日旧签名以便下一轮重试。

条目撤回的唯一持久状态为retractions表(task_id唯一,day,reason,created)，它记录“否决”而非删除：任务仍为done、摘要与版本文件不动、versions行保留，因此可一键恢复。publish在_merge_previous之后按retracted_ids(day)过滤；顺序是必须的，因为沿用旧条目的逻辑会把已撤内容带回。publish新增allow_empty参数，仅撤回调用方传入：没有任何条目时它不清空日报，而是调用clear_active移除当天活动指针，该日随之从dates()消失，避免出现“空白日报”。dates()只列active=1的日期，这也是归档导航不产生死链的前提；此前未过滤active，活动版本文件丢失时会留下指向404的导航项，prune_versions现在会先把“活动行但文件不存在”降级为非活动，下一次publish重建并重新激活。撤回与恢复都写events(stage=retract)，保留期沿用30天；HTTP入口/admin/tasks/{id}/retract与/restore复用管理员会话与CSRF校验，重复撤回按幂等处理。

B站Cookie由integration/bili_cookies.py的BiliSession统一管理：旧请求头导入CookieJar，响应完成后将受限B站域有效Cookie原子写回原配置文件，使用LWP格式保留有效期及路径。凭据文件的注释元数据保存刷新口令、检查/尝试时间和待确认旧口令；这些均不进入任务state或数据库。导入、保存和续期共用跨进程文件锁，旧会话写入前核对文件版本，采集前重新读取外部变更。下载仍只经_populate_download_cookies交接，失效Cookie不转交。轮换前的内存回滚使用CookieJar.copy()深拷贝；rotate成功但新凭据写盘失败时保留新口令与待确认标记并重试保存，绝不把新Cookie与已作废的旧口令组合写回。

重试有两道上限，二者都在ContentPipeline.run的失败分支内判定。max_attempts限制单轮的自动重试次数，超过即置failed；max_total_attempts（默认10）限制同一任务的终身累计尝试。tasks.attempts由Store.claim在认领时自增，是全生命周期的累计值。此前Store.retry与process_rebuild都会把它重置为0，使每次手动重试都重新获得完整的自动重试额度，等于无上限重试；现在两者都保留累计值，改为只重置等待时间与错误文本。达到终身上限后：Store.retry的SQL仍只作用于failed，但DailyService.retry先行拒绝并抛出带指引的ValueError（HTTP 409），管理页按dashboard注入的exhausted标记隐藏按钮，status.task_state给出“已达终身尝试上限”的说明。重建在重新排队前检查该值，达到上限的失败任务跳过而不复活，避免重建成为绕过上限的第三个入口。

主动续期传输唯一实现为integration/bili_refresh.py：cookie/info → 本地RSA-OAEP SHA256计算CorrespondPath → 读取refresh_csrf → cookie/refresh → 原子保存新Cookie和新口令 → confirm/refresh（携带新CSRF和旧口令）。所有刷新请求固定HTTPS地址、禁用重定向、有超时、不自动重试POST，不转发响应正文或异常私密信息。返回新口令但缺少关键Set-Cookie时拒绝确认。检查周期、失败冷却和开关由config.py统一读取；无口令维持旧行为。确认超时后保留新凭据和待确认标记，冷却后仅补确认；收到刷新响应前断网、确认已被远端处理但本地未收到结果等边界无法提供端到端事务保证，可能需人工重登。文件系统原子替换保证正常进程崩溃下凭据成组更新，不保证硬件断电持久性。协议参考[社区Web Cookie刷新研究](https://github.com/pskdje/bilibili-API-collect/blob/main/docs/login/cookie_refresh.md)，非官方稳定API；不执行跨域SSO。


生产交付为PyInstaller onedir目录包，huginn_digest.py经interaction.cli默认启动网页与后台，提供serve（web兼容别名）、worker（诊断/单轮兼容入口）、setup-admin、setup-bili和doctor入口；交互的管理和诊断只调用DailyService。ROOT为只读内置资源位置，APP_ROOT在冻结程序中为可执行程序目录上一层；配置选择和相对数据路径均由config.py处理，配置、数据、Cookie不写入_internal。--config优先于部署环境覆盖，默认外置config.toml。

构建资源采用huginn_digest.spec白名单，仅模板、同源脚本、提示词及依赖资源随程序收集，禁止把本地config.toml、Cookie或data加入包。GitHub Actions在Ubuntu22.04/x86_64构建，离线回归后验证目录包，再从tar.gz解压到无Python的Ubuntu22.04容器检查启动和媒体分片。tar保留符号链接，sha256校验文件配套输出；产物上传不是生产部署。WSL可复用scripts/build_linux.py，但须匹配目标架构与glibc基线。依赖仍按范围安装，build-info记录解析后的版本；不声称字节级可复现。

冻结Linux进程启动系统FFmpeg/FFprobe时恢复原LD_LIBRARY_PATH；直接分片仅调整子进程env，yt-dlp的串行下载临时恢复环境并在finally还原，避免内置动态库污染系统工具。该环境作用域只用于单后台进程下载，不用于Web请求。doctor仅验证资源、LangGraph装配、下载器/提取器/加密导入、存储和可选本地音频分片，不访问真实来源或模型；worker --once沿生产文件锁和任务恢复流程执行一轮，会处理已有来源和任务。

媒体启动预检唯一实现为integration/media.py的check_media_tools：以清理后的子进程环境执行ffmpeg/ffprobe -version，检查退出码及工具标识，单工具超时10秒，只报告固定原因和安装指引，不输出进程内容。DailyService在Worker文件锁内、LangGraph装配与任务恢复前强制检查，失败退出；Web lifespan经DailyService检查，失败仅警告。doctor --media复用检查后再验证真实分片。

配置入口不读取.env文件，示例仅保留config.example.toml；运行依赖不包含python-dotenv，PyInstaller显式排除dotenv，避免Uvicorn未使用的env_file可选分支将构建机中的dotenv收集进产物。保留的环境变量覆盖直接来自操作系统，由config.py读取，不依赖dotenv。

项目公开标识统一为huginn-digest，中文默认名为鸦闻 · 每日简报；入口/构建规格为huginn_digest.py与huginn_digest.spec，产物、可执行程序和systemd样例使用新标识。持久化兼容边界：保留daily_session、auto-daily-session签名salt、DAILY_CONFIG等环境变量以及数据库/文件结构；B站凭据元数据标记为huginn-digest-login，其他标记不被识别。已有绝对数据路径不得因更名自动搬动。

统一运行入口为单应用进程：FastAPI lifespan通过DailyService.background_worker启动非daemon后台线程，Web与后台共享配置及门面，SQLite每次操作使用独立连接。后台保留操作系统任务锁与LangGraph串行流水线，锁冲突或流水线装配失败阻止应用启动；缺少FFmpeg/FFprobe则仅开放网页并打印后台未启动提示。停止时通过Event中断空闲等待，等待当前轮次结束并join线程后释放生命周期；systemd提供300秒停止期限，超期强制结束后沿持久任务恢复。worker命令保留用于单轮诊断和兼容，正常运行不再单独启动，Linux使用一份huginn-digest.service。外部FFmpeg等媒体子进程仍按需调用。

## 凭据传输与会话边界

公开抓取在integration/http/public.py集中完成：每次建立连接时解析并检查全部地址，直接连接已检查的sockaddr，保留原主机名用于Host、TLS SNI和证书验证，避免检查后再次解析导致DNS重绑定；禁用环境代理并逐次验证重定向。该适配器只应用于RSS/正文，不携带模型Key或B站Cookie。

云端模型地址要求HTTPS且无内嵌凭据；模型与ASR传输禁用重定向，避免上传正文或音频被重放至非预期地址；[Requests传输行为](https://requests.readthedocs.io/en/latest/user/quickstart/)见官方说明。B站下载器日志和视频详情错误使用固定脱敏信息，不回显远端响应或异常文本。

DailyService串行检查登录限速；Web启动入口关闭代理IP头自动信任。退出经DailyService撤销会话，SQLite的meta仅保存令牌SHA256摘要及撤销时间，跨进程重启生效，过期记录在退出时清理；不存会话原文或凭据。

HTTP输入限制由interaction/request_limits.py的ASGI中间件在表单解析前执行：先检查Content-Length，再有上限地读取实际请求体，完整通过后才交给路由；缺失或低报长度、分块传输与multipart都计入相同字节上限，超限返回413。表单各字段通过FastAPI的max_length按解码后字符数校验，超限返回422；统一校验错误处理只输出固定提示，不回显输入值。模板maxlength仅提供输入辅助，服务端校验独立生效。阈值统一由config.py读取[web_limits]，旧配置缺段时回落默认值；业务门面及后台处理流程不变。

登录密码错误、限速或CSRF过期时保留401/403状态，重新渲染带安全提示的登录页；使用同源静态脚本login.js打开原生dialog，关闭后聚焦空密码框，无JavaScript时仍显示页内提示。提示不回填密码；有效匿名CSRF会话复用，无效会话重新签发，不绕过验证或重置限速。

管理页自动更新分两层：同源admin-refresh.js按 `[server].admin_refresh_seconds` 串行GET `/admin/status` 获取进程内实时活动（当前阶段、分片进度、耗时、任务编号、队列计数、活动重建），只更新文本与进度条；HTML片段（来源、任务面板、计数）按至少15秒的倍数间隔GET `/admin`，经DOMParser解析后只替换模板已转义的命名区域，不执行返回页脚本。输入表单不替换，CSRF字段与来源排序令牌随当前会话同步；source-order.js在来源行替换后重新绑定手柄，拖动/保存标记及排序版本阻止刷新覆盖交互或使用排序前的过期响应。隐藏页面暂停请求，失败保留旧内容并重试，401/403停止轮询并提示重新登录。

进程内实时活动由orchestration/runtime.py的RuntimeState承载，只描述“此刻在做什么”，不作持久进度源；持久状态仍以SQLite任务与rebuilds行唯一为准。Worker在来源检查、产物核对、任务执行与重建各阶段写入活动，ContentPipeline经progress回调上报分片进度（下载/转写第N/M个分P、纠错第N/M段、摘要第N/M段）；回调异常只记日志，不中断内容处理。状态词表、展示结论与错误解释集中在orchestration/status.py，Web过滤器和日志共用，避免标签漂移；任务错误仍只存“阶段: 异常类型”，页面按阶段与异常类型映射为可读结论，不回显远端响应。

管理页无重复列表：任务与重建合并为单一面板，用视图（全部/进行中/失败/已完成/已取消/重建记录）与日期过滤切换，同一时刻只渲染一个列表。任务状态由task_state折算为等待处理、等待重试（带倒计时）、正在处理、已完成、处理失败、已取消；重建由rebuild_state给出创建/开始/耗时与按日完成比例。存储层新增dashboard_tasks、task_counts、day_progress、recent_events与finish_rebuild，视图状态经白名单映射为参数化SQL；events新增detail列记录耗时与阶段说明，rebuilds新增created/started以计算耗时，旧库按IF NOT EXISTS/PRAGMA迁移。

来源删除是“停止发现 + 取消未完成”的统一事务，唯一实现为Store.delete_source：在同一BEGIN IMMEDIATE内把该来源queued与failed任务置为canceled（error=source: SourceDeleted）再删除索引行。只删索引行不足以阻止已入队任务继续下载该来源正文；保留failed也不行，因为管理页“重试”会把它重新入队。四道防线保证删除后不再采集：delete_source事务、Store.enqueue拒绝已删除来源（删除与发现并发时不补回）、Store.claim只认领来源仍存在的任务（含cancel_orphan_tasks兜底手工改库）、ContentPipeline.acquire在抓取前再次确认来源存在并抛出SourceDeleted（认领后抓取前被删除时立即停止，不进入重试）。Store.recover同样把已删除来源的running任务转为canceled。发布语义不变：已发布版本不被改写，仅“未完成”判定排除canceled，重建会跳过这些任务并在说明里报告跳过数量。

来源ID由meta中的source_id_seq单调分配，不再依赖SQLite的INTEGER PRIMARY KEY隐式复用：普通主键在删除最大行号后会复用该号，使旧任务的source_id指向新来源、管理页把旧内容显示为新来源。首次分配时以sources与tasks引用的最大ID为种子，旧库无需迁移即可避免冲突。

文本模型配置热重载的唯一实现为config.py的Settings.refresh_llm与parse_llm_config：[llm]段（model、base_url、api_key、timeout、chunk_chars、thinking_disabled）用文件元数据判断变化，先stat后解析，校验失败保留旧值并记录不含凭据的原因；应用时原子替换values['llm']、llm_key与llm_base_url，并递增llm_revision。显式构造的llm_key与LLM_BRAIN_API_KEY仍优先于文件，与统一优先级一致。DailyService.refresh_config在每轮tick与任务之间应用变更，正在处理任务或重建时只登记待应用，避免同一份摘要的缓存指纹跨模型；管理页POST /admin/config/reload可手动触发，处理中同样登记并显示“有变更待应用”。监听地址、会话密钥、数据目录、来源等其他设置仍只在启动时读取，不会因热加载改变。
