"""读写共享的接口规则，并为网关快照和用户限流返回同一套有效值。"""

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mycomfy.database.traffic_models import TrafficRule

FIELDS = ("edge_rpm", "edge_burst", "user_rpm", "provider_concurrency")


def route_key(route):
    """将路由目录中的一项转换为数据库和网关共用的复合标识。"""
    return route["listener"], route["method"], route["path"]


def _values(row):
    """只返回可用的限流参数，避免将 ORM 对象带出事务。"""
    return {field: getattr(row, field) for field in FIELDS}


def load_rule_overrides(db):
    """一次读取所有人工覆盖，供批量生成网关规则和管理页使用。"""
    return {
        (row.listener, row.method, row.path): _values(row)
        for row in db.scalars(select(TrafficRule))
    }


def effective_rules(db, routes):
    """按完整目录批量取得当前设置；没有持久覆盖的路由使用程序默认值。"""
    from mycomfy.api.traffic_catalog import default_rule

    overrides = load_rule_overrides(db)
    return {
        route_key(route): overrides.get(route_key(route)) or default_rule(route) for route in routes
    }


def effective_rule(db, route):
    """按主键获取单个接口的限流规则，供已鉴权的业务请求使用。"""
    from mycomfy.api.traffic_catalog import default_rule

    row = db.get(TrafficRule, route_key(route))
    return _values(row) if row is not None else default_rule(route)


def store_rule(db, route, rule):
    """在调用方事务中原子替换整条规则或删除覆盖，并发首次保存也只会留下一行。"""
    listener, method, path = route_key(route)
    if rule is None:
        db.execute(
            delete(TrafficRule).where(
                TrafficRule.listener == listener,
                TrafficRule.method == method,
                TrafficRule.path == path,
            )
        )
        return
    values = {"listener": listener, "method": method, "path": path, **rule}
    dialect = db.get_bind().dialect.name
    if dialect == "mysql":
        statement = mysql_insert(TrafficRule).values(**values)
        db.execute(statement.on_duplicate_key_update(**rule))
    elif dialect == "sqlite":
        statement = sqlite_insert(TrafficRule).values(**values)
        db.execute(
            statement.on_conflict_do_update(
                index_elements=["listener", "method", "path"], set_=rule
            )
        )
    else:
        db.merge(TrafficRule(**values))
