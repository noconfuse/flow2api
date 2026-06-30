"""Configuration management for Flow2API"""
try:
    import tomllib as toml_loader
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    import tomli as toml_loader
from pathlib import Path
from typing import Dict, Any, Optional

DEFAULT_YESCAPTCHA_TASK_TYPE = "RecaptchaV3TaskProxylessM1"
LEGACY_YESCAPTCHA_TASK_TYPE_ALIASES = {
    "RecaptchaV3Task": "RecaptchaV3EnterpriseTask",
}
YESCAPTCHA_TASK_TYPE_OPTIONS = {
    "RecaptchaV3EnterpriseTask": None,
    "RecaptchaV3TaskProxyless": None,
    "RecaptchaV3TaskProxylessM1": None,
    "RecaptchaV3TaskProxylessM1S7": 0.7,
    "RecaptchaV3TaskProxylessM1S9": 0.9,
}


def normalize_yescaptcha_task_type(task_type: Optional[str]) -> str:
    normalized = (task_type or "").strip()
    normalized = LEGACY_YESCAPTCHA_TASK_TYPE_ALIASES.get(normalized, normalized)
    if normalized in YESCAPTCHA_TASK_TYPE_OPTIONS:
        return normalized
    return DEFAULT_YESCAPTCHA_TASK_TYPE


def get_yescaptcha_min_score(task_type: Optional[str]) -> Optional[float]:
    return YESCAPTCHA_TASK_TYPE_OPTIONS.get(normalize_yescaptcha_task_type(task_type))


def yescaptcha_task_type_requires_proxy(task_type: Optional[str]) -> bool:
    normalized = normalize_yescaptcha_task_type(task_type)
    return normalized == "RecaptchaV3EnterpriseTask"


class Config:
    """Application configuration"""

    def __init__(self):
        self._config = self._load_config()
        self._admin_username: Optional[str] = None
        self._admin_password: Optional[str] = None

    def _load_config(self) -> Dict[str, Any]:
        """Load configuration from setting.toml, falling back to the example file."""
        config_dir = Path(__file__).parent.parent.parent / "config"
        config_path = config_dir / "setting.toml"
        if not config_path.exists():
            config_path = config_dir / "setting_example.toml"
        with open(config_path, "rb") as f:
            return toml_loader.load(f)

    def reload_config(self):
        """Reload configuration from file"""
        self._config = self._load_config()

    def get_raw_config(self) -> Dict[str, Any]:
        """Get raw configuration dictionary"""
        return self._config

    @property
    def admin_username(self) -> str:
        # If admin_username is set from database, use it; otherwise fall back to config file
        if self._admin_username is not None:
            return self._admin_username
        return self._config["global"]["admin_username"]

    @admin_username.setter
    def admin_username(self, value: str):
        self._admin_username = value
        self._config["global"]["admin_username"] = value

    def set_admin_username_from_db(self, username: str):
        """Set admin username from database"""
        self._admin_username = username

    # Flow2API specific properties
    @property
    def flow_labs_base_url(self) -> str:
        """Google Labs base URL for project management"""
        return self._config["flow"]["labs_base_url"]

    @property
    def flow_api_base_url(self) -> str:
        """Google AI Sandbox API base URL for generation"""
        return self._config["flow"]["api_base_url"]

    @property
    def flow_timeout(self) -> int:
        timeout = self._config.get("flow", {}).get("timeout", 120)
        try:
            return max(5, int(timeout))
        except Exception:
            return 120

    @property
    def flow_max_retries(self) -> int:
        retries = self._config.get("flow", {}).get("max_retries", 3)
        try:
            return max(1, int(retries))
        except Exception:
            return 3

    def set_flow_max_retries(self, retries: int):
        """Set flow max retries"""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(1, int(retries))
        except Exception:
            normalized = 3
        self._config["flow"]["max_retries"] = normalized

    @property
    def flow_image_request_timeout(self) -> int:
        """图片生成单次 HTTP 请求超时(秒)。"""
        default_timeout = min(self.flow_timeout, 40)
        timeout = self._config.get("flow", {}).get(
            "image_request_timeout",
            default_timeout
        )
        try:
            return max(5, int(timeout))
        except Exception:
            return self.flow_timeout

    @property
    def flow_image_timeout_retry_count(self) -> int:
        """图片生成遇到网络超时时的快速重试次数。"""
        retry_count = self._config.get("flow", {}).get("image_timeout_retry_count", 1)
        try:
            return max(0, min(3, int(retry_count)))
        except Exception:
            return 1

    @property
    def flow_image_timeout_retry_delay(self) -> float:
        """图片生成网络超时重试前等待秒数。"""
        delay = self._config.get("flow", {}).get("image_timeout_retry_delay", 0.8)
        try:
            return max(0.0, min(5.0, float(delay)))
        except Exception:
            return 0.8

    @property
    def flow_image_timeout_use_media_proxy_fallback(self) -> bool:
        """网络超时时是否切换媒体代理重试。"""
        return bool(
            self._config.get("flow", {}).get(
                "image_timeout_use_media_proxy_fallback",
                True
            )
        )

    @property
    def flow_image_prefer_media_proxy(self) -> bool:
        """图片生成是否优先走媒体代理链路。"""
        return bool(
            self._config.get("flow", {}).get(
                "image_prefer_media_proxy",
                False
            )
        )

    @property
    def flow_image_slot_wait_timeout(self) -> float:
        """图片硬并发槽位等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("image_slot_wait_timeout", 120)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 120.0

    @property
    def flow_image_launch_soft_limit(self) -> int:
        """图片生成前置发车软并发上限(0 表示关闭软整形，仅使用硬并发)。"""
        value = self._config.get("flow", {}).get("image_launch_soft_limit", 0)
        try:
            return max(0, min(200, int(value)))
        except Exception:
            return 0

    @property
    def flow_image_launch_wait_timeout(self) -> float:
        """图片前置发车软并发等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("image_launch_wait_timeout", 180)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 180.0

    @property
    def flow_image_launch_stagger_ms(self) -> int:
        """图片请求前置发车间隔(毫秒)，用于平滑同批突发。"""
        value = self._config.get("flow", {}).get("image_launch_stagger_ms", 0)
        try:
            return max(0, min(5000, int(value)))
        except Exception:
            return 0

    @property
    def flow_video_slot_wait_timeout(self) -> float:
        """视频硬并发槽位等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("video_slot_wait_timeout", 120)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 120.0

    @property
    def flow_video_launch_soft_limit(self) -> int:
        """视频生成前置发车软并发上限(0 表示关闭软整形，仅使用硬并发)。"""
        value = self._config.get("flow", {}).get("video_launch_soft_limit", 0)
        try:
            return max(0, min(200, int(value)))
        except Exception:
            return 0

    @property
    def flow_video_launch_wait_timeout(self) -> float:
        """视频前置发车软并发等待超时(秒)。"""
        timeout = self._config.get("flow", {}).get("video_launch_wait_timeout", 180)
        try:
            return max(1.0, min(600.0, float(timeout)))
        except Exception:
            return 180.0

    @property
    def flow_video_launch_stagger_ms(self) -> int:
        """视频请求前置发车间隔(毫秒)，用于平滑同批突发。"""
        value = self._config.get("flow", {}).get("video_launch_stagger_ms", 0)
        try:
            return max(0, min(5000, int(value)))
        except Exception:
            return 0

    @property
    def flow_video_frontend_event_mode(self) -> str:
        """视频前端事件发送时机: before/after/both/off。"""
        mode = str(
            self._config.get("flow", {}).get("video_frontend_event_mode", "before")
        ).strip().lower()
        if mode in {"before", "after", "both", "off"}:
            return mode
        return "before"

    def set_flow_video_frontend_event_mode(self, mode: str):
        """Set video frontend event mode."""
        if "flow" not in self._config:
            self._config["flow"] = {}
        normalized = str(mode or "before").strip().lower()
        if normalized not in {"before", "after", "both", "off"}:
            normalized = "before"
        self._config["flow"]["video_frontend_event_mode"] = normalized

    @property
    def poll_interval(self) -> float:
        return self._config["flow"]["poll_interval"]

    @property
    def max_poll_attempts(self) -> int:
        return self._config["flow"]["max_poll_attempts"]

    @property
    def exhausted_credit_threshold(self) -> int:
        """账号余额小于等于该值时视为已耗尽，不再参与调度。"""
        value = self._config.get("flow", {}).get("exhausted_credit_threshold", 0)
        try:
            return max(0, int(value))
        except Exception:
            return 0

    def set_exhausted_credit_threshold(self, value: int):
        """Set exhausted credit threshold."""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(0, int(value))
        except Exception:
            normalized = 0
        self._config["flow"]["exhausted_credit_threshold"] = normalized

    @property
    def low_credit_threshold(self) -> int:
        """账号余额小于等于该值时视为低余额，参与调度但降低优先级。"""
        value = self._config.get("flow", {}).get("low_credit_threshold", 100)
        try:
            return max(self.exhausted_credit_threshold, int(value))
        except Exception:
            return 100

    def set_low_credit_threshold(self, value: int):
        """Set low credit threshold."""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(self.exhausted_credit_threshold, int(value))
        except Exception:
            normalized = max(self.exhausted_credit_threshold, 100)
        self._config["flow"]["low_credit_threshold"] = normalized

    @property
    def active_token_credit_refresh_interval_seconds(self) -> int:
        """活跃账号余额自动刷新周期，0 表示关闭。"""
        value = self._config.get("flow", {}).get("active_token_credit_refresh_interval_seconds", 900)
        try:
            return max(0, int(value))
        except Exception:
            return 900

    def set_active_token_credit_refresh_interval_seconds(self, value: int):
        """Set active token credit refresh interval."""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(0, int(value))
        except Exception:
            normalized = 900
        self._config["flow"]["active_token_credit_refresh_interval_seconds"] = normalized

    @property
    def rate_limit_auto_unban_hours(self) -> int:
        """429 限流账号自动解禁时长（小时）。"""
        value = self._config.get("flow", {}).get("rate_limit_auto_unban_hours", 12)
        try:
            return max(1, int(value))
        except Exception:
            return 12

    def set_rate_limit_auto_unban_hours(self, value: int):
        """Set 429 auto-unban duration in hours."""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(1, int(value))
        except Exception:
            normalized = 12
        self._config["flow"]["rate_limit_auto_unban_hours"] = normalized

    @property
    def server_host(self) -> str:
        return self._config["server"]["host"]

    @property
    def server_port(self) -> int:
        return self._config["server"]["port"]

    @property
    def debug_enabled(self) -> bool:
        return self._config.get("debug", {}).get("enabled", False)

    @property
    def debug_log_requests(self) -> bool:
        return self._config.get("debug", {}).get("log_requests", True)

    @property
    def debug_log_responses(self) -> bool:
        return self._config.get("debug", {}).get("log_responses", True)

    @property
    def debug_mask_token(self) -> bool:
        return self._config.get("debug", {}).get("mask_token", True)

    # Mutable properties for runtime updates
    @property
    def api_key(self) -> str:
        return self._config["global"]["api_key"]

    @api_key.setter
    def api_key(self, value: str):
        self._config["global"]["api_key"] = value

    @property
    def admin_password(self) -> str:
        # If admin_password is set from database, use it; otherwise fall back to config file
        if self._admin_password is not None:
            return self._admin_password
        return self._config["global"]["admin_password"]

    @admin_password.setter
    def admin_password(self, value: str):
        self._admin_password = value
        self._config["global"]["admin_password"] = value

    def set_admin_password_from_db(self, password: str):
        """Set admin password from database"""
        self._admin_password = password

    def set_debug_enabled(self, enabled: bool):
        """Set debug mode enabled/disabled"""
        if "debug" not in self._config:
            self._config["debug"] = {}
        self._config["debug"]["enabled"] = enabled

    @property
    def image_timeout(self) -> int:
        """Get image generation timeout in seconds"""
        return self._config.get("generation", {}).get("image_timeout", 300)

    def set_image_timeout(self, timeout: int):
        """Set image generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["image_timeout"] = timeout

    @property
    def video_timeout(self) -> int:
        """Get video generation timeout in seconds"""
        return self._config.get("generation", {}).get("video_timeout", 1500)

    def set_video_timeout(self, timeout: int):
        """Set video generation timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["video_timeout"] = timeout

    @property
    def polling_mode_enabled(self) -> bool:
        """Get polling mode enabled status."""
        return self.call_logic_mode == "polling"

    @property
    def call_logic_mode(self) -> str:
        """Get call logic mode (default or polling)."""
        call_logic = self._config.get("call_logic", {})
        mode = call_logic.get("call_mode")
        if mode in ("default", "polling"):
            return mode
        if call_logic.get("polling_mode_enabled", False):
            return "polling"
        return "default"

    def set_polling_mode_enabled(self, enabled: bool):
        """Set polling mode enabled/disabled."""
        self.set_call_logic_mode("polling" if enabled else "default")

    def set_call_logic_mode(self, mode: str):
        """Set call logic mode (default or polling)."""
        normalized = "polling" if mode == "polling" else "default"
        if "call_logic" not in self._config:
            self._config["call_logic"] = {}
        self._config["call_logic"]["call_mode"] = normalized
        self._config["call_logic"]["polling_mode_enabled"] = normalized == "polling"

    def set_flow_image_slot_wait_timeout(self, timeout: float):
        """Set image hard slot wait timeout."""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(1.0, min(600.0, float(timeout)))
        except Exception:
            normalized = 120.0
        self._config["flow"]["image_slot_wait_timeout"] = normalized

    def set_flow_video_slot_wait_timeout(self, timeout: float):
        """Set video hard slot wait timeout."""
        if "flow" not in self._config:
            self._config["flow"] = {}
        try:
            normalized = max(1.0, min(600.0, float(timeout)))
        except Exception:
            normalized = 120.0
        self._config["flow"]["video_slot_wait_timeout"] = normalized

    @property
    def upsample_timeout(self) -> int:
        """Get upsample (4K/2K) timeout in seconds"""
        return self._config.get("generation", {}).get("upsample_timeout", 300)

    def set_upsample_timeout(self, timeout: int):
        """Set upsample (4K/2K) timeout in seconds"""
        if "generation" not in self._config:
            self._config["generation"] = {}
        self._config["generation"]["upsample_timeout"] = timeout

    # Cache configuration
    @property
    def cache_enabled(self) -> bool:
        """Get cache enabled status"""
        return self._config.get("cache", {}).get("enabled", False)

    def set_cache_enabled(self, enabled: bool):
        """Set cache enabled status"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["enabled"] = enabled

    @property
    def cache_timeout(self) -> int:
        """Get cache timeout in seconds"""
        return self._config.get("cache", {}).get("timeout", 7200)

    def set_cache_timeout(self, timeout: int):
        """Set cache timeout in seconds"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["timeout"] = timeout

    @property
    def cache_base_url(self) -> str:
        """Get cache base URL"""
        return self._config.get("cache", {}).get("base_url", "")

    def set_cache_base_url(self, base_url: str):
        """Set cache base URL"""
        if "cache" not in self._config:
            self._config["cache"] = {}
        self._config["cache"]["base_url"] = base_url

    # Captcha configuration
    @property
    def captcha_method(self) -> str:
        """Get captcha method"""
        method = self._config.get("captcha", {}).get("captcha_method", "yescaptcha")
        if method == "remote_browser":
            return "personal"
        if method == "extensions":
            return "extension"
        return method

    def set_captcha_method(self, method: str):
        """Set captcha method"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        if method == "remote_browser":
            method = "personal"
        elif method == "extensions":
            method = "extension"
        self._config["captcha"]["captcha_method"] = method

    @property
    def browser_launch_background(self) -> bool:
        """有头浏览器打码是否默认后台启动，避免抢占前台窗口。"""
        return self._config.get("captcha", {}).get("browser_launch_background", True)

    def set_browser_launch_background(self, enabled: bool):
        """设置有头浏览器打码是否后台启动。"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["browser_launch_background"] = bool(enabled)

    @property
    def browser_count(self) -> int:
        """浏览器打码实例数量，browser/personal 模式共用。"""
        value = self._config.get("captcha", {}).get("browser_count", 1)
        try:
            return max(1, min(20, int(value)))
        except Exception:
            return 1

    def set_browser_count(self, value: int):
        """设置浏览器打码实例数量。"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["browser_count"] = max(1, min(20, int(value)))

    @property
    def browser_recaptcha_settle_seconds(self) -> float:
        """有头打码在 reload/clr 就绪后的额外等待秒数。"""
        value = self._config.get("captcha", {}).get("browser_recaptcha_settle_seconds", 3.0)
        try:
            return max(0.0, min(10.0, float(value)))
        except Exception:
            return 3.0

    @property
    def browser_idle_ttl_seconds(self) -> int:
        value = self._config.get("captcha", {}).get("browser_idle_ttl_seconds", 600)
        try:
            return max(60, int(value))
        except Exception:
            return 600

    @property
    def personal_max_resident_tabs(self) -> int:
        """内置浏览器打码单实例共享标签页上限"""
        value = self._config.get("captcha", {}).get("personal_max_resident_tabs", 5)
        try:
            return max(1, min(50, int(value)))  # 限制在1-50之间
        except Exception:
            return 5

    @property
    def personal_project_pool_size(self) -> int:
        """单个 Token 默认维护的项目池数量，仅影响项目轮换。"""
        value = self._config.get("captcha", {}).get("personal_project_pool_size", 4)
        try:
            return max(1, min(50, int(value)))
        except Exception:
            return 4

    @property
    def personal_idle_tab_ttl_seconds(self) -> int:
        """内置浏览器打码标签页空闲超时(秒)"""
        value = self._config.get("captcha", {}).get("personal_idle_tab_ttl_seconds", 600)
        try:
            return max(60, int(value))
        except Exception:
            return 600

    @property
    def personal_browser_user_data_dir(self) -> str:
        """personal 模式显式指定 user-data-dir。"""
        return str(
            self._config.get("captcha", {}).get("personal_browser_user_data_dir", "")
        ).strip()

    @property
    def personal_browser_profile_source_dir(self) -> str:
        """personal 模式用于克隆可信 Chrome 资料的源目录。"""
        return str(
            self._config.get("captcha", {}).get("personal_browser_profile_source_dir", "")
        ).strip()

    @property
    def personal_browser_profile_directory(self) -> str:
        """personal 模式要使用的 Chrome profile 目录名，例如 Default/Profile 1。"""
        value = str(
            self._config.get("captcha", {}).get("personal_browser_profile_directory", "Default")
        ).strip()
        return value or "Default"

    @property
    def browser_startup_cookie_enabled(self) -> bool:
        """是否在浏览器启动时注入系统级明文 Cookie。"""
        return bool(
            self._config.get("captcha", {}).get("browser_startup_cookie_enabled", False)
        )

    @property
    def browser_startup_cookie(self) -> str:
        """浏览器启动时注入的系统级明文 Cookie 文本。"""
        return str(
            self._config.get("captcha", {}).get("browser_startup_cookie", "")
        ).strip()

    def set_personal_max_resident_tabs(self, value: int):
        """设置内置浏览器打码单实例共享标签页上限"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_max_resident_tabs"] = max(1, min(50, int(value)))

    def set_personal_project_pool_size(self, value: int):
        """设置单个 Token 默认维护的项目池数量，仅影响项目轮换"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_project_pool_size"] = max(1, min(50, int(value)))

    def set_personal_idle_tab_ttl_seconds(self, value: int):
        """设置内置浏览器打码标签页空闲超时(秒)"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["personal_idle_tab_ttl_seconds"] = max(60, int(value))

    @property
    def browser_personal_fresh_restart_every_n_solves(self) -> int:
        """内置浏览器成功打码多少次后使用全新 profile 重启，0 表示禁用。"""
        value = self._config.get("captcha", {}).get("browser_personal_fresh_restart_every_n_solves", 10)
        try:
            return max(0, int(value))
        except Exception:
            return 10

    def set_browser_personal_fresh_restart_every_n_solves(self, value: int):
        """设置内置浏览器 fresh profile 轮换阈值。"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["browser_personal_fresh_restart_every_n_solves"] = max(0, int(value))

    @property
    def yescaptcha_api_key(self) -> str:
        """Get YesCaptcha API key"""
        return self._config.get("captcha", {}).get("yescaptcha_api_key", "")

    def set_yescaptcha_api_key(self, api_key: str):
        """Set YesCaptcha API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_api_key"] = api_key

    @property
    def yescaptcha_base_url(self) -> str:
        """Get YesCaptcha base URL"""
        return self._config.get("captcha", {}).get("yescaptcha_base_url", "https://api.yescaptcha.com")

    def set_yescaptcha_base_url(self, base_url: str):
        """Set YesCaptcha base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_base_url"] = base_url

    @property
    def yescaptcha_task_type(self) -> str:
        """Get YesCaptcha reCAPTCHA V3 task type"""
        return normalize_yescaptcha_task_type(
            self._config.get("captcha", {}).get(
                "yescaptcha_task_type",
                DEFAULT_YESCAPTCHA_TASK_TYPE,
            )
        )

    def set_yescaptcha_task_type(self, task_type: str):
        """Set YesCaptcha reCAPTCHA V3 task type"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["yescaptcha_task_type"] = normalize_yescaptcha_task_type(task_type)

    @property
    def capmonster_api_key(self) -> str:
        """Get CapMonster API key"""
        return self._config.get("captcha", {}).get("capmonster_api_key", "")

    def set_capmonster_api_key(self, api_key: str):
        """Set CapMonster API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capmonster_api_key"] = api_key

    @property
    def capmonster_base_url(self) -> str:
        """Get CapMonster base URL"""
        return self._config.get("captcha", {}).get("capmonster_base_url", "https://api.capmonster.cloud")

    def set_capmonster_base_url(self, base_url: str):
        """Set CapMonster base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capmonster_base_url"] = base_url

    @property
    def ezcaptcha_api_key(self) -> str:
        """Get EzCaptcha API key"""
        return self._config.get("captcha", {}).get("ezcaptcha_api_key", "")

    def set_ezcaptcha_api_key(self, api_key: str):
        """Set EzCaptcha API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["ezcaptcha_api_key"] = api_key

    @property
    def ezcaptcha_base_url(self) -> str:
        """Get EzCaptcha base URL"""
        return self._config.get("captcha", {}).get("ezcaptcha_base_url", "https://api.ez-captcha.com")

    def set_ezcaptcha_base_url(self, base_url: str):
        """Set EzCaptcha base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["ezcaptcha_base_url"] = base_url

    @property
    def capsolver_api_key(self) -> str:
        """Get CapSolver API key"""
        return self._config.get("captcha", {}).get("capsolver_api_key", "")

    def set_capsolver_api_key(self, api_key: str):
        """Set CapSolver API key"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capsolver_api_key"] = api_key

    @property
    def capsolver_base_url(self) -> str:
        """Get CapSolver base URL"""
        return self._config.get("captcha", {}).get("capsolver_base_url", "https://api.capsolver.com")

    def set_capsolver_base_url(self, base_url: str):
        """Set CapSolver base URL"""
        if "captcha" not in self._config:
            self._config["captcha"] = {}
        self._config["captcha"]["capsolver_base_url"] = base_url


# Global config instance
config = Config()
