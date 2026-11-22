"""在维护凭据保护下管理 C 端用户的分组和权限。"""

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import load_only

from mycomfy.database.models import User, UserGroup, UserGroupMember, UserGroupPermission
from mycomfy.errors import ApiError, not_found

from .pagination import page, read_position
from .permissions import Permission, access_profile, access_profiles

GROUP_KEY_PATTERN = r"^[a-z][a-z0-9_-]{0,47}$"
GroupKey = Annotated[str, StringConstraints(pattern=GROUP_KEY_PATTERN)]


class GroupDefinition(BaseModel):
    """定义分组名称和可以授予的一组权限。"""

    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    permissions: list[Permission] = Field(max_length=len(Permission))


class GroupCreate(GroupDefinition):
    """创建分组时提供稳定的组键。"""

    key: GroupKey


class Memberships(BaseModel):
    """声明某个用户调整后所属的完整分组列表。"""

    model_config = ConfigDict(extra="forbid")
    group_keys: list[GroupKey]


def _replace_permissions(db, group_key, permissions):
    """在调用方的事务中用当前授权列表替换旧权限。"""
    db.execute(delete(UserGroupPermission).where(UserGroupPermission.group_key == group_key))
    db.add_all(
        UserGroupPermission(group_key=group_key, permission=permission)
        for permission in sorted(set(permissions))
    )


def build_group_admin_router(sessions, audit):
    """注册本机分组查询和修改接口，复用维护鉴权和请求审计。"""
    router = APIRouter(prefix="/local/v1")

    @router.get("/groups")
    def list_groups():
        """列出分组及其授权，并提供可配置权限的完整清单。"""
        with sessions() as db:
            rows = db.execute(
                select(UserGroup.key, UserGroup.name, UserGroupPermission.permission)
                .outerjoin(UserGroupPermission, UserGroupPermission.group_key == UserGroup.key)
                .order_by(UserGroup.key, UserGroupPermission.permission)
            )
            groups = {}
            for key, name, permission in rows:
                if key not in groups:
                    groups[key] = {"key": key, "name": name, "permissions": []}
                if permission is not None:
                    groups[key]["permissions"].append(permission)
        return {"items": list(groups.values()), "available_permissions": sorted(Permission)}

    @router.post("/groups", status_code=201)
    def create_group(request: Request, body: GroupCreate):
        """创建命名分组，并原子写入权限和审计。"""
        request.state.target = body.key
        try:
            with sessions.begin() as db:
                db.add(UserGroup(key=body.key, name=body.name))
                db.flush()
                _replace_permissions(db, body.key, body.permissions)
                audit(db, request, 201)
        except IntegrityError as exc:
            raise ApiError(409, "GROUP_EXISTS", "A group with this key already exists") from exc
        return {"key": body.key, "name": body.name, "permissions": sorted(set(body.permissions))}

    @router.put("/groups/{group_key}")
    def update_group(
        request: Request,
        group_key: Annotated[str, Path(pattern=GROUP_KEY_PATTERN)],
        body: GroupDefinition,
    ):
        """锁定组记录并更新组名与授权，现有成员使用更新后的权限。"""
        request.state.target = group_key
        with sessions.begin() as db:
            group = db.get(UserGroup, group_key, with_for_update=True)
            if group is None:
                raise not_found()
            group.name = body.name
            _replace_permissions(db, group.key, body.permissions)
            audit(db, request, 200)
        return {"key": group_key, "name": body.name, "permissions": sorted(set(body.permissions))}

    @router.get("/users")
    def list_users(
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = None,
    ):
        """按账户 ID 分页列出用户名、所属分组和权限并集。"""
        scope = ["maintenance-users", "id.asc"]
        position = read_position(cursor, scope, str)
        statement = select(User).options(load_only(User.id, User.username)).order_by(User.id)
        if position is not None:
            statement = statement.where(User.id > position[0])
        with sessions() as db:
            result = page(
                db,
                statement,
                limit,
                scope,
                lambda user: [user.id],
                lambda user: {"user_id": user.id, "username": user.username},
            )
            # 先按用户分页，再批量填充组与权限，确保多组用户完整地出现在同一页。
            profiles = access_profiles(db, [item["user_id"] for item in result["items"]])
            for item in result["items"]:
                item.update(profiles[item["user_id"]])
        return result

    @router.get("/users/{user_id}/groups")
    def get_memberships(user_id: str, request: Request):
        """查看账户的分组及当前生效的权限。"""
        with sessions() as db:
            user = db.get(User, user_id)
            if user is None:
                raise not_found()
            request.state.target = user.id
            return access_profile(db, user.id)

    @router.put("/users/{user_id}/groups")
    def replace_memberships(user_id: str, request: Request, body: Memberships):
        """原子替换用户分组，空列表表示撤销所有组授权。"""
        requested = set(body.group_keys)
        with sessions.begin() as db:
            # 写接口鉴权同样持有这把用户行锁，修改分组与业务写入按顺序完成。
            user = db.get(User, user_id, with_for_update=True)
            if user is None:
                raise not_found()
            request.state.target = user.id
            existing = set(db.scalars(select(UserGroup.key).where(UserGroup.key.in_(requested))))
            if requested != existing:
                raise ApiError(422, "UNKNOWN_GROUP", "One or more user groups do not exist")
            db.execute(delete(UserGroupMember).where(UserGroupMember.user_id == user.id))
            db.add_all(UserGroupMember(user_id=user.id, group_key=key) for key in sorted(requested))
            db.flush()
            audit(db, request, 200)
            return access_profile(db, user.id)

    return router
