import hmac
import ipaddress
import logging

from fastapi import Header, HTTPException, Request, status

from app.config import get_settings

logger = logging.getLogger("fandazi.security")

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "::1/128")
)


def _is_private(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in PRIVATE_NETWORKS)


def client_host(request: Request) -> str:
    """取真实客户端 IP。

    直连时就是 socket 对端地址；经 Caddy/nginx 反代时对端是内网代理地址，
    此时取 X-Forwarded-For 最左边那个公网地址。只有对端确实是内网地址才信任
    该头，避免外部请求自带伪造的 X-Forwarded-For 骗过限流和敏感接口的本机判断。
    """
    peer = request.client.host if request.client else ""
    if peer and not _is_private(peer):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    for candidate in forwarded.split(","):
        hop = candidate.strip()
        if hop and not _is_private(hop):
            return hop
    return peer or "unknown"


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
