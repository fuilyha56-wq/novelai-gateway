"""
内容重写模块。

负责在返回给客户端的 HTML / JS 中注入脚本或替换 URL，
使前端的 API 请求自动指向本地网关而非 NovelAI 官方。
"""

# 需要被劫持的官方域名
_OFFICIAL_ORIGINS = (
    "https://api.novelai.net",
    "https://image.novelai.net",
)


def build_hijack_script(local_api_prefix: str, shared_token: str = "") -> str:
    """生成注入到 HTML <head> 中的 fetch 劫持脚本，支持自动写入共享 Token。"""
    import json
    token_val = "null"
    if shared_token:
        token_str = shared_token.strip().strip("'\"")
        try:
            parsed = json.loads(token_str)
            token_val = json.dumps(parsed)
        except Exception:
            token_val = json.dumps(token_str)

    has_shared_token_js = "true" if shared_token else "false"

    return f"""
(function() {{
    const LOCAL = '{local_api_prefix}';
    const SHARED_TOKEN = {token_val};

    // 只有当前页面是由合法的 Cookie 认证过且加载出劫持脚本时，才会自动写入该共享登录 Token
    if (SHARED_TOKEN) {{
        let sharedAuthToken = "";
        let tokenStr = "";
        try {{
            const sharedObj = typeof SHARED_TOKEN === 'string' ? JSON.parse(SHARED_TOKEN) : SHARED_TOKEN;
            sharedAuthToken = sharedObj.auth_token;
            tokenStr = JSON.stringify(sharedObj);
        }} catch(e) {{
            sharedAuthToken = SHARED_TOKEN;
            tokenStr = SHARED_TOKEN;
        }}

        const storedStr = localStorage.getItem('session');
        let currentAuthToken = "";
        try {{
            if (storedStr) {{
                const storedObj = JSON.parse(storedStr);
                currentAuthToken = storedObj.auth_token || storedStr;
            }}
        }} catch(e) {{
            currentAuthToken = storedStr || "";
        }}

        if (sharedAuthToken && currentAuthToken !== sharedAuthToken) {{
            localStorage.setItem('session', tokenStr);
            window.location.reload();
            return;
        }}
    }}

    const rewrite = (url) => {{
        if (typeof url !== 'string') return url;
        const regex = new RegExp("https://(api|image)\\\\.novelai\\\\.net", "g");
        return url.replace(regex, LOCAL);
    }};
    const hijack = () => {{
        if (window.fetch && !window.fetch.__gw) {{
            const orig = window.fetch;
            window.fetch = function(input, init) {{
                if (typeof input === 'string') input = rewrite(input);
                else if (input instanceof Request) input = new Request(rewrite(input.url), input);
                return orig.call(this, input, init);
            }};
            window.fetch.__gw = true;
        }}
    }};
    hijack();
    setInterval(hijack, 500);
}})();
"""


def rewrite_html(html_bytes: bytes, local_api_prefix: str, shared_token: str = "") -> bytes:
    """在 HTML 中注入劫持脚本。"""
    try:
        script_code = build_hijack_script(local_api_prefix, shared_token)
        script_tag = f"<script>{script_code}</script>".encode("utf-8")
        
        # 优先插入到 <head> 之后
        if b"<head>" in html_bytes:
            return html_bytes.replace(b"<head>", b"<head>" + script_tag, 1)
        elif b"<head " in html_bytes:
            idx = html_bytes.find(b"<head")
            end_idx = html_bytes.find(b">", idx)
            if idx != -1 and end_idx != -1:
                return html_bytes[:end_idx+1] + script_tag + html_bytes[end_idx+1:]
        elif b"<html>" in html_bytes:
            return html_bytes.replace(b"<html>", b"<html>" + script_tag, 1)
        
        return script_tag + html_bytes
    except Exception as e:
        import logging
        logging.getLogger("gateway").error(f"❌ [Rewrite HTML Error] {e}")
        return html_bytes


def rewrite_js(js_text: str, local_api_prefix: str) -> str:
    """将 JS 源码中的官方 API 域名替换为本地网关地址。"""
    result = js_text
    for origin in _OFFICIAL_ORIGINS:
        result = result.replace(origin, local_api_prefix)
    return result
