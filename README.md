# 鸦闻 · 每日简报（huginn-digest）

面向4GB海外Linux服务器的B站/RSS日报。全部模型调用云端；公开阅读、登录管理，支持配置站点地址和导航入口。当前为可本地运行的首版，尚未部署服务器或配置DNS。

## 文档入口

[架构与迁移](docs/architecture.md) · [验证证据与待验边界](docs/validation.md) · [项目硬约束](AGENTS.md)。本页维护运行及操作方式，不维护历史测试计数。

## 源码开发启动（非生产部署）

需要Python 3.13、系统FFmpeg/FFprobe。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m interaction.secrets_cli
```

管理密码哈希与会话密钥会自动保存到本地 `config.toml`，不再输出供手工复制。ASR Key填写 `[asr].api_key`，摘要/纠错 Key填写 `[llm].api_key`，可选Cookie文件路径填写 `[bili].cookie_file`。网页与后台在同一应用进程中读取该文件。密码至少10位。首次获取代码先复制 `config.example.toml` 为 `config.toml`；本地配置已被Git排除。

依赖版本要求以requirements.txt、requirements-test.txt和requirements-build.txt为准，包括间接依赖的证书与异步库最低版本。维护开发/构建环境时运行 `.venv\Scripts\python -m pip install --upgrade --upgrade-strategy eager -r requirements-build.txt -r requirements-test.txt`，再执行pip check及离线回归；升级后重新构建发行包才能更新服务器内嵌依赖。

```powershell
# 本机HTTP：config.toml 中 [security] secure_cookie = false；生产HTTPS改true。
.\.venv\Scripts\python huginn_digest.py
```

访问 `http://127.0.0.1:8000`，管理入口 `/admin/login`。默认空来源，不自动扫描个人账号。管理页添加UP主MID/空间地址或RSS；单视频入口只处理该BV及分P。条目标题由总结模型随要点一起改写，不再直接使用视频或RSS的原始标题；历史摘要没有该字段时仍显示原来源标题。最新日报按 技术／产品／行业／研究／其他 固定顺序分节，缺失或未知主题归入“其他”；主题条可跳到对应分节。每条默认显示标题、首条要点预览、来源与发布时间，点标题展开全部要点，条目右下角的 ¶ 是可分享的单条锚点。正文底部有前后一日翻页，阅读旧日期时顶部提供“回到最新”，当天尚未生成时提示“今日尚未更新”并保留最近一期；不存在的日期返回带返回入口的 404 页面。左侧“往期归档”按年、月两级折叠，只展开当前阅读日期所在的分组；年份与月份后显示天数，日期只显示日号，悬停可见完整日期。归档较多时侧栏内部单独滚动，不会把正文挤走。

## Windows本地可执行程序

保留完整dist/app目录。首次运行将config.example.toml复制到dist/config.toml（已有配置不覆盖），编辑配置并执行一次管理员设置：

```powershell
.\dist\app\huginn-digest.exe setup-admin
.\dist\app\huginn-digest.exe
```

一个应用进程同时提供网页和后台任务，访问http://127.0.0.1:8000。无需另开Worker窗口；serve为显式统一命令，web为兼容别名。视频处理仍需系统FFmpeg/FFprobe。按Ctrl+C停止，程序等待当前后台轮次结束、释放任务锁；强制结束时未完成任务下次恢复。默认配置与数据分别位于dist/config.toml、dist/data。

## Linux可执行程序部署（推荐）

生产服务器无需安装Python、pip或虚拟环境。发行包是PyInstaller目录包，目标为x86_64、glibc Linux；当前构建基线Ubuntu22.04，不支持Alpine/musl或ARM。系统仍需FFmpeg/FFprobe和CA证书。不要只复制huginn-digest一个文件，必须保留完整app目录及符号链接。

### 获取构建包

在GitHub仓库的Actions中选择 **Linux executable → Run workflow**；拉取请求和v开头的标签也会触发构建。成功后下载artifact，里面是 `huginn-digest-linux-x86_64.tar.gz` 和校验文件。流程只上传构建产物，不自动部署服务器或发布GitHub Release。本地尚未执行该远端流程，验收结果以Actions记录为准。

也可在Ubuntu22.04的x86_64 Linux或WSL的Linux文件系统（建议~/huginn-digest，避免/mnt/c上的符号链接差异）构建。仅构建机需要Python3.13：

```bash
python3.13 -m venv .build-venv
.build-venv/bin/python -m pip install -r requirements-build.txt -r requirements-test.txt
# 干净构建目录先复制示例为config.toml；不要覆盖已有本机配置。
cp config.example.toml config.toml
.build-venv/bin/python -m unittest discover -s tests -v
.build-venv/bin/python scripts/build_linux.py
.build-venv/bin/python scripts/smoke_bundle.py dist/huginn-digest-linux-x86_64/app/huginn-digest --media
```

构建前通过系统包管理器安装FFmpeg。重复构建时移走dist/huginn-digest-linux-x86_64旧输出目录；构建脚本不会合并旧发行目录。依赖版本、Python和构建平台记录于包内build-info.json，升级依赖后必须重新验收打包产物。

### 首次安装

以下以Debian/Ubuntu为例，安装到 `/opt/huginn-digest`，直接使用当前登录账号运行。先将压缩包及校验文件放在服务器同一目录；root登录时可省略命令中的sudo：

```bash
sha256sum -c huginn-digest-linux-x86_64.tar.gz.sha256
sudo apt-get update
sudo apt-get install -y ffmpeg ca-certificates
sudo install -d -o "$(id -un)" -g "$(id -gn)" -m 750 /opt/huginn-digest
# 仅用于首次安装；升级时不要解压覆盖已有app。
sudo tar -xzf huginn-digest-linux-x86_64.tar.gz --strip-components=1 -C /opt/huginn-digest
cd /opt/huginn-digest
# 已有配置时跳过复制。
cp config.example.toml config.toml
chmod 600 config.toml
app/huginn-digest setup-admin
```

编辑外置config.toml，填写ASR/LLM凭据，生产HTTPS将security.secure_cookie设true；地址和端口统一配置在server段。若需要B站主动续期，配置bili.cookie_file后运行 `app/huginn-digest setup-bili`，按隐藏提示导入配套凭据。

```bash
# 离线验证程序资源、依赖和系统媒体工具，不采集、不调用模型。
app/huginn-digest doctor --media
# 一个进程同时运行网页与后台；试运行后按Ctrl+C停止，再交给systemd。
app/huginn-digest
```

```bash
sudo cp deploy/huginn-digest.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now huginn-digest
sudo systemctl status huginn-digest
sudo journalctl -u huginn-digest -n 100 --no-pager
```

后台启动（包括worker --once）会实际执行ffmpeg和ffprobe的版本检测；缺失、无执行权限、运行失败或超时会给出安装/PATH提示并以失败状态退出，不认领或恢复任务。Web启动也会检查，失败只记录警告，保留公开阅读和管理功能。统一入口在网页生命周期中启动后台线程；缺少媒体工具时保留网页，后台不启动。安装工具后重启程序。systemd运行时可在journalctl中查看提示。该检测确认工具可以启动，不替代doctor --media的实际音频分片验证。

systemd样例显式使用 `/opt/huginn-digest/config.toml`，服务样例以root运行，安装路径变更时同步修改服务中的路径；整个应用进程的内存限制为2GB，4GB服务器仍需监控验收。`worker --once`会实际处理一轮已有来源和任务，仅doctor是纯离线诊断。

程序默认读取app目录上一层的config.toml，不依赖启动时的当前目录；也可使用 `app/huginn-digest --config /绝对路径/config.toml web`。data_dir和cookie_file相对路径统一相对于所选config.toml所在目录；内置模板、提示词和脚本始终读取app内资源。沿用旧安装位置与配置时，默认数据位置不变；如果旧版将自定义配置放在其他目录，请迁移前改成数据和Cookie的原绝对路径。

### 升级与回滚

从其他安装目录或旧项目标识升级：保留原安装目录和数据绝对路径，不要随项目名迁移已有data。新二进制名为huginn-digest；采用新systemd样例前，将WorkingDirectory、ExecStart和--config改为原安装位置。若仍装有其他标识的旧服务单元，先停用并禁用对应旧服务，再启用huginn-digest，避免两个进程同时处理同一份数据。数据库、会话Cookie和B站Cookie可继续读取，但旧凭据文件中的刷新口令不再识别，需重新运行 `app/huginn-digest setup-bili` 才能恢复自动续期；现有DAILY_CONFIG等部署环境变量保留兼容。仅项目标识与默认展示名称变更，已配置的站点域名不会自动修改。

先验证新包校验值，解压到独立临时目录，停止服务并备份配置与完整数据目录。保留旧app目录，将新包完整app目录放到 `/opt/huginn-digest/app`；不要覆盖config.toml、Cookie或data，也不要只覆盖单个可执行文件。检查属主，运行doctor --media，通过后启动服务。检查Web、后台心跳和实际任务；回滚时停服换回旧app。涉及数据库结构变化时，应连同升级前数据快照恢复，不能假定旧程序兼容新数据。更换服务样例后需要daemon-reload。

Cloudflare Tunnel仍由独立cloudflared服务提供入口，公开主机名 `daily.example.com`，源站 **http://127.0.0.1:8000**（与server配置保持一致）。Tunnel凭据不属于应用配置。参考[官方Tunnel文档](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)。HTTPS、管理登录、后台心跳和一次真实处理验收后，再发布导航入口。当前尚未实际部署海外服务器，Linux构建与systemd运行均需服务器验收。

管理页的状态分两层来源：顶部“当前活动”显示后台进程此刻在做什么——来源检查、下载分P、ASR、逐段纠错、逐段摘要或发布，含阶段、分片进度、已运行时长与任务编号；它来自需要 `[server].admin_refresh_seconds` 间隔轮询的 `/admin/status`，只读内存与轻量计数，不触发采集或模型调用。任务与重建列表来自数据库，按约 6 倍间隔（最少 15 秒）刷新 HTML 片段，因此长任务期间进度不会停在旧值，表格也不会被高频重绘。

任务与重建合并为一个面板，用“全部任务 / 进行中 / 失败 / 已完成 / 已撤回 / 已取消 / 重建记录”切换视图，可按日期过滤；原先“最近任务”与“重新汇集日报”重复列同一批内容的问题由此消除。任务状态区分“等待处理”“等待重试（含倒计时）”“正在处理”“已完成”“已撤回”“已取消”“处理失败”，失败原因按“阶段 · 可读结论（异常类型）”显示，悬停前即可判断是模型、网络还是内容校验问题。已撤回条目显示撤回原因与时间。页面底部可展开最近阶段事件，用于核对某任务究竟走到哪一步。

“存储与保留”卡片显示数据目录总占用及分类明细（转写原文、摘要与快照、下载缓存、数据库）、各项保留期、上次清理时间与释放量、下次自动清理时间，并可“立即清理一次”（POST `/admin/cleanup`，需登录与CSRF）。清理失败记录异常类型并以409提示，不影响后续轮次。ASR 与摘要模型是否配置同时显示在“摘要模型与转写”卡片；未配置 ASR 时明确提示涉及视频的任务会在转写阶段失败。注意：存储统计与清理共用同一套分类口径，数据库含 `-wal`/`-shm` 文件。

修改 `config.toml` 的 `[llm]` 段（模型名、接口地址、Key、超时、分片容量、是否关闭思考）后无需重启：后台在任务间隙自动应用，也可在管理页“摘要模型”卡片点“立即重新读取配置”。为避免同一份摘要混用两个模型，处理中的任务不会被中途切换，变更显示为“有变更待应用”，任务结束后生效。配置读取或校验失败时保留上一版并显示原因，Key 和完整接口路径不会出现在页面上。其余配置段仍需重启进程。

页面整体刷新时不会覆盖正在编辑的表单或排序中的来源列表；页面隐藏时暂停轮询；400/401 分别按“日期格式错误”“登录过期”处理。

管理页“订阅来源”中的“编辑”可修改名称、类型和目标。仅改名称保留检查进度；更换类型或地址后以最近24小时重新检查，保留原来源编号、启停状态及已有任务/日报。重复目标或无效输入不会覆盖原设置。

## 调度、状态与发布

- 每小时发现新增内容，来源初次回溯24小时；每轮每来源最多入队20条，重复URL不入队。B站每轮最多检查10页，按发布时间截断；很久未运行后超出300条的来源需分批手动补视频，不默认扫全历史。RSS中缺少发布时间的条目按发现时刻归入当天，单条私网或非法链接只跳过该条，不影响整个来源。
- Worker串行处理，操作系统文件锁防止双Worker。意外退出后恢复running任务；原稿、分P、纠错分片和摘要已完成的文件复用。重试有两道上限：`[worker].max_attempts`（默认3，含首次）限一轮内的自动重试，两次之间按60秒递增等待；`[worker].max_total_attempts`（默认10）限同一任务的**终身**累计尝试，含管理页手动重试与重建补齐。手动重试不再把累计次数清零，因此反复点击不会无限重试——达到终身上限后按钮消失、直接POST返回409，状态说明提示改用“重建该日日报”。网络请求自身另有独立的有限重试。
- 处理链为获取→ASR（仅视频）→校对（仅视频）→摘要→发布。纠错结合同一视频标题和相邻原稿，有依据才恢复专名，无法可靠恢复的词保留原词并标记【待核】。每段只执行一次模型纠错，完整设置见config.example.toml。长文逐段摘要，再汇集；首轮某段失败只回退该段原稿，其他有效修正保留，后台明确标记降级。RSS正文失败时使用订阅摘要并标记信息不足。
- 原稿、纠错稿、摘要分别存储，公开页只显示摘要与来源链接。按原发布时间归入北京时间日期，次日08:00定稿；迟到成功内容产生补充版本。后台只在完成状态变化时重算发布，空闲轮次不重复读取历史摘要，并会补齐“任务完成但尚未发布”的情况。历史版本写入后不被改写，当前展示的版本由数据库活动指针决定；内容回退到曾发布过的版本时只切换指针，不新增文件。
- 存储分两类控制，都可设 `0` 关闭（即升级前的永久保留行为）。**转写类**：`[storage].transcript_days`（默认30）清理 `raw.txt`、`corrected.txt`、`feed.txt`、分片与纠错清单；实测单条约90KB，日报展示不依赖它们，删除后首页与历史日报照常显示，只是该日再“重建”会重新下载与转写。清理只按年龄、不受 `cache_max_mb` 驱动，因为重新生成要再付出下载与ASR成本。**日报类**：`[storage].report_days`（默认1095，约3年）给日报设上限；实测每条约1.6KB（单日20条约32KB、3年约35MB），到期后该日的版本行、磁盘快照、任务索引与撤回记录整日删除，归档导航随之消失。
- 日报到期删除是不可恢复的：删掉任务索引行才能避免下一次发布把该日“复活”，代价是失去该 URL 的去重记忆，若该条仍在来源的时间窗内被再次发现，会被当作新内容重新处理。需要长期留档时应把 `report_days` 设为 `0` 或调大，而不是依赖事后恢复。

- 日报按主题组织，完全相同URL只收录一次；首版不把不同来源的相似报道强行合并，以免错误吞掉不同观点。来源删除与暂停的含义不同：**暂停**只停止发现新内容，已入队任务照常处理完，已发布日报保留；**删除**除停止发现外，还会把该来源尚未完成的任务（等待处理与处理失败）标记为“已取消”，Worker 不再认领，也不会下载该来源的正文或视频，重建该日期时同样跳过这些任务。已完成的摘要和已发布日报不会被删除或改写：已发布过的条目保留发布当时的来源名，日后再重建或重算也不会被改写或降级；只有从未发布过的新条目才会显示“已删除的订阅来源”。若删除发生在任务已认领、尚未开始抓取的间隙，处理链在获取正文前会再次确认来源存在并立即停止，不进入重试。取消的任务不会占用自动重试次数，管理页“已取消”视图可查看；要恢复采集请重新添加来源。来源编号永不复用，删除后新来源不会继承旧来源的任务归属。
- “撤回条目”用于内容本身不该继续展示的情况（加错来源、版权或合规要求）。撤回在“已完成”或“已撤回”视图操作，需登录并带CSRF；它只把该条从日报中拿掉，**不删除**摘要文件、原始转写、任务记录或历史版本，因此在“已撤回”视图点“恢复”即可完全还原。撤回只重算所属那一天，其他日期不受影响；被撤回的条目在后续重建、重算与重新发布中一直保持隐藏（否决记录持久生效，不会因再次发布而复活）。若某天全部条目都被撤回，该日不再有活动版本，会从归档导航中消失，`/daily/{日期}` 返回404，而不是显示空日报；恢复任一条目后该日重新出现。撤回与恢复都写入阶段事件（含填写的原因，保留30天）以便追溯。撤回不是“删除日报”：日报版本与产物仍按 `[storage].intermediate_days` 正常保留。
- “重建日报”提交持久任务。Worker先强制检查启用来源中所选北京时间日期的条目（不受常规一小时检查间隔与首次24小时回溯限制），仍保留每来源单轮20条和B站10页的采集上限。不会无界扫描历史；RSS只能补取当前订阅源仍提供的历史条目。
- 然后检查该日期已有任务与原稿、纠错稿、摘要文件，缺失/空白/损坏产物进入生产流水线补齐，完整产物复用。失败或未完成任务获得一轮重新处理机会。可以为尚无摘要的日期提交重建。
- 补齐后汇集成功摘要并发布；来源错误、未完成内容或采集达到上限时，状态明确标为未全部完成，保留已有日报，修复后可再次提交。内容未变则复核后保留既有版本。失败、重试中或摘要损坏的任务沿用其上一次成功条目，日报不会因处理失败而缩水；单条摘要缺失只跳过该条并记录日志。重复的活跃重建请求直接返回409，后台未启动时保持排队。
- 除单日重建外，管理页提供“最近 N 天”范围重建（1/3/7/14/30 天，POST `/admin/rebuild/range`）。它把区间展开成多条按日的重建任务并共用一个批次号，因此仍保留每日去重、失败隔离与逐日发布——某天失败不影响其他天。只入队“有任务或已发布版本”的日期，完全空白的日期不入队，避免产生一串必然失败的记录；区间内没有任何可用内容时返回409。**整批来源只扫描一次**：首个任务按区间起点到终点采集一次，后续任务复用该结果，不会把每个来源重复扫 N 遍，也不会让 B站分页上限被多日重复消耗。批次进度可用 `Store.batch_progress` 查询，管理页重建列表标注所属批次。
- 已确认的专名错误可在 `[correction]` 添加 `terms` 列表，格式见config.example.toml。每项包含单个BV号 `video`、正确名称 `name`、明确错写列表 `aliases` 和人工确认依据 `evidence`。仅在对应视频内按完整词边界匹配，不做模糊匹配、不跨视频替换，也不会自动把模型猜测写入术语表；`evidence` 是人工依据说明，不代表程序联网核验。先保留原始ASR，再用确认后的正文纠错；型号数字仅允许这些明确映射变化，其他数字仍受保护。术语变化后重建会重新纠错并复用已有ASR。
- 重建先校验纠错稿；原稿、标题、纠错提示词、模型或纠错设置变化，以及旧稿缺少校验记录时，会重新纠错并归档旧稿。部分分片失败时复用成功分片，仅补做失败分片。重建复用校验一致的摘要；原文、摘要提示词、模型、接口地址或分片容量变化，以及没有校验记录的旧摘要，会重新生成摘要。旧摘要和分片先归档，已发布日报版本保留。不会仅因重建而重复执行已有ASR。非活动旧版本在 `[storage].intermediate_days` 后由清理任务连同索引行删除，活动版本始终保留。

- Web读取已生成内容，不同步等待ASR。当前管理页提交视频需要先查询元信息，查询有超时；正文处理仍在Worker。

## 接口与安全

公开：`GET /`、`/daily/{YYYY-MM-DD}`、`/api/public/days`、`/api/public/daily/{day}`、`/api/public/preview`、`/health`。健康检查仅说明Web可用；Worker心跳在管理页单独查看。预览接口可用于后续Nav_page只读卡片，本版导航只有普通跳转。

管理入口：`/admin/login`、`/admin`；来源管理、单视频、重试、重建、撤回/恢复和模型配置重载均为带CSRF校验的POST。`GET /admin/status` 返回实时状态摘要，需要管理员会话，仅含阶段、计数、模型名与接口主机名，不含正文、凭据或完整URL。签名会话12小时、HttpOnly/SameSite Strict；退出时服务端撤销会话，复制的旧Cookie也会失效；密码scrypt哈希。无配置时管理入口关闭。登录失败按连接来源IP限速，不信任客户端转发IP请求头；反向代理下访问者可能共享限速额度。原始任务错误只存阶段和异常类型，不存请求响应正文。首次启动缺少config.toml时命令行会提示复制示例文件；`[site].public_url`/`nav_url`留空时页面不输出canonical与导航链接，不会导致整站500。

RSS地址和重定向拒绝私网/回环与非HTTP(S)，正文下载限制5MB；不要让不可信用户访问管理权限。来源正文与模型输出经模板自动转义，不渲染任意HTML。RSS实际连接只使用已验证的公网IP，逐次检查重定向；模型接口必须使用HTTPS，ASR与模型请求禁止跟随重定向。

HTTP请求体在表单解析前检查实际字节数，超限返回413；管理表单字段过长返回422，不执行对应操作，也不回显输入值。限制统一在config.toml的 `[web_limits]` 调整，缺少该段的旧配置沿用默认值，修改后重启生效。完整字段与默认值见[配置示例](config.example.toml)。Nginx的请求体上限可设为相同或更小值，应用自身仍独立检查。

公网部署保持 `[server].host = "127.0.0.1"`，由HTTPS反向代理或Tunnel提供入口，`[security].secure_cookie = true`。不要把项目目录作为静态文件目录，也不要通过代理公开config.toml、Cookie、data或备份文件。Linux配置与Cookie权限保持600，备份按同等敏感级别保管。

systemd样例以root运行；应用漏洞一旦导致进程被控制，攻击者可取得该账号权限及其可读凭据。应用不能防护已被控制的服务器或运行账号。可使用服务器上已有的普通账号减少权限范围，无需创建新用户。云服务Key应限制权限和额度，疑似泄漏后在提供方撤销并更换；Cookie应退出对应登录会话后重新导入。

## 验证、备份与恢复

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
.\.venv\Scripts\python -m compileall -q config.py interaction orchestration model integration
```

回归测试不触网不加载模型。新增功能必须测试真实生产入口并打桩昂贵依赖。真实服务验收另记录模型、样本、时间和失败边界。

备份前运行 `sudo systemctl stop huginn-digest`，备份配置 `[storage].data_dir` 指定的整个目录（默认 `/opt/huginn-digest/data`，包括SQLite、正文和日报版本），并单独保管config.toml。备份完成后启动服务。

恢复到原来的绝对路径，并确保服务运行账号可读配置、可写数据，再启动服务；不要只还原SQLite而丢失引用文件。首版不支持直接搬动数据路径，单机SQLite只运行一个Worker。

## 统一配置优先级

显式构造参数 > 非空环境变量 > config.toml。 项目不加载或分发.env文件，不依赖python-dotenv；配置示例只提供config.example.toml。环境变量是操作系统/systemd注入的部署覆盖，与.env文件无关。留空的环境变量不会遮蔽配置文件。`[llm]` 段在保存后由后台自动热加载（见“调度、状态与发布”前文与管理页说明），显式构造的 `llm_key` 与 `LLM_BRAIN_API_KEY` 仍优先于文件；其余段落修改后需重启应用。`config.toml` 不提交，源码开发时位于项目目录，发行部署时位于app目录上一层；仓库只包含空凭据 `config.example.toml`。旧环境变量如仍有值会覆盖文件，迁移时清除即可。Cloudflare Tunnel的凭据由原生cloudflared服务单独管理，不属于Python应用配置。

## B站订阅出现风控或未登录

Cookie文件存在、含SESSDATA不代表登录态仍有效。登录检查返回-101表示未登录；在浏览器重新登录B站后，从实际请求头复制完整Cookie到配置指定的本地文件（单行name=value; name=value格式），不要只复制SESSDATA名称，也不要把Cookie发到聊天或提交仓库。重启Worker，再在管理页对来源执行“检查更新”。

投稿接口-352或HTTP412/429会记录为来源错误，不再伪装成零投稿。系统按订阅检查周期冷却，不立即反复请求；有效登录仍可能触发风控。不要连续点击检查更新，可稍后重试或先用指定视频入口。

Cookie文件首次仍可粘贴单行请求头；运行后，采集器使用CookieJar接收服务器更新，并将B站域、仅HTTPS的有效Cookie原子保存为LWP格式（Linux权限600），保留到期时间供下次启动使用。文件需可写，所在目录需允许创建临时文件；写入失败仅提示权限问题，不输出凭据。项目内Cookie保存、导入与续期共用文件锁；检测到人工替换时，旧会话跳过保存，下次采集读取新文件。人工编辑不参与文件锁，请先停止服务再替换。需要重新导入时可将文件整体替换为新的单行Cookie，后续新建采集器会读取。

主动续期需一次性导入配套的Cookie和刷新口令。先在config.toml配置 `[bili].cookie_file` 为受限、可写的本地文件路径（如 `data/bili-cookie.txt`），然后运行：

```powershell
# 源码开发入口；Linux发行包使用 app/huginn-digest setup-bili。
# 本命令只保存凭据，不访问B站。
.\.venv\Scripts\python -m interaction.bili_credentials_cli
```

在浏览器登录B站，打开开发者工具：从实际B站请求复制完整Cookie；在“应用/Application → 本地存储/Local Storage → https://www.bilibili.com”查找 `ac_time_value`。在本机终端的隐藏输入中分别粘贴两者，必须来自同一次登录；不要发到聊天。命令会整体替换Cookie文件，刷新口令和后续轮换值只保存在该凭据文件，不写入config.toml、数据库、正文或日志。原有只含Cookie的文件仍可用，但没有刷新口令时不主动续期。

重启服务后，采集器在访问B站前按 `[bili].auto_refresh` 与检查周期判断是否需要续期。刷新时先将新Cookie、新口令和待确认旧口令原子保存，再确认废止旧凭据；确认超时会保留恢复信息，冷却后先补确认。失败不会循环刷新，按配置冷却，明确报告登录失效或续期失败；-352/412/429仍按风控或限流处理。此流程仅覆盖B站主站/API，不执行其他站点的跨域SSO。

确认刷新会使被替换的旧登录凭据失效；如果与日常浏览器共用同一登录，浏览器也可能需要重新登录，浏览器自行续期也可能让程序副本失效。建议使用独立登录会话供后台使用。已过期、被注销或被强制下线的登录态，仍可能需要重新导入配套凭据；续期不能保证绕过风控。

## 后台运行日志

Worker终端按北京时间打印启动/恢复任务数、来源检查与分页、候选/入队数量、任务尝试次数、下载分P、ASR分片上传与返回字数、纠错与摘要分段、降级、失败重试和日报发布。各阶段打印耗时并即时刷新；空闲等待仅在进入等待状态时打印一次，不每5秒刷屏。长时间云端请求会先打印正在等待对应阶段，完成或超时后继续打印。日志不打印正文、请求头、Cookie值或密钥。修改后需重启Worker；systemd下通过journalctl查看。

## 空间清理

Worker在流水线空闲时每小时检查一次可再生产物。`[storage]` 的 `cache_days`（默认1天）控制下载及音频临时残留，`intermediate_days`（默认7天）控制已完成任务的ASR/纠错/摘要分片、旧纠错与摘要归档以及非活动日报版本；`cache_max_mb`（默认512）是上述可清理文件的合计软上限，超出时按最旧优先提前清理。设置统一写入config.toml，重启Worker生效。

原稿、纠错稿、当前摘要及校验文件、当前活动日报、数据库和未完成任务的文本分片不删除；非活动日报版本在保留期后连同索引行一起清理，孤儿快照立即删除。清理不触发模型调用。上限不包含保留产物，也不限制正在运行任务的瞬时媒体用量；原稿和日报仍会随使用增长。已清理的分片将失去对应细粒度续跑能力，但完整聚合文件可继续复用。清理日志只显示文件数和释放空间；本次未对已有数据执行批量删除。

订阅来源支持拖动名称旁的⋮⋮手柄排序，也可聚焦手柄后用上下方向键调整；松开后自动保存，失败恢复原顺序。顺序持久保存，新来源追加到末尾；Worker也按该顺序检查来源，已入队任务仍按任务ID处理。
