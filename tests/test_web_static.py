"""前端资源与 CSP 的一致性检查。

这个模块来自一次自查：模板里同时存在

1. 一条严格到 ``script-src 'self'`` 的 CSP，和
2. ``<script src="https://cdn.jsdelivr.net/.../bootstrap.bundle.min.js">``

两者是矛盾的 —— 浏览器会拦掉那个外部脚本（控制台刷 CSP 报错），
于是 ``data-bs-toggle="collapse"`` 这种导航折叠行为在**手机上完全失效**
（导航菜单打不开）。而手机正是本面板的主要使用场景。

修复方式是「不依赖任何外部脚本」：去掉那个永远跑不起来的外链，
把用到的两处 Bootstrap JS 行为改用本地 ``app.js`` 原生实现。
本模块就是防止这类矛盾再被引入。
"""

from __future__ import annotations

import re
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "haut_seat" / "web"
TEMPLATES = WEB_DIR / "templates"
STATIC = WEB_DIR / "static"
BOOTSTRAP_CSS = (
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
)

# 行内事件处理器 / javascript: URL 在没有 'unsafe-inline' 的 CSP 下同样不执行
_INLINE_HANDLER = re.compile(
    r"\son(click|change|submit|load|error|input|focus|blur|keydown|mouseover)\s*=",
    re.IGNORECASE,
)
_EXTERNAL_SRC = re.compile(r"""<script[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_EXTERNAL_ASSET = re.compile(r"""(?:src|href)\s*=\s*["'](https?://[^"']+|//[^"']+)["']""")


def templates() -> list[Path]:
    return sorted(TEMPLATES.glob("*.html"))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def js_code_only(text: str) -> str:
    """去掉注释，只留可执行代码：注释里提到 localStorage 不算数。"""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


def test_templates_reference_no_external_scripts():
    offenders = []
    for path in templates():
        for match in _EXTERNAL_SRC.finditer(read(path)):
            url = match.group(1)
            if url.startswith(("http://", "https://", "//")):
                offenders.append(f"{path.name}: {url}")
    assert offenders == [], (
        f"CSP 是 script-src 'self'，这些外部脚本会被浏览器拦掉：{offenders}"
    )


def test_templates_use_no_inline_event_handlers():
    offenders = [
        path.name for path in templates() if _INLINE_HANDLER.search(read(path))
    ]
    assert offenders == [], (
        f"CSP 没有放行 'unsafe-inline'，行内事件处理器不会执行：{offenders}"
    )


def test_templates_use_no_javascript_urls():
    offenders = [
        path.name for path in templates() if "javascript:" in read(path).lower()
    ]
    assert offenders == [], f"模板里出现了 javascript: URL：{offenders}"


def test_the_bootstrap_stylesheet_is_the_only_external_asset():
    """只有样式走 CDN；脚本一律本地。多出任何一条外链都要在这里显式确认。"""
    urls = set()
    for path in templates():
        urls |= set(_EXTERNAL_ASSET.findall(read(path)))
    assert urls == {BOOTSTRAP_CSS}, (
        "外部资源集合变了：新增的外部依赖需要同步检查 CSP，"
        f"并确认它不会执行脚本。实际：{sorted(urls)}"
    )


def test_the_base_template_loads_local_app_js():
    text = read(TEMPLATES / "base.html")
    assert "path='app.js'" in text


def test_navbar_toggler_targets_an_element_that_exists():
    text = read(TEMPLATES / "base.html")
    match = re.search(r'data-bs-target="#([^"]+)"', text)
    assert match, "导航折叠按钮的 data-bs-target 不见了"
    target = match.group(1)
    assert f'id="{target}"' in text, f"折叠目标 #{target} 在模板里不存在"


# ------------------------------------------------------------------ 本地脚本内容


def test_app_js_implements_the_bootstrap_behaviours_the_templates_use():
    """Bootstrap 的 JS 不加载，那么页面用到的那两处行为必须自己实现。"""
    js = read(STATIC / "app.js")
    assert 'data-bs-toggle="collapse"' in js
    assert 'data-bs-dismiss="alert"' in js
    assert "classList.toggle('show')" in js


def test_app_js_never_touches_storage_or_credentials():
    """需求：不在 localStorage 里保存管理员凭据或图书馆 token。"""
    js = js_code_only(read(STATIC / "app.js"))
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "document.cookie",
        "XMLHttpRequest",
        "fetch(",
        "Authorization",
        "token",
    ):
        assert forbidden not in js, f"app.js 的可执行代码里不应该出现 {forbidden!r}"


def test_app_css_defines_the_mobile_collapse_rule():
    """不依赖 CDN 的 CSS 也要让折叠在手机上生效（断点与 navbar-expand-lg 一致）。"""
    css = read(STATIC / "app.css")
    assert ".collapse:not(.show)" in css
    assert "991.98px" in css


def test_templates_do_not_reference_remote_images_or_fonts():
    for path in templates():
        text = read(path)
        assert not re.search(r"@import\s+url\(\s*['\"]?https?://", text), path.name


# ------------------------------------------------------------------ 服务端一致性


def test_csp_allows_only_same_origin_scripts(admin_client):
    _app, client = admin_client
    resp = client.get("/admin")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    script_src = next(
        (part.strip() for part in csp.split(";") if part.strip().startswith("script-src")),
        "",
    )
    assert script_src == "script-src 'self'", (
        f"脚本来源策略被放宽了：{script_src!r}；"
        "如果确实要引入外部脚本，必须同时更新本模块的检查与文档"
    )


def test_local_static_assets_are_served_with_the_right_types(make_client):
    _app, client = make_client()
    for name, expected in (("app.js", "javascript"), ("app.css", "css")):
        resp = client.get(f"/static/{name}")
        assert resp.status_code == 200, name
        assert expected in resp.headers["content-type"], name


def test_static_assets_cannot_escape_the_static_directory(make_client):
    """静态目录不能变成任意文件读取入口。"""
    _app, client = make_client()
    for path in (
        "/static/../app.py",
        "/static/..%2fapp.py",
        "/static/%2e%2e/app.py",
        "/static/../../.env",
        "/static/....//app.py",
    ):
        resp = client.get(path)
        assert resp.status_code in (307, 308, 400, 404), f"{path} → {resp.status_code}"