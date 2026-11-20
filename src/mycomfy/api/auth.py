import hashlib
import hmac
import secrets
import unicodedata
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from mycomfy.database.models import LoginLimit, User, UserSequence, UserSession
from mycomfy.database.session import db_now, new_id
from mycomfy.errors import ApiError, invalid_session

COOKIE = "mycomfy_session"
HASHER = PasswordHasher()
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(32))
LOGIN_WINDOW = timedelta(minutes=1)
LOGIN_ATTEMPTS = 10


def digest(value: str) -> str:
    """计算令牌或限流键的 SHA-256 摘要，用作持久化查找键。"""
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_name(value: str) -> str:
    """用 Unicode NFKC、去首尾空格和大小写折叠统一登录名。"""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def password_hash(value: str) -> str:
    """校验密码长度并生成带独立盐值的 Argon2 哈希。"""
    if len(value) < 8 or len(value) > 1024:
        raise ApiError(422, "INVALID_PASSWORD", "Password must contain 8 to 1024 characters")
    return HASHER.hash(value)


def verify_password(hash_value, value):
    """用 Argon2 校验密码，将密码不匹配转换成普通认证失败。"""
    try:
        return HASHER.verify(hash_value, value)
    except VerifyMismatchError:
        return False


def register(db, username, password):
    """原子创建账号及事件序号行，并将重名转换为注册冲突。"""
    username = normalize_name(username)
    if not 1 <= len(username) <= 100:
        raise ApiError(422, "INVALID_USERNAME", "Username must contain 1 to 100 characters")
    user = User(id=new_id("u"), username=username, password_hash=password_hash(password))
    try:
        with db.begin():
            db.add(user)
            db.flush()
            db.add(UserSequence(user_id=user.id))
    except IntegrityError as exc:
        raise ApiError(409, "USERNAME_TAKEN", "Username is already in use") from exc
    return user.id


def check_origin(request, public_origin):
    """校验浏览器写请求的 Origin 与部署的公开来源一致。"""
    if request.headers.get("origin") != public_origin:
        raise ApiError(403, "ORIGIN_REJECTED", "Request origin is not allowed")


def limit_login(db, username, source):
    """按规范化账号和请求来源分别计数，超限时返回剩余等待秒数。"""
    # 账号和来源共享限流表，按固定顺序取锁以免并发登录发生反向锁等待。
    ids = sorted((digest(f"account:{username}"), digest(f"source:{source}")))
    with db.begin():
        now = db_now(db)
        for key in ids:
            row = db.get(LoginLimit, key, with_for_update=True)
            if row is None:
                try:
                    with db.begin_nested():
                        db.add(LoginLimit(id=key, window_at=now, attempts=0))
                        db.flush()
                except IntegrityError:
                    pass
                row = db.get(LoginLimit, key, with_for_update=True)
            if now >= row.window_at + LOGIN_WINDOW:
                row.window_at = now
                row.attempts = 0
            if row.attempts >= LOGIN_ATTEMPTS:
                seconds = (row.window_at + LOGIN_WINDOW - now).total_seconds()
                raise ApiError(
                    429,
                    "LOGIN_LIMIT",
                    "Too many login attempts",
                    headers={"Retry-After": str(int(seconds) + 1)},
                )
            row.attempts += 1


def session_payload(user, session):
    """生成登录或换代后的会话响应，供页面保存代次和 CSRF 令牌。"""
    return {
        "user_id": user.id,
        "username": user.username,
        "session_generation": str(session.generation),
        "csrf_token": session.csrf_token,
    }


def rotate_session(db, user, seconds):
    """在已锁定用户的事务中撤销旧会话并签发下一代凭据。"""
    now = db_now(db)
    if user.current_session_id:
        old = db.get(UserSession, user.current_session_id)
        old.revoked_at = now
    token = secrets.token_urlsafe(32)
    user.session_generation += 1
    session = UserSession(
        id=new_id("s"),
        user_id=user.id,
        token_digest=digest(token),
        csrf_token=secrets.token_urlsafe(32),
        generation=user.session_generation,
        expires_at=now + timedelta(seconds=seconds),
    )
    db.add(session)
    user.current_session_id = session.id
    return session_payload(user, session), token


def login(db, username, password, source, seconds):
    """核验密码后重新锁定账号及凭据版本，使并发登录只保留最后提交的会话。"""
    username = normalize_name(username)
    limit_login(db, username, source)
    with db.begin():
        user = db.scalar(select(User).where(User.username == username))
        original_hash = user.password_hash if user else DUMMY_HASH
        user_id = user.id if user else None
        credential_version = user.credential_version if user else None
    # 高成本密码比较放在事务外，提交新会话前还会锁定并复核凭据版本。
    if not verify_password(original_hash, password) or user_id is None:
        raise ApiError(401, "AUTH_FAILED", "Invalid username or password")
    with db.begin():
        user = db.get(User, user_id, with_for_update=True, populate_existing=True)
        if user.credential_version != credential_version:
            raise ApiError(401, "AUTH_FAILED", "Invalid username or password")
        return rotate_session(db, user, seconds)


def authenticate(db, request, *, write=False, allow_revoked=False, generation=None, page_user=None):
    """校验 Cookie、有效期及当前会话指针，并对写请求复核页签代次和 CSRF。"""
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 64:
        raise invalid_session()
    session = db.scalar(select(UserSession).where(UserSession.token_digest == digest(token)))
    if not session:
        raise invalid_session()
    user = db.get(User, session.user_id, with_for_update=write, populate_existing=True)
    # 用户锁可能等到另一次登录提交后才取得，必须刷新旧会话的撤销状态。
    db.refresh(session)
    # 账号之间的代次可相同。页签在 fetch 请求头或媒体、事件 URL 中附带打开时的身份。
    for expected in (page_user, request.headers.get("x-page-user")):
        if expected is not None and expected != user.id:
            raise invalid_session()
    header_generation = request.headers.get("x-session-generation")
    for expected in (generation, header_generation):
        if expected is not None and expected != str(session.generation):
            raise invalid_session()
    if write:
        if header_generation is None:
            raise invalid_session()
        if not hmac.compare_digest(
            request.headers.get("x-csrf-token", "").encode(), session.csrf_token.encode()
        ):
            raise ApiError(403, "CSRF_REJECTED", "CSRF token is invalid")
    if session.revoked_at and allow_revoked:
        return user, session
    if (
        session.revoked_at
        or session.expires_at <= db_now(db)
        or user.current_session_id != session.id
    ):
        raise invalid_session()
    return user, session


def revoke_session(db, user, session):
    """撤销指定会话，仅在其仍是当前会话时清除账号指针。"""
    if session.revoked_at is None:
        session.revoked_at = db_now(db)
    if user.current_session_id == session.id:
        user.current_session_id = None
