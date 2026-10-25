"""配置校验测试。"""

from __future__ import annotations

import pytest


def test_default_real_booking_disabled(make_settings):
    s = make_settings()
    assert s.enable_real_booking is False


def test_base_url_strips_trailing_slash(make_settings):
    assert make_settings(base_url="https://example.com/").base_url == "https://example.com"


def test_base_url_requires_scheme(make_settings):
    with pytest.raises(Exception):
        make_settings(base_url="example.com")


def test_negative_timeout_rejected(make_settings):
    with pytest.raises(Exception):
        make_settings(request_timeout_s=-1)


def test_max_retry_nonnegative(make_settings):
    assert make_settings(max_retry=0).max_retry == 0
    with pytest.raises(Exception):
        make_settings(max_retry=-1)


def test_log_level_normalized(make_settings):
    assert make_settings(log_level="debug").log_level == "DEBUG"


def test_invalid_log_level_rejected(make_settings):
    with pytest.raises(Exception):
        make_settings(log_level="VERBOSE")


def test_secret_fields_redacted_in_repr(make_settings):
    s = make_settings(cookie="SECRETCOOKIE", password="SECRETPW")
    r = repr(s)
    assert "SECRETCOOKIE" not in r
    assert "SECRETPW" not in r


def test_query_param_defaults_reference(make_settings):
    # 默认值以浏览器正常查询为参考（regionid=16、pageSize=10）
    s = make_settings()
    assert s.regionid == 16
    assert s.page_size == 10


def test_http_headers_defaults(make_settings):
    s = make_settings()
    assert s.http_accept
    assert s.http_accept_language
    assert s.http_referer == ""  # Referer 默认不发送
    assert "haut-seat-helper" in s.http_user_agent


def test_regionid_page_size_positive(make_settings):
    with pytest.raises(Exception):
        make_settings(regionid=0)
    with pytest.raises(Exception):
        make_settings(page_size=-1)


def test_booking_config_default(make_settings):
    assert make_settings().booking_config == "config/booking.json"


def test_test_settings_never_read_a_dotenv(tmp_path, monkeypatch, make_settings):
    """测试夹具绝不读工作目录下的 ``.env``。

    仓库根目录那份 ``.env`` 是给**本机手工运行**用的，可能开着真实预约
    （``HAUT_ENABLE_REAL_BOOKING=true``）。若夹具改回默认构造，
    一次 ``pytest`` 就可能按真实开关初始化应用 —— 这条断言把它钉死。
    """
    (tmp_path / ".env").write_text("HAUT_ENABLE_REAL_BOOKING=true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert make_settings().enable_real_booking is False

    # 对照：默认构造（env_file=".env"）确实会读到它 —— 这正是夹具必须传
    # ``_env_file=None`` 的原因，不是多余的写法。
    from haut_seat.config import Settings

    assert Settings().enable_real_booking is True


def test_web_session_secure_defaults_to_off(make_settings):
    """Secure Cookie 默认关闭：默认部署（回环地址 + 可能没有 TLS）也能登录。"""
    assert make_settings().web_session_secure is False


def test_trigger_lead_defaults_to_off(make_settings):
    """提前启动量默认 0：不配置时调度器行为与从前一致（绝不悄悄改变生产行为）。"""
    assert make_settings().scheduler_trigger_lead_s == 0.0


def test_trigger_lead_accepts_zero_and_boundaries(make_settings):
    assert make_settings(scheduler_trigger_lead_s=0).scheduler_trigger_lead_s == 0.0
    assert make_settings(scheduler_trigger_lead_s=60).scheduler_trigger_lead_s == 60.0
    assert make_settings(scheduler_trigger_lead_s=600).scheduler_trigger_lead_s == 600.0


def test_trigger_lead_rejects_negative_and_absurd_values(make_settings):
    """负数无意义；过长会让调度器在触发前长时间占着 data/book.lock。"""
    with pytest.raises(Exception):
        make_settings(scheduler_trigger_lead_s=-1)
    with pytest.raises(Exception):
        make_settings(scheduler_trigger_lead_s=3600)


def test_not_open_keywords_default_to_empty(make_settings):
    """默认一个识别词都不内置：服务器「未开放」的文案我们从未观察到，
    猜词会把永久性失败当成「稍后再试」—— 拿一整天去赌一个猜测。"""
    assert make_settings().not_open_keywords == ""


def test_not_open_keywords_bind_from_the_documented_env_var(monkeypatch, make_settings):
    monkeypatch.setenv("HAUT_NOT_OPEN_KEYWORDS", "尚未开放,未开放")
    assert make_settings().not_open_keywords == "尚未开放,未开放"


# ------------------------------------------------------------------ 凭据来源


def test_default_auth_source_is_auto():
    """默认 auto：装好快照就能自动取用，没装则照旧走 .env。

    这里读**类字段默认值**而不是 ``make_settings()``：autouse 夹具把
    ``HAUT_AUTH_SOURCE=env`` 钉在环境里（防止测试读到开发机上的真实快照），
    而 pydantic-settings 的优先级是 init kwargs > 环境变量 > .env > 默认值，
    所以从实例上观察不到默认值。
    """
    from haut_seat.config import Settings

    assert Settings.model_fields["auth_source"].default == "auto"


def test_auth_source_accepts_the_three_documented_values(make_settings):
    for value in ("auto", "browser", "env"):
        assert make_settings(auth_source=value).auth_source == value


def test_invalid_auth_source_rejected(make_settings):
    """拼错的取值必须直接报错，而不是静默退回某个默认分支。"""
    with pytest.raises(Exception):
        make_settings(auth_source="brower")


def test_auth_check_ttl_defaults_to_disabled(make_settings):
    """预检结果复用默认关闭：不配置时行为与从前逐字节一致。"""
    from haut_seat.config import Settings

    assert Settings.model_fields["auth_check_ttl_s"].default == 0.0
    # 用例里的显式传参仍然照常生效
    assert make_settings(auth_check_ttl_s=0).auth_check_ttl_s == 0.0


def test_auth_check_ttl_accepts_zero_and_positive(make_settings):
    assert make_settings(auth_check_ttl_s=0).auth_check_ttl_s == 0.0
    assert make_settings(auth_check_ttl_s=120).auth_check_ttl_s == 120.0


def test_auth_check_ttl_rejects_negative(make_settings):
    with pytest.raises(Exception):
        make_settings(auth_check_ttl_s=-1)


def test_auth_login_timeout_must_be_positive(make_settings):
    with pytest.raises(Exception):
        make_settings(auth_login_timeout_s=0)


def test_auth_paths_default_under_data_auth():
    """默认路径都在 data/auth 下 —— 该目录被 .gitignore 排除，不会被提交。

    同样读类字段默认值（理由见 ``test_default_auth_source_is_auto``）。
    """
    from haut_seat.config import Settings

    fields = Settings.model_fields
    assert fields["auth_dir"].default == "data/auth"
    assert fields["auth_snapshot_file"].default.startswith("data/auth/")
    assert fields["auth_profile_dir"].default.startswith("data/auth/")


def test_auth_browser_channel_defaults_to_system_edge():
    """默认复用系统已装的 Edge：不执行 playwright install、不下载浏览器。"""
    from haut_seat.config import Settings

    assert Settings.model_fields["auth_browser_channel"].default == "msedge"


# ------------------------------------------------------------------ 环境变量名与文档必须一致
#
# 这里是踩过一次**真实**的坑才补上的：多用户主密钥的文档、错误消息、注释统统写
# ``HAUT_CRED_KEY``，而字段叫 ``auth_cred_key`` ⇒ pydantic 按 ``HAUT_`` + 字段名
# 推导出来的是 ``HAUT_AUTH_CRED_KEY``，于是文档里那个名字**被静默忽略**：
# 操作者以为配上了主密钥，程序却悄悄用了文件里那把（这正是"不得静默使用错误的
# 密钥"那条约束要防的事）。下面这组用例把「文档里写的键名真的能生效」钉成断言，
# 以后再出现同类错位会在 CI 里直接失败，而不是等到某天解不开密文才发现。

def _recognized_env_names() -> set[str]:
    """``Settings`` 真正认得的所有环境变量名（字段机械名 + 显式别名）。"""
    from pydantic import AliasChoices

    from haut_seat.config import Settings

    names = {f"HAUT_{name.upper()}" for name in Settings.model_fields}
    for field in Settings.model_fields.values():
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            names |= {str(choice) for choice in alias.choices}
        elif alias is not None:
            names.add(str(alias))
    return names


#: 文档里提到、但**故意**不由 Settings 实现的键名，必须在这里逐条列出并写清原因
#: （例如"某个第三方变量我们不支持"）。目前是空的。
DOC_ONLY_ENV_NAMES: set[str] = set()


def test_every_documented_env_var_is_actually_bound():
    """README / .env.example / 部署文档里出现的每个 ``HAUT_*`` 键名都必须真的能生效。"""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    documented: set[str] = set()
    for rel in ("README.md", ".env.example", "docs/web-deploy.md", "docs/auth-recon.md"):
        path = root / rel
        if path.exists():
            documented |= set(re.findall(r"\bHAUT_[A-Z0-9_]+\b", path.read_text(encoding="utf-8")))
    assert documented, "文档里一个 HAUT_* 都没读到，说明这份用例的路径已经失效"
    unknown = documented - _recognized_env_names() - DOC_ONLY_ENV_NAMES
    assert unknown == set(), (
        "这些键名写在文档里，但 Settings 根本不认（会被静默忽略），"
        f"请给字段加 validation_alias 或改文档：{sorted(unknown)}"
    )


CANONICAL_MASTER_KEY_VAR = "HAUT_AUTH_CRED_KEY"
ALIAS_MASTER_KEY_VAR = "HAUT_CRED_KEY"
MASTER_KEY_SAMPLE = "0123456789abcdef0123456789abcdef"  # 32 字节原文写法


def test_master_key_binds_from_its_canonical_env_var(monkeypatch, make_settings):
    monkeypatch.setenv(CANONICAL_MASTER_KEY_VAR, MASTER_KEY_SAMPLE)
    got = make_settings().auth_cred_key
    assert got is not None and got.get_secret_value() == MASTER_KEY_SAMPLE


def test_master_key_also_binds_from_the_short_alias(monkeypatch, make_settings):
    """``HAUT_CRED_KEY`` 是早期文档用过的名字，一并接受 —— 写它同样生效。"""
    monkeypatch.setenv(ALIAS_MASTER_KEY_VAR, MASTER_KEY_SAMPLE)
    got = make_settings().auth_cred_key
    assert got is not None and got.get_secret_value() == MASTER_KEY_SAMPLE


def test_master_key_prefers_the_canonical_name_when_both_are_set(monkeypatch, make_settings):
    """两个都设时以 ``HAUT_AUTH_CRED_KEY`` 为准（别名列表按序取第一个命中）。

    这条顺序必须被钉住：它决定"我到底用了哪把密钥"，随机或随版本变化都不可接受。
    """
    monkeypatch.setenv(ALIAS_MASTER_KEY_VAR, "short-one-short-one")
    monkeypatch.setenv(CANONICAL_MASTER_KEY_VAR, MASTER_KEY_SAMPLE)
    got = make_settings().auth_cred_key
    assert got is not None and got.get_secret_value() == MASTER_KEY_SAMPLE


def test_master_key_is_unset_when_no_env_var_is_set(make_settings):
    """都没配 ⇒ ``None``（此时主密钥来自 ``data/auth/master.key``），不是空串。"""
    assert make_settings().auth_cred_key is None


def test_master_key_can_still_be_passed_by_field_name(make_settings):
    """按字段名传参必须继续有效。

    给字段加 ``validation_alias`` 后，pydantic 默认**不再**接受字段名 ——
    那会让 ``Settings(auth_cred_key=...)`` 静默变成 ``None``。所以
    ``SettingsConfigDict`` 里的 ``populate_by_name=True`` 不是可选项。
    """
    settings = make_settings(auth_cred_key="by-field-name-1234")
    assert settings.auth_cred_key is not None
    assert settings.auth_cred_key.get_secret_value() == "by-field-name-1234"
