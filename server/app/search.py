"""关键词搜索的 LIKE 模式构造

单独成模块的原因：这条规则以前在 `routes/records.py` 里写了一份、在
`web/views.py` 里忘了写，结果后台页面的搜索框搜「50%」会把全表捞出来
（`%` 被 SQLite 当成通配符），主管拿着"全部记录"当搜索结果去对账。
同一套语义只允许有一处实现，两边都从这里引用。

为什么不能只靠字符串拼接：员工名/标题里真的会出现 `%`、`_`
（"占比50%的样本"、"a_b测试"），这些必须按字面匹配，否则搜索是错的。
"""

from __future__ import annotations

# LIKE 的转义符。选反斜杠是因为 SQLite 默认的 LIKE 不带 ESCAPE 子句时
# 没有转义符语义，必须显式 `ESCAPE '\'` 才生效——所以调用方要把
# escape=LIKE_ESCAPE 一起传，光有 like_contains 是不够的。
LIKE_ESCAPE = "\\"


def like_contains(keyword: str) -> str:
    """把关键词变成安全的 LIKE 模式（前后模糊）。

    转义顺序不能反：必须先转义转义符自己，否则后面插入的反斜杠会被
    再转义一遍，关键词里本来含反斜杠时会变成非法模式。
    """
    escaped = (
        keyword.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"
