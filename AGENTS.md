# 项目约束

- 单向分层 interaction → orchestration → model / integration / knowledge。交互只调用 DailyService，不直接调用模型与采集器。
- LangGraph硬依赖；正文落磁盘，不落state；任务范围严格绑定来源或指定视频。
- 配置、HTTP、任务存储、发布唯一出口；远端不进本地模型守卫。首版不加载本地模型。
- 所有设置统一经config.py读取config.toml；凭据可写本地config.toml，环境变量仅作为部署覆盖。不进代码/状态/日志/报告。禁止提交config.toml、.env、Cookie、data；只提交无凭据config.example.toml。
- 只发布成功摘要；失败不清空日报；模型文本不作为HTML执行。
- 修改后运行 python -m unittest discover -s tests -v；回归不联网、不加载模型。真实API必须显式授权。
- 架构来源和迁移边界见docs/architecture.md，部署见README.md。

- 正常运行采用单应用进程，网页与后台同启同停；后台使用进程内线程，不要求另启Worker。
- 生产部署采用Linux目录式可执行程序与systemd，服务器无需Python/虚拟环境；源码运行仅用于开发和构建。服务样例位于deploy/，统一读取外置config.toml；构建产物不得包含本机配置、Cookie或data。

## 文档唯一职责

- 本文件：硬约束与代理入口；不要复制完整实现说明。
- [docs/architecture.md](docs/architecture.md)：迁移映射与实现机制。
- [README.md](README.md)：启动、配置、部署与用户操作。
- [docs/validation.md](docs/validation.md)：最新测试结论、真实证据和未验边界，不追加过程流水、重复日期或历轮测试计数；新结论就地替换旧结论。
- 默认值取config.py/config.example.toml，不在记忆里维护第二张配置表。规则变化就地修改权威文件，其他位置只留摘要及链接。
