"""凭据来源解析与快照存储测试。

全部离线、零网络：本模块只碰文件系统与纯逻辑，**不构造任何 HTTP 客户端**。
（因此这里的守卫不是 "必须含 MockTransport"——那是给会发请求的模块用的——
而是"本模块不 import 也不构造任何真实客户端，且不含真实学校域名"。）
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from haut_seat.services import auth_store
from haut_seat.services.auth_store import (
    Credentials,
    CredentialsUnavailableError,
    Snapshot,
    fingerprint,
    load_snapshot,
    resolve_credentials,
    save_snapshot,
    source_label,
)

# 哨兵串：形状像真令牌，但显然是测试专用值。任何日志/异常/页面都不该出现它。
SENTINEL = "SENTINELTOKEN-._0001-abcdefghijklmnop"


@pytest.fixture
def auth_settings(make_settings, tmp_path):
    """把快照/浏览器目录都指向临时目录的配置工厂。"""

    def _make(**overrides):
        base = {
            "auth_dir": str(tmp_path / "auth"),
            "auth_snapshot_file": str(tmp_path / "auth" / "credentials.json"),
            "auth_profile_dir": str(tmp_path / "auth" / "browser-profile"),
        }
        base.update(overrides)
        return make_settings(**base)

    return _make


def write_snapshot_file(path: Path, **fields) -> None:
    """直接落一个快照文件（绕过 save_snapshot，用于构造各种异常形态）。"""
    payload = {
        "version": 1,
        "source": "browser-login",
        "base_url": "",
        "captured_at": "2026-10-18 12:00:00",
        "cookie": f"Admin-Token={SENTINEL}",
        "authorization": f"Bearer {SENTINEL}",
    }
    payload.update(fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 往返与落盘


def test_save_then_load_roundtrip(auth_settings):
    settings = auth_settings()
    saved_path = save_snapshot(
        settings, cookie=f"Admin-Token={SENTINEL}", authorization=f"Bearer {SENTINEL}"
    )
    assert saved_path == settings.auth_snapshot_file

    snap = load_snapshot(settings)
    assert snap is not None
    assert snap.cookie == f"Admin-Token={SENTINEL}"
    assert snap.authorization == f"Bearer {SENTINEL}"
    assert snap.source == "browser-login"
    assert snap.captured_at  # 由 save_snapshot 写入当前时间


def test_saved_snapshot_records_the_configured_base_url(auth_settings):
    """写上 base_url 是为了防止把 A 环境的会话发到 B 环境。"""
    settings = auth_settings()
    save_snapshot(settings, cookie="c", authorization="a")
    payload = json.loads(Path(settings.auth_snapshot_file).read_text(encoding="utf-8"))
    assert payload["base_url"] == settings.base_url


def test_save_snapshot_rejects_empty_values(auth_settings):
    """绝不写出一份"看起来有、其实空"的快照。"""
    settings = auth_settings()
    with pytest.raises(ValueError):
        save_snapshot(settings, cookie="", authorization="Bearer x")
    with pytest.raises(ValueError):
        save_snapshot(settings, cookie="Admin-Token=x", authorization="")
    assert not Path(settings.auth_snapshot_file).exists()


def test_save_snapshot_leaves_no_temp_file_behind(auth_settings):
    settings = auth_settings()
    save_snapshot(settings, cookie="c", authorization="a")
    leftover = [p.name for p in Path(settings.auth_snapshot_file).parent.iterdir()]
    assert leftover == ["credentials.json"]


@pytest.mark.skipif(os.name == "nt", reason="Windows 无 POSIX 权限位，chmod 不产生『仅本人可读』")
def test_saved_snapshot_is_owner_only_on_posix(auth_settings):
    settings = auth_settings()
    save_snapshot(settings, cookie="c", authorization="a")
    mode = Path(settings.auth_snapshot_file).stat().st_mode & 0o777
    assert mode == 0o600


def test_snapshot_file_is_never_readable_from_logs(auth_settings, caplog):
    """落盘日志只记路径与时间，绝不记凭据值。"""
    import logging

    settings = auth_settings()
    with caplog.at_level(logging.DEBUG, logger="haut_seat.auth"):
        save_snapshot(settings, cookie=f"Admin-Token={SENTINEL}", authorization=f"Bearer {SENTINEL}")
        load_snapshot(settings)
    assert SENTINEL not in caplog.text
    assert "Admin-Token=" not in caplog.text


# ---------------------------------------------------------------- 容错读取


def test_load_snapshot_missing_file_returns_none(auth_settings):
    assert load_snapshot(auth_settings()) is None


def test_broken_snapshot_returns_none_not_raise(auth_settings):
    """半写/断电导致的坏 JSON 只能返回 None —— 调用方据此回退或报错，不能崩。"""
    settings = auth_settings()
    Path(settings.auth_snapshot_file).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.auth_snapshot_file).write_text("{not json", encoding="utf-8")
    assert load_snapshot(settings) is None


def test_snapshot_version_unsupported_is_rejected(auth_settings):
    settings = auth_settings()
    write_snapshot_file(Path(settings.auth_snapshot_file), version=99)
    assert load_snapshot(settings) is None


@pytest.mark.parametrize(
    "fields",
    [
        {"cookie": ""},
        {"authorization": ""},
        {"cookie": None},
        {"authorization": 12345},
    ],
)
def test_snapshot_missing_or_invalid_fields_is_rejected(auth_settings, fields):
    settings = auth_settings()
    write_snapshot_file(Path(settings.auth_snapshot_file), **fields)
    assert load_snapshot(settings) is None


def test_snapshot_base_url_mismatch_is_rejected(auth_settings):
    """A 环境的会话绝不发给 B 环境。"""
    settings = auth_settings()
    write_snapshot_file(Path(settings.auth_snapshot_file), base_url="https://other.invalid")
    assert load_snapshot(settings) is None


def test_snapshot_non_object_json_is_rejected(auth_settings):
    settings = auth_settings()
    Path(settings.auth_snapshot_file).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.auth_snapshot_file).write_text("[1, 2, 3]", encoding="utf-8")
    assert load_snapshot(settings) is None


# ---------------------------------------------------------------- 优先级：env 模式


def test_env_mode_uses_env_values(auth_settings):
    settings = auth_settings(auth_source="env", cookie="COOKIE", authorization="AUTH")
    creds = resolve_credentials(settings)
    assert creds.source == "env"
    assert creds.cookie == "COOKIE"
    assert creds.authorization == "AUTH"


def test_env_mode_ignores_snapshot_entirely(auth_settings):
    """env 模式连快照文件都不该读 —— 这是"既有部署零变化"的核心保证。"""
    settings = auth_settings(auth_source="env", cookie="ENVCOOKIE", authorization="ENVAUTH")
    write_snapshot_file(Path(settings.auth_snapshot_file))
    creds = resolve_credentials(settings)
    assert creds.source == "env"
    assert creds.cookie == "ENVCOOKIE"
    assert creds.authorization == "ENVAUTH"


def test_env_mode_without_credentials_is_none(auth_settings):
    creds = resolve_credentials(auth_settings(auth_source="env"))
    assert creds.source == "none"
    assert creds.cookie is None and creds.authorization is None
    assert creds.has_authorization is False
    assert creds.fingerprint() == ""


def test_none_source_label_is_not_confused_with_a_credential(auth_settings):
    assert resolve_credentials(auth_settings(auth_source="env")).source_label == "未配置"


# ---------------------------------------------------------------- 优先级：browser 模式


def test_browser_mode_uses_snapshot(auth_settings):
    settings = auth_settings(auth_source="browser")
    save_snapshot(settings, cookie=f"Admin-Token={SENTINEL}", authorization=f"Bearer {SENTINEL}")
    creds = resolve_credentials(settings)
    assert creds.source == "snapshot"
    assert creds.captured_at
    assert creds.authorization == f"Bearer {SENTINEL}"


def test_browser_mode_missing_snapshot_raises_and_never_falls_back(auth_settings):
    """关键安全行为：browser 模式缺快照必须**明确报错**，绝不静默用 .env 的旧凭据。"""
    settings = auth_settings(auth_source="browser", cookie="STALECOOKIE", authorization="STALEAUTH")
    with pytest.raises(CredentialsUnavailableError) as excinfo:
        resolve_credentials(settings)
    message = str(excinfo.value)
    assert "auth-login" in message           # 给出补救方式
    assert "STALECOOKIE" not in message      # 绝不回显旧凭据
    assert "STALEAUTH" not in message


def test_browser_mode_with_broken_snapshot_also_raises(auth_settings):
    settings = auth_settings(auth_source="browser", cookie="STALECOOKIE")
    Path(settings.auth_snapshot_file).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.auth_snapshot_file).write_text("{broken", encoding="utf-8")
    with pytest.raises(CredentialsUnavailableError):
        resolve_credentials(settings)


# ---------------------------------------------------------------- 优先级：auto 模式


def test_auto_falls_back_to_env_when_snapshot_absent(auth_settings, monkeypatch):
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: None)
    settings = auth_settings(auth_source="auto", cookie="ENVCOOKIE", authorization="ENVAUTH")
    creds = resolve_credentials(settings)
    assert creds.source == "env"
    assert creds.cookie == "ENVCOOKIE"


def test_auto_prefers_snapshot_over_env(auth_settings, monkeypatch):
    """快照更新鲜时压过 .env —— 这是"避免旧 .env 覆盖新获取的凭据"。"""
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: 1.0)
    settings = auth_settings(auth_source="auto", cookie="OLDCOOKIE", authorization="OLDAUTH")
    save_snapshot(settings, cookie=f"Admin-Token={SENTINEL}", authorization=f"Bearer {SENTINEL}")
    creds = resolve_credentials(settings)
    assert creds.source == "snapshot"
    assert creds.cookie == f"Admin-Token={SENTINEL}"


def test_auto_prefers_env_when_env_is_newer(auth_settings, monkeypatch):
    """用户在 .env 里刚粘贴了新值 → 用他刚写的，而不是旧快照。"""
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: 9_999_999_999.0)
    settings = auth_settings(auth_source="auto", cookie="FRESHCOOKIE", authorization="FRESHAUTH")
    save_snapshot(settings, cookie=f"Admin-Token={SENTINEL}", authorization=f"Bearer {SENTINEL}")
    creds = resolve_credentials(settings)
    assert creds.source == "env"
    assert creds.cookie == "FRESHCOOKIE"


def test_auto_with_no_credentials_at_all_is_none(auth_settings, monkeypatch):
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: None)
    creds = resolve_credentials(auth_settings(auth_source="auto"))
    assert creds.source == "none"


def test_auto_uses_snapshot_when_env_has_no_credentials(auth_settings, monkeypatch):
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: 9_999_999_999.0)
    settings = auth_settings(auth_source="auto")
    save_snapshot(settings, cookie=f"Admin-Token={SENTINEL}", authorization=f"Bearer {SENTINEL}")
    assert resolve_credentials(settings).source == "snapshot"


def test_auto_falls_back_to_snapshot_when_mtime_is_unavailable(auth_settings, monkeypatch):
    """读不到 .env 的 mtime 时以快照优先（无从比较，就顺从功能本意）。"""
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: None)
    settings = auth_settings(auth_source="auto", cookie="ENVCOOKIE", authorization="ENVAUTH")
    save_snapshot(settings, cookie=f"Admin-Token={SENTINEL}", authorization=f"Bearer {SENTINEL}")
    assert resolve_credentials(settings).source == "snapshot"


def test_auto_prefers_snapshot_when_captured_at_is_unparseable(auth_settings, monkeypatch):
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: 9_999_999_999.0)
    settings = auth_settings(auth_source="auto", cookie="ENVCOOKIE", authorization="ENVAUTH")
    write_snapshot_file(Path(settings.auth_snapshot_file), captured_at="not-a-time")
    assert resolve_credentials(settings).source == "snapshot"


# ---------------------------------------------------------------- 重启后仍可用


def test_snapshot_available_to_fresh_settings(auth_settings, monkeypatch):
    """重启即重新构造 Settings —— 快照必须仍然可用（不依赖进程内状态）。"""
    monkeypatch.setattr(auth_store, "env_file_mtime", lambda settings: None)
    save_snapshot(
        auth_settings(), cookie=f"Admin-Token={SENTINEL}", authorization=f"Bearer {SENTINEL}"
    )
    fresh = auth_settings(auth_source="auto")
    creds = resolve_credentials(fresh)
    assert creds.source == "snapshot"
    assert creds.authorization == f"Bearer {SENTINEL}"


def test_env_file_mtime_reads_the_configured_env_file(make_settings, tmp_path, monkeypatch):
    """auto 的"较新"判据来自真实文件 mtime —— 用真实文件验证一次。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("HAUT_AUTH_SOURCE=env\n", encoding="utf-8")
    settings = make_settings()
    assert auth_store.env_file_mtime(settings) == pytest.approx(
        (tmp_path / ".env").stat().st_mtime
    )


def test_env_file_mtime_is_none_when_file_absent(make_settings, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert auth_store.env_file_mtime(make_settings()) is None


# ---------------------------------------------------------------- 脱敏与指纹


def test_credentials_repr_redacts():
    creds = Credentials(f"Admin-Token={SENTINEL}", f"Bearer {SENTINEL}", "snapshot", "t")
    text = repr(creds)
    assert SENTINEL not in text
    assert "Admin-Token=" not in text
    assert "snapshot" in text          # 来源仍然可见，便于排查
    assert creds.fingerprint() in text  # 指纹可见，用于指认"用的是哪一份"


def test_snapshot_repr_redacts():
    snap = Snapshot(f"Admin-Token={SENTINEL}", f"Bearer {SENTINEL}", "2026-10-18 12:00:00")
    text = repr(snap)
    assert SENTINEL not in text
    assert "Admin-Token=" not in text


def test_fingerprint_is_stable_and_distinct():
    assert fingerprint(f"Bearer {SENTINEL}") == fingerprint(f"Bearer {SENTINEL}")
    assert fingerprint("Bearer a") != fingerprint("Bearer b")
    assert fingerprint(None) == "" and fingerprint("") == ""
    assert len(fingerprint("Bearer x")) == 12


def test_source_labels_cover_the_three_sources():
    assert source_label("snapshot") == "浏览器会话快照"
    assert source_label("env") == ".env 手动配置"
    assert source_label("none") == "未配置"
    assert source_label("weird") == "weird"  # 未知值原样返回，不抛


def test_snapshot_captured_ts_is_none_for_unparseable_text():
    assert Snapshot("c", "a", "nope").captured_ts is None


# ---------------------------------------------------------------- 守卫


def test_this_module_never_builds_an_http_client():
    """守卫：本模块只测存储与解析，不碰网络，也不出现真实学校域名。"""
    text = Path(__file__).read_text(encoding="utf-8")
    # 按片段拼装，避免断言文本自身造成自匹配
    real_host = "wslib" + ".haut" + ".edu.cn"
    client_cls = "Seat" + "HttpClient"
    factory = "build_http" + "_client"
    assert real_host not in text
    assert client_cls not in text
    assert factory not in text