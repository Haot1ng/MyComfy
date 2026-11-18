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


def invalid_session():
    """生成无效或过期会话共用的认证错误。"""
    return ApiError(401, "SESSION_INVALID", "Session is invalid or expired")
