import hmac
import logging

from fastapi import Header, HTTPException, Request, status

from app.config import get_settings

logger = logging.getLogger("fandazi.security")

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _reject(request: Request, detail: str) -> HTTPException:
    logger.warning("auth_rejected path=%s client=%s", request.url.path, client_host(request))
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _is_production(settings) -> bool:
    return bool(getattr(settings, "is_production", False))


def require_token(
    request: Request, x_api_token: str = Header(default="", alias="X-API-Token")
) -> None:
    settings = get_settings()
    expected = settings.api_token
    if not expected:
        # 生产环境理论上在启动时就被 validate_for_startup 拦住了，这里再兜一层。
        if _is_production(settings):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="服务未配置 API_TOKEN，拒绝访问",
            )
        return
    if not hmac.compare_digest(x_api_token, expected):
        raise _reject(request, "未授权")


def require_sensitive_access(
    request: Request, x_api_token: str = Header(default="", alias="X-API-Token")
) -> None:
    """导出、清理、删除接口的保护。

    配了 API_TOKEN 就校验；没配时只允许本机访问，避免开发机把服务开到
    0.0.0.0 后，局域网里任何人都能导出或清空数据。
    """
    settings = get_settings()
    if settings.api_token:
        if not hmac.compare_digest(x_api_token, settings.api_token):
            raise _reject(request, "未授权")
        return
    if _is_production(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="服务未配置 API_TOKEN，拒绝执行敏感操作",
        )
    if client_host(request) not in LOOPBACK_HOSTS:
        logger.warning(
            "sensitive_access_denied path=%s client=%s", request.url.path, client_host(request)
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="未配置 API_TOKEN 时，导出/清理/删除只允许本机访问",
        )
