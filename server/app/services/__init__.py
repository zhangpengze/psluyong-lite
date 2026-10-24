"""业务服务层（Services）：只放业务逻辑与数据编排。

分层铁律（架构师门禁，逐文件可 grep 实测）：
- 不 import flask.request / Blueprint / g / session——纯参数进、纯结果出；
- 不返回 HTTP 响应（错误用业务异常表达，由路由层映射成状态码）；
- 依赖只向下：services → models/db/storage/overview/search/config，禁止反向
  import routes/web；唯一例外是告警推送的延迟 import bot.report（发 QQ 群属
  下游外设适配，在函数体内延迟引入，不形成模块级依赖）。
"""
