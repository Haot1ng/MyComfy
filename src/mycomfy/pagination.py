"""Opaque keyset cursors bound to their original query and user."""

import base64
import binascii
import json

from .errors import ApiError
from .state import canonical_digest


def invalid_cursor():
    """构造查询游标无效的统一协议错误。"""
    return ApiError(400, "INVALID_CURSOR", "Cursor is invalid for this query")


def encode_cursor(scope: list, position: list) -> str:
    """将查询范围摘要及末条记录的排序键编码为续页游标。"""
    body = json.dumps([canonical_digest(scope), position], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=")


def decode_cursor(token: str | None, scope: list) -> list | None:
    """解析游标并核对其所属用户、排序规则和过滤条件。"""
    if token is None:
        return None
    if not token or len(token) > 2048:
        raise invalid_cursor()
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        cursor_scope, position = json.loads(raw)
    except (ValueError, TypeError, binascii.Error, RecursionError) as exc:
        raise invalid_cursor() from exc
    # 客户端游标仅确定排序位置，每次查询仍由服务端限定归属。
    if cursor_scope != canonical_digest(scope) or not isinstance(position, list):
        raise invalid_cursor()
    return position


def read_position(token, scope, *types):
    """核对排序键数量及类型后返回数据库续读位置。"""
    pos = decode_cursor(token, scope)
    if pos is not None and (
        len(pos) != len(types) or any(type(v) is not t for v, t in zip(pos, types))
    ):
        raise invalid_cursor()
    return pos


def page(db, query, limit, scope, position, render):
    """多取一条记录判断是否有下一页，并只序列化当前页。"""
    rows = db.scalars(query.limit(limit + 1)).all()
    return {
        "items": [render(row) for row in rows[:limit]],
        "next_cursor": encode_cursor(scope, position(rows[limit - 1]))
        if len(rows) > limit
        else None,
    }
