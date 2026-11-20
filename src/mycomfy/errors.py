class ApiError(Exception):
    def __init__(self, status, code, message, details=None, headers=None):
        """保存 HTTP 状态、业务错误码、逐项明细及可选响应头。"""
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details or []
        self.headers = headers


def not_found():
    """生成无法访问或不存在的资源共用的 404 响应错误。"""
    return ApiError(404, "NOT_FOUND", "Resource not found")


class InvalidSession(ApiError):
    def __init__(self, inactive_token_digest: str | None = None):
        """保留统一的 401 响应，并标记数据库已确认失效的令牌。"""
        super().__init__(401, "SESSION_INVALID", "Session is invalid or expired")
        self.inactive_token_digest = inactive_token_digest


def invalid_session(inactive_token_digest: str | None = None):
    """生成会话认证错误，可携带仅供服务端清理缓存的令牌摘要。"""
    return InvalidSession(inactive_token_digest)
