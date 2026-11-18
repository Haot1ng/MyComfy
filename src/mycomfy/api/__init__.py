"""HTTP 应用、用户会话、分页与事件订阅。"""


def create_app(settings, *, local=False, sessions=None):
    """从包入口创建 HTTP 应用，单独导入认证或分页时无需加载全部路由。"""
    from .app import create_app as build_app

    return build_app(settings, local=local, sessions=sessions)
