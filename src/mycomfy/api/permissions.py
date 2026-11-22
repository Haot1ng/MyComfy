"""从 MySQL 读取组权限，供 API 和页面共用。"""

from enum import StrEnum

from sqlalchemy import select

from mycomfy.database.models import UserGroup, UserGroupMember, UserGroupPermission
from mycomfy.errors import ApiError


class Permission(StrEnum):
    """C 端可授权的页面与操作。"""

    VIEW_STRATEGIES = "strategies.view"
    SUBMIT_JOBS = "jobs.submit"
    VIEW_JOBS = "jobs.view"
    CANCEL_JOBS = "jobs.cancel"
    UPLOAD_ASSETS = "assets.upload"
    DOWNLOAD_ASSETS = "assets.download"
    VIEW_NOTIFICATIONS = "notifications.view"
    MARK_NOTIFICATIONS_READ = "notifications.mark_read"


DEFAULT_GROUP_KEY = "standard"


def permission_query(user_id):
    """创建从用户各分组读取权限的查询。"""
    return (
        select(UserGroupPermission.permission)
        .join(UserGroupMember, UserGroupMember.group_key == UserGroupPermission.group_key)
        .where(UserGroupMember.user_id == user_id)
    )


def access_profile(db, user_id):
    """读取分组和权限并集，返回页面可直接使用的访问配置。"""
    rows = db.execute(
        select(UserGroup.key, UserGroup.name, UserGroupPermission.permission)
        .join(UserGroupMember, UserGroupMember.group_key == UserGroup.key)
        .outerjoin(UserGroupPermission, UserGroupPermission.group_key == UserGroup.key)
        .where(UserGroupMember.user_id == user_id)
        .order_by(UserGroup.key)
    )
    # 同一条查询读取两者，保留成员所属但没有授权的分组。
    groups = {}
    permissions = set()
    for key, name, permission in rows:
        groups[key] = {"key": key, "name": name}
        if permission is not None:
            permissions.add(permission)
    return {"groups": list(groups.values()), "permissions": sorted(permissions)}


def has_permission(db, user_id, permission: Permission, *, write=False):
    """以当前分组关系校验权限，写请求持有授权行锁直到事务结束。"""
    statement = permission_query(user_id).where(UserGroupPermission.permission == permission)
    if write:
        # 锁定实际授予权限的组关系，使权限撤销与业务写入按事务顺序生效。
        statement = statement.with_for_update(read=True)
    return db.scalar(statement.limit(1)) is not None


def require_permission(db, user_id, permission: Permission, *, write=False):
    """在缺少本项权限时拒绝请求。"""
    if not has_permission(db, user_id, permission, write=write):
        raise ApiError(403, "PERMISSION_DENIED", "Current groups do not grant this permission")
