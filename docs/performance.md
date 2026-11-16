# 线上访问慢的排查与优化

本文记录 2026-11-16 对 `https://www.tz369.xyz/` 的实测结论和后续排查方法。

## 已观察到的现象

- 首页 HTML 约 279 KB，Cloudflare 返回 `cf-cache-status: DYNAMIC`，没有边缘缓存。
- 首页首字节约 0.7–2.5 秒，但 HTML 传输完成需要 9–30 秒，速度约 9–19 KB/s。
- `/api/v1/auth/me` 在无 Cookie 时本应立刻返回 401，实测出现过 8–89 秒；`/auth/config` 也出现过 12–20 秒超时。
- 这些接口本身不查数据库或只做设置读取，说明长尾主要来自 Cloudflare→源站链路或源站出口，而不是 SQL。

## 代码侧已经做的优化

- 首页 HTML 在进程启动时缓存并预生成 gzip，带 `ETag`、`Vary: Accept-Encoding` 和短缓存头；重复访问返回 304。
- 前端不再等待 `/auth/me` 才初始化页面，慢接口不会再冻结标签和按钮。
- 启动时的 `/meals` 请求在 5 秒内复用同一个 promise，避免同一周数据重复请求。
- `/api/` 请求超过 1 秒会写一条 `slow_request` 日志，并返回 `Server-Timing` 头。

## 先判断：慢在 Cloudflare 还是源站

在服务器或本地执行，把 `<服务器公网IP>` 换成真实 IP：

```bash
# 经过 Cloudflare
curl -sS -o /dev/null \
  -w 'cf status=%{http_code} ttfb=%{time_starttransfer}s total=%{time_total}s bytes=%{size_download} speed=%{speed_download}B/s\n' \
  https://tz369.xyz/

# 绕过 Cloudflare，直连源站（SNI/Host 仍是域名）
curl --resolve tz369.xyz:443:<服务器公网IP> -sS -o /dev/null \
  -w 'origin status=%{http_code} ttfb=%{time_starttransfer}s total=%{time_total}s bytes=%{size_download} speed=%{speed_download}B/s\n' \
  https://tz369.xyz/
```

各跑 3–5 次，比较 `time_starttransfer`、`time_total` 和 `speed_download`。

- **直连明显更快**：瓶颈在 Cloudflare 跨境链路。代码压缩只能缓解，不能根治。
- **直连也慢**：检查服务器出口带宽、CPU、内存和 Caddy/应用日志。

## 如果瓶颈在 Cloudflare 链路

按优先级从高到低：

1. **DNS 直连 Caddy（灰云）**：把域名 A 记录指回服务器公网 IP，关闭 Cloudflare 代理。中国大陆用户通常最快；代价是暴露源站 IP，需要在云防火墙限制端口并保留应用限流。
2. **国内 CDN**：腾讯云/阿里云 CDN + 已备案域名，适合有较多公网用户；需要 ICP 备案和 CDN 配置。
3. **Cloudflare 中国网络或 Argo**：保留 Cloudflare 防护，但通常需要付费/企业资格，按实际套餐评估。
4. **继续用 Cloudflare 免费代理**：至少加一条首页 Cache Rule，让边缘缓存 HTML 5 分钟，减少回源；但跨境传输本身的延迟仍在。

Cloudflare Cache Rule 参考：

- 匹配：URI Path equals `/`
- Cache eligibility：Eligible for cache
- Edge TTL：5 分钟
- Browser TTL：Respect origin

## 服务器侧检查

```bash
docker stats --no-stream
uptime
free -h
docker compose logs --tail=100 app
sudo journalctl -u caddy -n 100 --no-pager
```

同时看云厂商控制台的公网带宽/出方向流量。轻量服务器常见 3–6 Mbps 出口；如果实测只有几十 KB/s，更像是跨境丢包或链路拥塞，而不是带宽上限。

## 验证改动是否生效

```bash
# gzip 与压缩后体积
curl -sS -H 'Accept-Encoding: gzip' -D - -o /tmp/home.gz https://tz369.xyz/ | grep -iE 'content-encoding|etag|cache-control|vary'
wc -c /tmp/home.gz

# 304
ETAG=$(curl -sSI https://tz369.xyz/ | awk -F': ' 'tolower($1)=="etag"{print $2}' | tr -d '\r')
curl -sS -o /dev/null -w '%{http_code}\n' -H "If-None-Match: $ETAG" https://tz369.xyz/
```

部署后如果首页仍是旧版本，先强制刷新；Cloudflare 若按上面的 Cache Rule 缓存了首页，最多等待 5 分钟或在控制台清缓存。
