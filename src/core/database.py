"""Database storage layer for Flow2API"""
import asyncio
import aiosqlite
import json
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional, List, Dict, Any
from pathlib import Path
from .config import DEFAULT_YESCAPTCHA_TASK_TYPE, normalize_yescaptcha_task_type
from .logger import debug_logger
from .models import (
    AdminConfig,
    BatchJob,
    BatchJobItem,
    BrowserProfile,
    CacheConfig,
    CallLogicConfig,
    CaptchaConfig,
    GenerationConfig,
    PluginConfig,
    Project,
    ProxyConfig,
    RequestLog,
    SchedulerConfig,
    Task,
    Token,
    TokenStats,
    WorkerJob,
    WorkerNode,
    WorkerSlot,
)


class Database:
    """SQLite database manager"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            # Store database in data directory
            data_dir = Path(__file__).parent.parent.parent / "data"
            data_dir.mkdir(exist_ok=True)
            db_path = str(data_dir / "flow.db")
        self.db_path = db_path
        self._write_lock = asyncio.Lock()
        self._connect_timeout = 30
        self._busy_timeout_ms = 30000

    def db_exists(self) -> bool:
        """Check if database file exists"""
        return Path(self.db_path).exists()

    async def _configure_connection(self, db):
        """Apply SQLite runtime settings for better concurrent behavior."""
        await db.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        await db.execute("PRAGMA foreign_keys = ON")
        # Tolerate legacy/corrupted TEXT rows so admin reads do not fail hard.
        db.text_factory = lambda b: b.decode("utf-8", errors="replace") if isinstance(b, (bytes, bytearray)) else str(b)

    def _current_stats_date(self) -> str:
        """Return the logical date used by daily token statistics."""
        return date.today().isoformat()

    def _normalize_token_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Best-effort sanitize token rows so one corrupted record does not break all reads."""
        normalized = dict(row or {})
        datetime_fields = (
            "at_expires",
            "created_at",
            "last_used_at",
            "banned_at",
            "automation_cooldown_until",
            "automation_last_risk_at",
        )

        for field in datetime_fields:
            value = normalized.get(field)
            if value is None or isinstance(value, datetime):
                continue
            text = str(value).strip()
            if not text:
                normalized[field] = None
                continue
            try:
                normalized[field] = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except Exception:
                normalized[field] = None

        return normalized

    def _token_from_row(self, row: Any) -> Token:
        return Token(**self._normalize_token_row(dict(row)))

    def _loads_json_field(self, value: Any) -> Any:
        if value is None or value == "":
            return None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except Exception:
            return None

    def _dumps_json_field(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)

    def _batch_job_from_row(self, row: Any) -> BatchJob:
        return BatchJob(**dict(row))

    def _batch_job_item_from_row(self, row: Any) -> BatchJobItem:
        item_dict = dict(row)
        item_dict["normalized_payload"] = self._loads_json_field(item_dict.get("normalized_payload"))
        return BatchJobItem(**item_dict)

    @asynccontextmanager
    async def _connect(self, *, write: bool = False):
        """Open a configured SQLite connection and optionally serialize writes."""
        if write:
            async with self._write_lock:
                async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
                    await self._configure_connection(db)
                    yield db
            return

        async with aiosqlite.connect(self.db_path, timeout=self._connect_timeout) as db:
            await self._configure_connection(db)
            yield db

    async def _table_exists(self, db, table_name: str) -> bool:
        """Check if a table exists in the database"""
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        result = await cursor.fetchone()
        return result is not None

    async def _column_exists(self, db, table_name: str, column_name: str) -> bool:
        """Check if a column exists in a table"""
        try:
            cursor = await db.execute(f"PRAGMA table_info({table_name})")
            columns = await cursor.fetchall()
            return any(col[1] == column_name for col in columns)
        except:
            return False

    async def _dedupe_token_stats(self, db) -> None:
        """Ensure token_stats keeps only the latest row for each token_id."""
        if not await self._table_exists(db, "token_stats"):
            return
        cursor = await db.execute("""
            SELECT token_id, MAX(id) AS keep_id, COUNT(*) AS row_count
            FROM token_stats
            GROUP BY token_id
            HAVING COUNT(*) > 1
        """)
        duplicate_rows = await cursor.fetchall()
        if not duplicate_rows:
            return

        for row in duplicate_rows:
            await db.execute(
                "DELETE FROM token_stats WHERE token_id = ? AND id <> ?",
                (row[0], row[1]),
            )
        print(f"  ✓ Deduplicated token_stats rows for {len(duplicate_rows)} token(s)")

    async def _migrate_tokens_table_allow_nullable_st(self, db):
        """Rebuild legacy tokens table so `st` can be NULL."""
        try:
            cursor = await db.execute("PRAGMA table_info(tokens)")
            columns = await cursor.fetchall()
        except Exception:
            return

        st_column = next((col for col in columns if col[1] == "st"), None)
        if not st_column or int(st_column[3] or 0) == 0:
            return

        print("  ✓ Rebuilding legacy tokens table to allow nullable st")
        await db.execute("PRAGMA foreign_keys = OFF")
        await db.execute("ALTER TABLE tokens RENAME TO tokens_legacy_notnull_st")
        await db.execute("""
            CREATE TABLE tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                st TEXT UNIQUE,
                cookie TEXT,
                at TEXT,
                at_expires TIMESTAMP,
                email TEXT NOT NULL,
                name TEXT,
                remark TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used_at TIMESTAMP,
                use_count INTEGER DEFAULT 0,
                credits INTEGER DEFAULT 0,
                user_paygate_tier TEXT,
                current_project_id TEXT,
                current_project_name TEXT,
                image_enabled BOOLEAN DEFAULT 1,
                video_enabled BOOLEAN DEFAULT 1,
                image_concurrency INTEGER DEFAULT -1,
                video_concurrency INTEGER DEFAULT 1,
                captcha_proxy_url TEXT,
                extension_route_key TEXT,
                ban_reason TEXT,
                banned_at TIMESTAMP,
                automation_risk_score INTEGER DEFAULT 0,
                automation_risk_state TEXT DEFAULT 'healthy',
                automation_cooldown_until TIMESTAMP,
                automation_last_risk_reason TEXT,
                automation_last_risk_at TIMESTAMP
            )
        """)
        await db.execute("""
            INSERT INTO tokens (
                id, st, cookie, at, at_expires, email, name, remark, is_active,
                created_at, last_used_at, use_count, credits, user_paygate_tier,
                current_project_id, current_project_name, image_enabled, video_enabled,
                image_concurrency, video_concurrency, captcha_proxy_url, extension_route_key,
                ban_reason, banned_at, automation_risk_score, automation_risk_state,
                automation_cooldown_until, automation_last_risk_reason, automation_last_risk_at
            )
            SELECT
                id, st, cookie, at, at_expires, email, name, remark, is_active,
                created_at, last_used_at, use_count, credits, user_paygate_tier,
                current_project_id, current_project_name, image_enabled, video_enabled,
                image_concurrency, video_concurrency, captcha_proxy_url, extension_route_key,
                ban_reason, banned_at,
                COALESCE(automation_risk_score, 0),
                COALESCE(NULLIF(automation_risk_state, ''), 'healthy'),
                automation_cooldown_until,
                automation_last_risk_reason,
                automation_last_risk_at
            FROM tokens_legacy_notnull_st
        """)
        await db.execute("DROP TABLE tokens_legacy_notnull_st")
        await db.execute("PRAGMA foreign_keys = ON")

    async def _table_sql_contains(self, db, table_name: str, needle: str) -> bool:
        """Check whether a table definition contains a given SQL fragment."""
        try:
            cursor = await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,),
            )
            row = await cursor.fetchone()
            return bool(row and needle in str(row[0] or ""))
        except Exception:
            return False

    async def _rebuild_table(self, db, table_name: str, create_sql: str, columns: list[str]):
        """Rebuild a table and copy rows back into the fresh schema."""
        if not await self._table_exists(db, table_name):
            return

        temp_name = f"{table_name}_fk_rebuild_old"
        column_sql = ", ".join(columns)
        await db.execute(f"ALTER TABLE {table_name} RENAME TO {temp_name}")
        await db.execute(create_sql)
        await db.execute(
            f"INSERT INTO {table_name} ({column_sql}) SELECT {column_sql} FROM {temp_name}"
        )
        await db.execute(f"DROP TABLE {temp_name}")

    async def _migrate_captcha_config_drop_remote_browser_fields(self, db):
        """Rebuild captcha_config to remove legacy remote_browser columns."""
        if not await self._table_exists(db, "captcha_config"):
            return

        legacy_columns = (
            "remote_browser_base_url",
            "remote_browser_api_key",
            "remote_browser_timeout",
        )
        has_legacy_column = False
        for name in legacy_columns:
            if await self._column_exists(db, "captcha_config", name):
                has_legacy_column = True
                break
        if not has_legacy_column:
            return

        print("  ✓ Rebuilding captcha_config to drop legacy remote_browser columns")
        await db.execute("PRAGMA foreign_keys = OFF")
        await db.execute("ALTER TABLE captcha_config RENAME TO captcha_config_remote_browser_legacy")
        await db.execute("""
            CREATE TABLE captcha_config (
                id INTEGER PRIMARY KEY DEFAULT 1,
                captcha_method TEXT DEFAULT 'browser',
                yescaptcha_api_key TEXT DEFAULT '',
                yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                yescaptcha_task_type TEXT DEFAULT 'RecaptchaV3TaskProxylessM1',
                capmonster_api_key TEXT DEFAULT '',
                capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                ezcaptcha_api_key TEXT DEFAULT '',
                ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                capsolver_api_key TEXT DEFAULT '',
                capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                page_action TEXT DEFAULT 'IMAGE_GENERATION',
                browser_proxy_enabled BOOLEAN DEFAULT 0,
                browser_proxy_url TEXT,
                browser_count INTEGER DEFAULT 1,
                personal_project_pool_size INTEGER DEFAULT 4,
                personal_max_resident_tabs INTEGER DEFAULT 5,
                browser_personal_fresh_restart_every_n_solves INTEGER DEFAULT 10,
                personal_idle_tab_ttl_seconds INTEGER DEFAULT 600,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            INSERT INTO captcha_config (
                id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                yescaptcha_task_type, capmonster_api_key, capmonster_base_url,
                ezcaptcha_api_key, ezcaptcha_base_url, capsolver_api_key,
                capsolver_base_url, website_key, page_action, browser_proxy_enabled,
                browser_proxy_url, browser_count, personal_project_pool_size,
                personal_max_resident_tabs, browser_personal_fresh_restart_every_n_solves,
                personal_idle_tab_ttl_seconds, created_at, updated_at
            )
            SELECT
                id,
                CASE WHEN captcha_method = 'remote_browser' THEN 'personal' ELSE captcha_method END,
                yescaptcha_api_key,
                yescaptcha_base_url,
                yescaptcha_task_type,
                capmonster_api_key,
                capmonster_base_url,
                ezcaptcha_api_key,
                ezcaptcha_base_url,
                capsolver_api_key,
                capsolver_base_url,
                website_key,
                page_action,
                browser_proxy_enabled,
                browser_proxy_url,
                browser_count,
                personal_project_pool_size,
                personal_max_resident_tabs,
                browser_personal_fresh_restart_every_n_solves,
                personal_idle_tab_ttl_seconds,
                created_at,
                updated_at
            FROM captcha_config_remote_browser_legacy
        """)
        await db.execute("DROP TABLE captcha_config_remote_browser_legacy")
        await db.execute("PRAGMA foreign_keys = ON")

    async def _repair_tables_referencing_legacy_tokens(self, db):
        """Repair dependent tables whose FK target was rewritten to tokens_legacy_notnull_st."""
        legacy_target = 'tokens_legacy_notnull_st'
        affected_tables = (
            "projects",
            "token_stats",
            "tasks",
            "request_logs",
            "browser_profiles",
            "worker_jobs",
        )
        if not any([await self._table_sql_contains(db, name, legacy_target) for name in affected_tables]):
            return

        print("  ✓ Rebuilding dependent tables that still reference legacy tokens table")
        await db.execute("PRAGMA foreign_keys = OFF")

        await self._rebuild_table(
            db,
            "projects",
            """
                CREATE TABLE projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    project_name TEXT NOT NULL,
                    tool_name TEXT DEFAULT 'PINHOLE',
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """,
            ["id", "project_id", "token_id", "project_name", "tool_name", "is_active", "created_at"],
        )
        await self._rebuild_table(
            db,
            "token_stats",
            """
                CREATE TABLE token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    image_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_success_at TIMESTAMP,
                    last_error_at TIMESTAMP,
                    today_image_count INTEGER DEFAULT 0,
                    today_video_count INTEGER DEFAULT 0,
                    today_error_count INTEGER DEFAULT 0,
                    today_date DATE,
                    consecutive_error_count INTEGER DEFAULT 0,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """,
            [
                "id", "token_id", "image_count", "video_count", "success_count", "error_count",
                "last_success_at", "last_error_at", "today_image_count", "today_video_count",
                "today_error_count", "today_date", "consecutive_error_count",
            ],
        )
        await self._rebuild_table(
            db,
            "tasks",
            """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    progress INTEGER DEFAULT 0,
                    result_urls TEXT,
                    error_message TEXT,
                    scene_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """,
            [
                "id", "task_id", "token_id", "model", "prompt", "status", "progress",
                "result_urls", "error_message", "scene_id", "created_at", "completed_at",
            ],
        )
        await self._rebuild_table(
            db,
            "request_logs",
            """
                CREATE TABLE request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    operation TEXT NOT NULL,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER NOT NULL,
                    duration FLOAT NOT NULL,
                    status_text TEXT DEFAULT '',
                    progress INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """,
            [
                "id", "token_id", "operation", "request_body", "response_body", "status_code",
                "duration", "status_text", "progress", "created_at", "updated_at",
            ],
        )
        await self._rebuild_table(
            db,
            "browser_profiles",
            """
                CREATE TABLE browser_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER UNIQUE,
                    expected_email TEXT,
                    proxy_binding TEXT,
                    storage_path TEXT,
                    profile_type TEXT DEFAULT 'chrome_local',
                    last_known_project_id TEXT,
                    health_status TEXT DEFAULT 'provisioned',
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """,
            [
                "id", "profile_id", "token_id", "expected_email", "proxy_binding", "storage_path",
                "profile_type", "last_known_project_id", "health_status", "notes",
                "created_at", "updated_at", "last_seen_at",
            ],
        )
        await self._rebuild_table(
            db,
            "worker_jobs",
            """
                CREATE TABLE worker_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER,
                    profile_id TEXT,
                    slot_id TEXT,
                    route_key TEXT,
                    job_type TEXT NOT NULL,
                    status TEXT DEFAULT 'queued',
                    request_payload TEXT,
                    result_payload TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (profile_id) REFERENCES browser_profiles(profile_id),
                    FOREIGN KEY (slot_id) REFERENCES worker_slots(slot_id)
                )
            """,
            [
                "id", "job_id", "token_id", "profile_id", "slot_id", "route_key", "job_type",
                "status", "request_payload", "result_payload", "error_message",
                "created_at", "started_at", "finished_at",
            ],
        )
        await db.execute("PRAGMA foreign_keys = ON")

    async def _repair_tables_referencing_rebuild_temps(self, db):
        """Repair tables whose FK target was accidentally rewritten to a rebuild temp table name."""
        stale_patterns = (
            "browser_profiles_fk_rebuild_old",
            "worker_slots_fk_rebuild_old",
            "worker_jobs_fk_rebuild_old",
            "projects_fk_rebuild_old",
            "token_stats_fk_rebuild_old",
            "tasks_fk_rebuild_old",
            "request_logs_fk_rebuild_old",
        )
        affected_tables = (
            "browser_profiles",
            "worker_slots",
            "worker_jobs",
        )
        if not any(
            [await self._table_sql_contains(db, table_name, pattern) for table_name in affected_tables for pattern in stale_patterns]
        ):
            return

        print("  ✓ Rebuilding tables that still reference fk rebuild temp names")
        await db.execute("PRAGMA foreign_keys = OFF")

        await self._rebuild_table(
            db,
            "browser_profiles",
            """
                CREATE TABLE browser_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER UNIQUE,
                    expected_email TEXT,
                    proxy_binding TEXT,
                    storage_path TEXT,
                    profile_type TEXT DEFAULT 'chrome_local',
                    last_known_project_id TEXT,
                    health_status TEXT DEFAULT 'provisioned',
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """,
            [
                "id", "profile_id", "token_id", "expected_email", "proxy_binding", "storage_path",
                "profile_type", "last_known_project_id", "health_status", "notes",
                "created_at", "updated_at", "last_seen_at",
            ],
        )
        await self._rebuild_table(
            db,
            "worker_slots",
            """
                CREATE TABLE worker_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_id TEXT UNIQUE NOT NULL,
                    worker_node_id TEXT,
                    route_key TEXT,
                    client_label TEXT,
                    profile_id TEXT,
                    current_email TEXT,
                    page_url TEXT,
                    project_id TEXT,
                    session_state TEXT,
                    worker_mode TEXT,
                    busy BOOLEAN DEFAULT 0,
                    job_type TEXT,
                    last_seen_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (worker_node_id) REFERENCES worker_nodes(worker_node_id),
                    FOREIGN KEY (profile_id) REFERENCES browser_profiles(profile_id)
                )
            """,
            [
                "id", "slot_id", "worker_node_id", "route_key", "client_label", "profile_id",
                "current_email", "page_url", "project_id", "session_state", "worker_mode",
                "busy", "job_type", "last_seen_at", "created_at", "updated_at",
            ],
        )
        await self._rebuild_table(
            db,
            "worker_jobs",
            """
                CREATE TABLE worker_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER,
                    profile_id TEXT,
                    slot_id TEXT,
                    route_key TEXT,
                    job_type TEXT NOT NULL,
                    status TEXT DEFAULT 'queued',
                    request_payload TEXT,
                    result_payload TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (profile_id) REFERENCES browser_profiles(profile_id),
                    FOREIGN KEY (slot_id) REFERENCES worker_slots(slot_id)
                )
            """,
            [
                "id", "job_id", "token_id", "profile_id", "slot_id", "route_key", "job_type",
                "status", "request_payload", "result_payload", "error_message",
                "created_at", "started_at", "finished_at",
            ],
        )
        await db.execute("PRAGMA foreign_keys = ON")

    async def _ensure_config_rows(self, db, config_dict: dict = None):
        """Ensure all config tables have their default rows

        Args:
            db: Database connection
            config_dict: Configuration dictionary from setting.toml (optional)
                        If None, use default values instead of reading from TOML.
        """
        # Ensure admin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM admin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            admin_username = "admin"
            admin_password = "admin"
            api_key = "han1234"
            error_ban_threshold = 3

            if config_dict:
                global_config = config_dict.get("global", {})
                admin_username = global_config.get("admin_username", "admin")
                admin_password = global_config.get("admin_password", "admin")
                api_key = global_config.get("api_key", "han1234")

                admin_config = config_dict.get("admin", {})
                error_ban_threshold = admin_config.get("error_ban_threshold", 3)

            await db.execute("""
                INSERT INTO admin_config (id, username, password, api_key, error_ban_threshold)
                VALUES (1, ?, ?, ?, ?)
            """, (admin_username, admin_password, api_key, error_ban_threshold))

        # Ensure proxy_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM proxy_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            proxy_enabled = False
            proxy_url = None
            media_proxy_enabled = False
            media_proxy_url = None

            if config_dict:
                proxy_config = config_dict.get("proxy", {})
                proxy_enabled = proxy_config.get("proxy_enabled", False)
                proxy_url = proxy_config.get("proxy_url", "")
                proxy_url = proxy_url if proxy_url else None
                media_proxy_enabled = proxy_config.get(
                    "media_proxy_enabled",
                    proxy_config.get("image_io_proxy_enabled", False)
                )
                media_proxy_url = proxy_config.get(
                    "media_proxy_url",
                    proxy_config.get("image_io_proxy_url", "")
                )
                media_proxy_url = media_proxy_url if media_proxy_url else None

            await db.execute("""
                INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                VALUES (1, ?, ?, ?, ?)
            """, (proxy_enabled, proxy_url, media_proxy_enabled, media_proxy_url))

        # Ensure generation_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM generation_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            image_timeout = 300
            video_timeout = 1500
            max_retries = 3

            if config_dict:
                generation_config = config_dict.get("generation", {})
                flow_config = config_dict.get("flow", {})
                image_timeout = generation_config.get("image_timeout", 300)
                video_timeout = generation_config.get("video_timeout", 1500)
                max_retries = flow_config.get("max_retries", 3)

            try:
                max_retries = max(1, int(max_retries))
            except Exception:
                max_retries = 3

            await db.execute("""
                INSERT INTO generation_config (id, image_timeout, video_timeout, max_retries)
                VALUES (1, ?, ?, ?)
            """, (image_timeout, video_timeout, max_retries))

        # Ensure call_logic_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM call_logic_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            call_mode = "default"
            polling_mode_enabled = False

            if config_dict:
                call_logic_config = config_dict.get("call_logic", {})
                call_mode = call_logic_config.get("call_mode", "default")
                if call_mode not in ("default", "polling"):
                    polling_mode_enabled = call_logic_config.get("polling_mode_enabled", False)
                    call_mode = "polling" if polling_mode_enabled else "default"
                else:
                    polling_mode_enabled = call_mode == "polling"

            await db.execute("""
                INSERT INTO call_logic_config (id, call_mode, polling_mode_enabled)
                VALUES (1, ?, ?)
            """, (call_mode, polling_mode_enabled))

        # Ensure scheduler_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM scheduler_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            exhausted_credit_threshold = 0
            low_credit_threshold = 100
            image_slot_wait_timeout = 120.0
            video_slot_wait_timeout = 120.0
            active_token_credit_refresh_interval_seconds = 900
            rate_limit_auto_unban_hours = 12

            if config_dict:
                flow_config = config_dict.get("flow", {})
                exhausted_credit_threshold = flow_config.get("exhausted_credit_threshold", 0)
                low_credit_threshold = flow_config.get("low_credit_threshold", 100)
                image_slot_wait_timeout = flow_config.get("image_slot_wait_timeout", 120)
                video_slot_wait_timeout = flow_config.get("video_slot_wait_timeout", 120)
                active_token_credit_refresh_interval_seconds = flow_config.get(
                    "active_token_credit_refresh_interval_seconds",
                    900,
                )
                rate_limit_auto_unban_hours = flow_config.get("rate_limit_auto_unban_hours", 12)

            try:
                exhausted_credit_threshold = max(0, int(exhausted_credit_threshold))
            except Exception:
                exhausted_credit_threshold = 0
            try:
                low_credit_threshold = max(exhausted_credit_threshold, int(low_credit_threshold))
            except Exception:
                low_credit_threshold = 100
            try:
                image_slot_wait_timeout = max(1.0, min(600.0, float(image_slot_wait_timeout)))
            except Exception:
                image_slot_wait_timeout = 120.0
            try:
                video_slot_wait_timeout = max(1.0, min(600.0, float(video_slot_wait_timeout)))
            except Exception:
                video_slot_wait_timeout = 120.0
            try:
                active_token_credit_refresh_interval_seconds = max(0, int(active_token_credit_refresh_interval_seconds))
            except Exception:
                active_token_credit_refresh_interval_seconds = 900
            try:
                rate_limit_auto_unban_hours = max(1, int(rate_limit_auto_unban_hours))
            except Exception:
                rate_limit_auto_unban_hours = 12

            await db.execute("""
                INSERT INTO scheduler_config (
                    id,
                    exhausted_credit_threshold,
                    low_credit_threshold,
                    image_slot_wait_timeout,
                    video_slot_wait_timeout,
                    active_token_credit_refresh_interval_seconds,
                    rate_limit_auto_unban_hours
                )
                VALUES (1, ?, ?, ?, ?, ?, ?)
            """, (
                exhausted_credit_threshold,
                low_credit_threshold,
                image_slot_wait_timeout,
                video_slot_wait_timeout,
                active_token_credit_refresh_interval_seconds,
                rate_limit_auto_unban_hours,
            ))

        # Ensure cache_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM cache_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            cache_enabled = False
            cache_timeout = 7200
            cache_base_url = None

            if config_dict:
                cache_config = config_dict.get("cache", {})
                cache_enabled = cache_config.get("enabled", False)
                cache_timeout = cache_config.get("timeout", 7200)
                cache_base_url = cache_config.get("base_url", "")
                # Convert empty string to None
                cache_base_url = cache_base_url if cache_base_url else None

            await db.execute("""
                INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                VALUES (1, ?, ?, ?)
            """, (cache_enabled, cache_timeout, cache_base_url))

        # Ensure debug_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM debug_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            debug_enabled = False
            log_requests = True
            log_responses = True
            mask_token = True

            if config_dict:
                debug_config = config_dict.get("debug", {})
                debug_enabled = debug_config.get("enabled", False)
                log_requests = debug_config.get("log_requests", True)
                log_responses = debug_config.get("log_responses", True)
                mask_token = debug_config.get("mask_token", True)

            await db.execute("""
                INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                VALUES (1, ?, ?, ?, ?)
            """, (debug_enabled, log_requests, log_responses, mask_token))

        # Ensure captcha_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM captcha_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            captcha_method = "personal"
            yescaptcha_api_key = ""
            yescaptcha_base_url = "https://api.yescaptcha.com"
            yescaptcha_task_type = DEFAULT_YESCAPTCHA_TASK_TYPE
            browser_count = 1
            personal_project_pool_size = 4
            personal_max_resident_tabs = 5
            browser_personal_fresh_restart_every_n_solves = 10
            personal_idle_tab_ttl_seconds = 600

            if config_dict:
                captcha_config = config_dict.get("captcha", {})
                captcha_method = captcha_config.get("captcha_method", "personal")
                if captcha_method == "remote_browser":
                    captcha_method = "personal"
                yescaptcha_api_key = captcha_config.get("yescaptcha_api_key", "")
                yescaptcha_base_url = captcha_config.get("yescaptcha_base_url", "https://api.yescaptcha.com")
                yescaptcha_task_type = normalize_yescaptcha_task_type(captcha_config.get("yescaptcha_task_type"))
                browser_count = captcha_config.get("browser_count", 1)
                personal_project_pool_size = captcha_config.get("personal_project_pool_size", 4)
                personal_max_resident_tabs = captcha_config.get("personal_max_resident_tabs", 5)
                browser_personal_fresh_restart_every_n_solves = captcha_config.get("browser_personal_fresh_restart_every_n_solves", 10)
                personal_idle_tab_ttl_seconds = captcha_config.get("personal_idle_tab_ttl_seconds", 600)
            try:
                browser_count = max(1, int(browser_count))
            except Exception:
                browser_count = 1
            try:
                personal_project_pool_size = max(1, min(50, int(personal_project_pool_size)))
            except Exception:
                personal_project_pool_size = 4
            try:
                personal_max_resident_tabs = max(1, min(50, int(personal_max_resident_tabs)))
            except Exception:
                personal_max_resident_tabs = 5
            try:
                browser_personal_fresh_restart_every_n_solves = max(0, int(browser_personal_fresh_restart_every_n_solves))
            except Exception:
                browser_personal_fresh_restart_every_n_solves = 10
            try:
                personal_idle_tab_ttl_seconds = max(60, int(personal_idle_tab_ttl_seconds))
            except Exception:
                personal_idle_tab_ttl_seconds = 600

            await db.execute("""
                INSERT INTO captcha_config (
                    id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                    yescaptcha_task_type,
                    browser_count, personal_project_pool_size,
                    personal_max_resident_tabs, browser_personal_fresh_restart_every_n_solves,
                    personal_idle_tab_ttl_seconds
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                captcha_method,
                yescaptcha_api_key,
                yescaptcha_base_url,
                yescaptcha_task_type,
                browser_count,
                personal_project_pool_size,
                personal_max_resident_tabs,
                browser_personal_fresh_restart_every_n_solves,
                personal_idle_tab_ttl_seconds,
            ))

        # Ensure plugin_config has a row
        cursor = await db.execute("SELECT COUNT(*) FROM plugin_config")
        count = await cursor.fetchone()
        if count[0] == 0:
            await db.execute("""
                INSERT INTO plugin_config (id, connection_token, auto_enable_on_update)
                VALUES (1, '', 1)
            """)

    async def check_and_migrate_db(self, config_dict: dict = None):
        """Check database integrity and perform migrations if needed

        This method is called during upgrade mode to:
        1. Create missing tables (if they don't exist)
        2. Add missing columns to existing tables
        3. Ensure all config tables have default rows

        Args:
            config_dict: Configuration dictionary from setting.toml (optional)
                        Used only to initialize missing config rows with default values.
                        Existing config rows will NOT be overwritten.
        """
        async with self._connect(write=True) as db:
            print("Checking database integrity and performing migrations...")
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")

            # ========== Step 1: Create missing tables ==========
            # Check and create cache_config table if missing
            if not await self._table_exists(db, "cache_config"):
                print("  ✓ Creating missing table: cache_config")
                await db.execute("""
                    CREATE TABLE cache_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        cache_enabled BOOLEAN DEFAULT 0,
                        cache_timeout INTEGER DEFAULT 7200,
                        cache_base_url TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create proxy_config table if missing
            if not await self._table_exists(db, "proxy_config"):
                print("  ✓ Creating missing table: proxy_config")
                await db.execute("""
                    CREATE TABLE proxy_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        enabled BOOLEAN DEFAULT 0,
                        proxy_url TEXT,
                        media_proxy_enabled BOOLEAN DEFAULT 0,
                        media_proxy_url TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create call_logic_config table if missing
            if not await self._table_exists(db, "call_logic_config"):
                print("  Creating missing table: call_logic_config")
                await db.execute("""
                    CREATE TABLE call_logic_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        call_mode TEXT DEFAULT 'default',
                        polling_mode_enabled BOOLEAN DEFAULT 0,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            if not await self._table_exists(db, "scheduler_config"):
                print("  ✓ Creating missing table: scheduler_config")
                await db.execute("""
                    CREATE TABLE scheduler_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        exhausted_credit_threshold INTEGER DEFAULT 0,
                        low_credit_threshold INTEGER DEFAULT 100,
                        image_slot_wait_timeout REAL DEFAULT 120,
                        video_slot_wait_timeout REAL DEFAULT 120,
                        active_token_credit_refresh_interval_seconds INTEGER DEFAULT 900,
                        rate_limit_auto_unban_hours INTEGER DEFAULT 12,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create captcha_config table if missing
            if not await self._table_exists(db, "captcha_config"):
                print("  ✓ Creating missing table: captcha_config")
                await db.execute("""
                    CREATE TABLE captcha_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        captcha_method TEXT DEFAULT 'browser',
                        yescaptcha_api_key TEXT DEFAULT '',
                        yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                        yescaptcha_task_type TEXT DEFAULT 'RecaptchaV3TaskProxylessM1',
                        capmonster_api_key TEXT DEFAULT '',
                        capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                        ezcaptcha_api_key TEXT DEFAULT '',
                        ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                        capsolver_api_key TEXT DEFAULT '',
                        capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                        website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                        page_action TEXT DEFAULT 'IMAGE_GENERATION',
                        browser_proxy_enabled BOOLEAN DEFAULT 0,
                        browser_proxy_url TEXT,
                        browser_count INTEGER DEFAULT 1,
                        personal_project_pool_size INTEGER DEFAULT 4,
                        personal_max_resident_tabs INTEGER DEFAULT 5,
                        browser_personal_fresh_restart_every_n_solves INTEGER DEFAULT 10,
                        personal_idle_tab_ttl_seconds INTEGER DEFAULT 600,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # Check and create plugin_config table if missing
            if not await self._table_exists(db, "plugin_config"):
                print("  ✓ Creating missing table: plugin_config")
                await db.execute("""
                    CREATE TABLE plugin_config (
                        id INTEGER PRIMARY KEY DEFAULT 1,
                        connection_token TEXT DEFAULT '',
                        auto_enable_on_update BOOLEAN DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

            # ========== Step 2: Add missing columns to existing tables ==========
            # Check and add missing columns to tokens table
            if await self._table_exists(db, "tokens"):
                columns_to_add = [
                    ("cookie", "TEXT"),  # 完整浏览器 cookie 快照
                    ("at", "TEXT"),  # Access Token
                    ("at_expires", "TIMESTAMP"),  # AT expiration time
                    ("credits", "INTEGER DEFAULT 0"),  # Balance
                    ("user_paygate_tier", "TEXT"),  # User tier
                    ("current_project_id", "TEXT"),  # Current project UUID
                    ("current_project_name", "TEXT"),  # Project name
                    ("image_enabled", "BOOLEAN DEFAULT 1"),
                    ("video_enabled", "BOOLEAN DEFAULT 1"),
                    ("image_concurrency", "INTEGER DEFAULT -1"),
                    ("video_concurrency", "INTEGER DEFAULT 1"),
                    ("captcha_proxy_url", "TEXT"),  # token级打码代理
                    ("extension_route_key", "TEXT"),  # extension 模式路由键
                    ("ban_reason", "TEXT"),  # 禁用原因
                    ("banned_at", "TIMESTAMP"),  # 禁用时间
                    ("automation_risk_score", "INTEGER DEFAULT 0"),
                    ("automation_risk_state", "TEXT DEFAULT 'healthy'"),
                    ("automation_cooldown_until", "TIMESTAMP"),
                    ("automation_last_risk_reason", "TEXT"),
                    ("automation_last_risk_at", "TIMESTAMP"),
                ]

                for col_name, col_type in columns_to_add:
                    if not await self._column_exists(db, "tokens", col_name):
                        try:
                            await db.execute(f"ALTER TABLE tokens ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to tokens table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

                await self._migrate_tokens_table_allow_nullable_st(db)
                await self._repair_tables_referencing_legacy_tokens(db)
                await self._repair_tables_referencing_rebuild_temps(db)

            # Check and add missing columns to admin_config table
            if await self._table_exists(db, "admin_config"):
                if not await self._column_exists(db, "admin_config", "error_ban_threshold"):
                    try:
                        await db.execute("ALTER TABLE admin_config ADD COLUMN error_ban_threshold INTEGER DEFAULT 3")
                        print("  ✓ Added column 'error_ban_threshold' to admin_config table")
                    except Exception as e:
                        print(f"  ✗ Failed to add column 'error_ban_threshold': {e}")

            # Check and add missing columns to proxy_config table
            if await self._table_exists(db, "proxy_config"):
                proxy_columns_to_add = [
                    ("media_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("media_proxy_url", "TEXT"),
                ]

                for col_name, col_type in proxy_columns_to_add:
                    if not await self._column_exists(db, "proxy_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE proxy_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to proxy_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to generation_config table
            if await self._table_exists(db, "generation_config"):
                generation_columns_to_add = [
                    ("max_retries", "INTEGER DEFAULT 3"),
                ]

                for col_name, col_type in generation_columns_to_add:
                    if not await self._column_exists(db, "generation_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE generation_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to generation_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to captcha_config table
            if await self._table_exists(db, "captcha_config"):
                captcha_columns_to_add = [
                    ("browser_proxy_enabled", "BOOLEAN DEFAULT 0"),
                    ("browser_proxy_url", "TEXT"),
                    ("yescaptcha_task_type", "TEXT DEFAULT 'RecaptchaV3TaskProxylessM1'"),
                    ("capmonster_api_key", "TEXT DEFAULT ''"),
                    ("capmonster_base_url", "TEXT DEFAULT 'https://api.capmonster.cloud'"),
                    ("ezcaptcha_api_key", "TEXT DEFAULT ''"),
                    ("ezcaptcha_base_url", "TEXT DEFAULT 'https://api.ez-captcha.com'"),
                    ("capsolver_api_key", "TEXT DEFAULT ''"),
                    ("capsolver_base_url", "TEXT DEFAULT 'https://api.capsolver.com'"),
                    ("browser_count", "INTEGER DEFAULT 1"),
                ]

                for col_name, col_type in captcha_columns_to_add:
                    if not await self._column_exists(db, "captcha_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to captcha_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

                await self._migrate_captcha_config_drop_remote_browser_fields(db)

            # Check and add missing columns to token_stats table
            if await self._table_exists(db, "token_stats"):
                stats_columns_to_add = [
                    ("today_image_count", "INTEGER DEFAULT 0"),
                    ("today_video_count", "INTEGER DEFAULT 0"),
                    ("today_error_count", "INTEGER DEFAULT 0"),
                    ("today_date", "DATE"),
                    ("consecutive_error_count", "INTEGER DEFAULT 0"),  # 🆕 连续错误计数
                ]

                for col_name, col_type in stats_columns_to_add:
                    if not await self._column_exists(db, "token_stats", col_name):
                        try:
                            await db.execute(f"ALTER TABLE token_stats ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to token_stats table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            await self._dedupe_token_stats(db)

            # Check and add missing columns to plugin_config table
            if await self._table_exists(db, "plugin_config"):
                plugin_columns_to_add = [
                    ("auto_enable_on_update", "BOOLEAN DEFAULT 1"),  # 默认开启
                ]

                for col_name, col_type in plugin_columns_to_add:
                    if not await self._column_exists(db, "plugin_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE plugin_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to plugin_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # Check and add missing columns to captcha_config table
            if await self._table_exists(db, "captcha_config"):
                captcha_columns_to_add = [
                    ("personal_project_pool_size", "INTEGER DEFAULT 4"),
                    ("personal_max_resident_tabs", "INTEGER DEFAULT 5"),
                    ("browser_personal_fresh_restart_every_n_solves", "INTEGER DEFAULT 10"),
                    ("personal_idle_tab_ttl_seconds", "INTEGER DEFAULT 600"),
                ]

                for col_name, col_type in captcha_columns_to_add:
                    if not await self._column_exists(db, "captcha_config", col_name):
                        try:
                            await db.execute(f"ALTER TABLE captcha_config ADD COLUMN {col_name} {col_type}")
                            print(f"  ✓ Added column '{col_name}' to captcha_config table")
                        except Exception as e:
                            print(f"  ✗ Failed to add column '{col_name}': {e}")

            # ========== Step 3: Ensure all config tables have default rows ==========
            # Note: This will NOT overwrite existing config rows
            # It only ensures missing rows are created with default values from setting.toml
            await self._ensure_config_rows(db, config_dict=config_dict)

            await db.commit()
            print("Database migration check completed.")

    async def init_db(self):
        """Initialize database tables"""
        async with self._connect(write=True) as db:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA synchronous = NORMAL")
            # Tokens table (Flow2API版本)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    st TEXT UNIQUE,
                    cookie TEXT,
                    at TEXT,
                    at_expires TIMESTAMP,
                    email TEXT NOT NULL,
                    name TEXT,
                    remark TEXT,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0,
                    credits INTEGER DEFAULT 0,
                    user_paygate_tier TEXT,
                    current_project_id TEXT,
                    current_project_name TEXT,
                    image_enabled BOOLEAN DEFAULT 1,
                    video_enabled BOOLEAN DEFAULT 1,
                    image_concurrency INTEGER DEFAULT -1,
                    video_concurrency INTEGER DEFAULT 1,
                    captcha_proxy_url TEXT,
                    extension_route_key TEXT,
                    ban_reason TEXT,
                    banned_at TIMESTAMP,
                    automation_risk_score INTEGER DEFAULT 0,
                    automation_risk_state TEXT DEFAULT 'healthy',
                    automation_cooldown_until TIMESTAMP,
                    automation_last_risk_reason TEXT,
                    automation_last_risk_at TIMESTAMP
                )
            """)

            # Projects table (新增)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    project_name TEXT NOT NULL,
                    tool_name TEXT DEFAULT 'PINHOLE',
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Token stats table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS token_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER NOT NULL,
                    image_count INTEGER DEFAULT 0,
                    video_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    last_success_at TIMESTAMP,
                    last_error_at TIMESTAMP,
                    today_image_count INTEGER DEFAULT 0,
                    today_video_count INTEGER DEFAULT 0,
                    today_error_count INTEGER DEFAULT 0,
                    today_date DATE,
                    consecutive_error_count INTEGER DEFAULT 0,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Tasks table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    progress INTEGER DEFAULT 0,
                    result_urls TEXT,
                    error_message TEXT,
                    scene_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Batch jobs tables
            await db.execute("""
                CREATE TABLE IF NOT EXISTS batch_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'csv',
                    file_name TEXT,
                    raw_payload_text TEXT,
                    status TEXT NOT NULL DEFAULT 'draft',
                    total_count INTEGER DEFAULT 0,
                    pending_count INTEGER DEFAULT 0,
                    running_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS batch_job_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    row_index INTEGER NOT NULL,
                    row_id TEXT,
                    task_type TEXT NOT NULL,
                    normalized_payload TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_code TEXT,
                    error_message TEXT,
                    result_media_id TEXT,
                    result_url TEXT,
                    token_id INTEGER,
                    project_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    FOREIGN KEY (job_id) REFERENCES batch_jobs(job_id) ON DELETE CASCADE,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (project_id) REFERENCES projects(project_id)
                )
            """)

            # Request logs table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_id INTEGER,
                    operation TEXT NOT NULL,
                    request_body TEXT,
                    response_body TEXT,
                    status_code INTEGER NOT NULL,
                    duration FLOAT NOT NULL,
                    status_text TEXT DEFAULT '',
                    progress INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            # Admin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS admin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    username TEXT DEFAULT 'admin',
                    password TEXT DEFAULT 'admin',
                    api_key TEXT DEFAULT 'han1234',
                    error_ban_threshold INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Proxy config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS proxy_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    proxy_url TEXT,
                    media_proxy_enabled BOOLEAN DEFAULT 0,
                    media_proxy_url TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Generation config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS generation_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    image_timeout INTEGER DEFAULT 300,
                    video_timeout INTEGER DEFAULT 1500,
                    max_retries INTEGER DEFAULT 3,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Call logic config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS call_logic_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    call_mode TEXT DEFAULT 'default',
                    polling_mode_enabled BOOLEAN DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS scheduler_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    exhausted_credit_threshold INTEGER DEFAULT 0,
                    low_credit_threshold INTEGER DEFAULT 100,
                    image_slot_wait_timeout REAL DEFAULT 120,
                    video_slot_wait_timeout REAL DEFAULT 120,
                    active_token_credit_refresh_interval_seconds INTEGER DEFAULT 900,
                    rate_limit_auto_unban_hours INTEGER DEFAULT 12,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Cache config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS cache_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    cache_enabled BOOLEAN DEFAULT 0,
                    cache_timeout INTEGER DEFAULT 7200,
                    cache_base_url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Debug config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS debug_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    enabled BOOLEAN DEFAULT 0,
                    log_requests BOOLEAN DEFAULT 1,
                    log_responses BOOLEAN DEFAULT 1,
                    mask_token BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Captcha config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS captcha_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    captcha_method TEXT DEFAULT 'browser',
                    yescaptcha_api_key TEXT DEFAULT '',
                    yescaptcha_base_url TEXT DEFAULT 'https://api.yescaptcha.com',
                    yescaptcha_task_type TEXT DEFAULT 'RecaptchaV3TaskProxylessM1',
                    capmonster_api_key TEXT DEFAULT '',
                    capmonster_base_url TEXT DEFAULT 'https://api.capmonster.cloud',
                    ezcaptcha_api_key TEXT DEFAULT '',
                    ezcaptcha_base_url TEXT DEFAULT 'https://api.ez-captcha.com',
                    capsolver_api_key TEXT DEFAULT '',
                    capsolver_base_url TEXT DEFAULT 'https://api.capsolver.com',
                    website_key TEXT DEFAULT '6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV',
                    page_action TEXT DEFAULT 'IMAGE_GENERATION',

                    browser_proxy_enabled BOOLEAN DEFAULT 0,
                    browser_proxy_url TEXT,
                    browser_count INTEGER DEFAULT 1,
                    personal_project_pool_size INTEGER DEFAULT 4,
                    personal_max_resident_tabs INTEGER DEFAULT 5,
                    browser_personal_fresh_restart_every_n_solves INTEGER DEFAULT 10,
                    personal_idle_tab_ttl_seconds INTEGER DEFAULT 600,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Plugin config table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS plugin_config (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    connection_token TEXT DEFAULT '',
                    auto_enable_on_update BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS browser_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER UNIQUE,
                    expected_email TEXT,
                    proxy_binding TEXT,
                    storage_path TEXT,
                    profile_type TEXT DEFAULT 'chrome_local',
                    last_known_project_id TEXT,
                    health_status TEXT DEFAULT 'provisioned',
                    notes TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS worker_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    worker_node_id TEXT UNIQUE NOT NULL,
                    machine_label TEXT,
                    host TEXT,
                    platform TEXT,
                    max_slots INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'online',
                    last_heartbeat_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS worker_slots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_id TEXT UNIQUE NOT NULL,
                    worker_node_id TEXT,
                    route_key TEXT,
                    client_label TEXT,
                    profile_id TEXT,
                    current_email TEXT,
                    page_url TEXT,
                    project_id TEXT,
                    session_state TEXT,
                    worker_mode TEXT,
                    busy BOOLEAN DEFAULT 0,
                    job_type TEXT,
                    last_seen_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (worker_node_id) REFERENCES worker_nodes(worker_node_id),
                    FOREIGN KEY (profile_id) REFERENCES browser_profiles(profile_id)
                )
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS worker_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE NOT NULL,
                    token_id INTEGER,
                    profile_id TEXT,
                    slot_id TEXT,
                    route_key TEXT,
                    job_type TEXT NOT NULL,
                    status TEXT DEFAULT 'queued',
                    request_payload TEXT,
                    result_payload TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    FOREIGN KEY (token_id) REFERENCES tokens(id),
                    FOREIGN KEY (profile_id) REFERENCES browser_profiles(profile_id),
                    FOREIGN KEY (slot_id) REFERENCES worker_slots(slot_id)
                )
            """)

            # Create indexes
            await db.execute("CREATE INDEX IF NOT EXISTS idx_task_id ON tasks(task_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_batch_jobs_job_id ON batch_jobs(job_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_batch_jobs_status_created_at ON batch_jobs(status, created_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_batch_job_items_job_id ON batch_job_items(job_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_batch_job_items_job_status_row ON batch_job_items(job_id, status, row_index)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_st ON tokens(st)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_project_id ON projects(project_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_tokens_email ON tokens(email)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_browser_profiles_token_id ON browser_profiles(token_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_browser_profiles_expected_email ON browser_profiles(expected_email)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_worker_slots_route_key ON worker_slots(route_key)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_worker_slots_profile_id ON worker_slots(profile_id)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_worker_jobs_status_created_at ON worker_jobs(status, created_at DESC)")

            # Migrate request_logs table if needed
            await self._migrate_request_logs(db)

            # Request logs query indexes (列表按 created_at 排序 / token 过滤)
            await db.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_created_at ON request_logs(created_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_request_logs_token_id_created_at ON request_logs(token_id, created_at DESC)")

            # Token stats lookup index
            await db.execute("CREATE INDEX IF NOT EXISTS idx_token_stats_token_id ON token_stats(token_id)")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_token_stats_token_id_unique ON token_stats(token_id)")

            await db.commit()

    async def _migrate_request_logs(self, db):
        """Migrate request_logs table from old schema to new schema"""
        try:
            has_model = await self._column_exists(db, "request_logs", "model")
            has_operation = await self._column_exists(db, "request_logs", "operation")

            if has_model and not has_operation:
                print("?? ?????request_logs???,????...")
                await db.execute("ALTER TABLE request_logs RENAME TO request_logs_old")
                await db.execute("""
                    CREATE TABLE request_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_id INTEGER,
                        operation TEXT NOT NULL,
                        request_body TEXT,
                        response_body TEXT,
                        status_code INTEGER NOT NULL,
                        duration FLOAT NOT NULL,
                        status_text TEXT DEFAULT '',
                        progress INTEGER DEFAULT 0,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (token_id) REFERENCES tokens(id)
                    )
                """)
                await db.execute("""
                    INSERT INTO request_logs (token_id, operation, request_body, status_code, duration, status_text, progress, created_at, updated_at)
                    SELECT
                        token_id,
                        model as operation,
                        json_object('model', model, 'prompt', substr(prompt, 1, 100)) as request_body,
                        CASE
                            WHEN status = 'completed' THEN 200
                            WHEN status = 'failed' THEN 500
                            ELSE 102
                        END as status_code,
                        response_time as duration,
                        CASE
                            WHEN status = 'completed' THEN 'completed'
                            WHEN status = 'failed' THEN 'failed'
                            ELSE 'processing'
                        END as status_text,
                        CASE
                            WHEN status = 'completed' THEN 100
                            WHEN status = 'failed' THEN 0
                            ELSE 0
                        END as progress,
                        created_at,
                        created_at
                    FROM request_logs_old
                """)
                await db.execute("DROP TABLE request_logs_old")
                print("? request_logs?????")

            if not await self._column_exists(db, "request_logs", "status_text"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN status_text TEXT DEFAULT ''")
            if not await self._column_exists(db, "request_logs", "progress"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN progress INTEGER DEFAULT 0")
            if not await self._column_exists(db, "request_logs", "updated_at"):
                await db.execute("ALTER TABLE request_logs ADD COLUMN updated_at TIMESTAMP")
            await db.execute("UPDATE request_logs SET updated_at = created_at WHERE updated_at IS NULL")
        except Exception as e:
            print(f"?? request_logs?????: {e}")
            # Continue even if migration fails

    # Token operations
    async def add_token(self, token: Token) -> int:
        """Add a new token"""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO tokens (st, at, at_expires, email, name, remark, is_active,
                                   credits, user_paygate_tier, current_project_id, current_project_name,
                                   image_enabled, video_enabled, image_concurrency, video_concurrency,
                                   captcha_proxy_url, extension_route_key, automation_risk_score,
                                   automation_risk_state, automation_cooldown_until,
                                   automation_last_risk_reason, automation_last_risk_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (token.st, token.at, token.at_expires, token.email, token.name, token.remark,
                  token.is_active, token.credits, token.user_paygate_tier,
                  token.current_project_id, token.current_project_name,
                  token.image_enabled, token.video_enabled,
                  token.image_concurrency, token.video_concurrency,
                  token.captcha_proxy_url, token.extension_route_key,
                  token.automation_risk_score, token.automation_risk_state,
                  token.automation_cooldown_until, token.automation_last_risk_reason,
                  token.automation_last_risk_at))
            await db.commit()
            token_id = cursor.lastrowid

            # Create stats entry
            await db.execute("""
                INSERT INTO token_stats (token_id) VALUES (?)
            """, (token_id,))
            await db.commit()

            return token_id

    async def get_token(self, token_id: int) -> Optional[Token]:
        """Get token by ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return self._token_from_row(row)
            return None

    async def get_token_by_st(self, st: str) -> Optional[Token]:
        """Get token by ST"""
        if not str(st or "").strip():
            return None
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE st = ?", (st,))
            row = await cursor.fetchone()
            if row:
                return self._token_from_row(row)
            return None

    async def get_token_by_email(self, email: str) -> Optional[Token]:
        """Get token by email"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE email = ?", (email,))
            row = await cursor.fetchone()
            if row:
                return self._token_from_row(row)
            return None

    async def get_all_tokens(self) -> List[Token]:
        """Get all tokens"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens ORDER BY created_at DESC")
            rows = await cursor.fetchall()
            return [self._token_from_row(row) for row in rows]

    async def get_all_tokens_with_stats(self) -> List[Dict[str, Any]]:
        """Get all tokens with merged statistics in one query"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            today = self._current_stats_date()
            cursor = await db.execute("""
                SELECT
                    t.*,
                    COALESCE(ts.image_count, 0) AS image_count,
                    COALESCE(ts.video_count, 0) AS video_count,
                    COALESCE(ts.error_count, 0) AS error_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_image_count ELSE 0 END, 0) AS today_image_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_video_count ELSE 0 END, 0) AS today_video_count,
                    COALESCE(CASE WHEN ts.today_date = ? THEN ts.today_error_count ELSE 0 END, 0) AS today_error_count,
                    COALESCE(ts.consecutive_error_count, 0) AS consecutive_error_count,
                    ts.last_error_at AS last_error_at
                FROM tokens t
                LEFT JOIN token_stats ts
                  ON ts.id = (
                      SELECT ts2.id
                      FROM token_stats ts2
                      WHERE ts2.token_id = t.id
                      ORDER BY ts2.id DESC
                      LIMIT 1
                  )
                ORDER BY t.created_at DESC
            """, (today, today, today))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_dashboard_stats(self) -> Dict[str, int]:
        """Get dashboard counters with aggregated SQL queries"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            today = self._current_stats_date()

            token_cursor = await db.execute("""
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens
                FROM tokens
            """)
            token_row = await token_cursor.fetchone()

            stats_cursor = await db.execute("""
                SELECT
                    COALESCE(SUM(image_count), 0) AS total_images,
                    COALESCE(SUM(video_count), 0) AS total_videos,
                    COALESCE(SUM(error_count), 0) AS total_errors,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_image_count ELSE 0 END), 0) AS today_images,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_video_count ELSE 0 END), 0) AS today_videos,
                    COALESCE(SUM(CASE WHEN today_date = ? THEN today_error_count ELSE 0 END), 0) AS today_errors
                FROM token_stats
            """, (today, today, today))
            stats_row = await stats_cursor.fetchone()

            token_data = dict(token_row) if token_row else {}
            stats_data = dict(stats_row) if stats_row else {}

            return {
                "total_tokens": int(token_data.get("total_tokens") or 0),
                "active_tokens": int(token_data.get("active_tokens") or 0),
                "total_images": int(stats_data.get("total_images") or 0),
                "total_videos": int(stats_data.get("total_videos") or 0),
                "total_errors": int(stats_data.get("total_errors") or 0),
                "today_images": int(stats_data.get("today_images") or 0),
                "today_videos": int(stats_data.get("today_videos") or 0),
                "today_errors": int(stats_data.get("today_errors") or 0)
            }

    async def get_system_info_stats(self) -> Dict[str, int]:
        """Get lightweight system counters used by admin dashboard"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("""
                SELECT
                    COUNT(*) AS total_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active_tokens,
                    COALESCE(SUM(CASE WHEN is_active = 1 THEN credits ELSE 0 END), 0) AS total_credits
                FROM tokens
            """)
            row = await cursor.fetchone()
            data = dict(row) if row else {}
            return {
                "total_tokens": int(data.get("total_tokens") or 0),
                "active_tokens": int(data.get("active_tokens") or 0),
                "total_credits": int(data.get("total_credits") or 0)
            }

    async def get_active_tokens(self) -> List[Token]:
        """Get all active tokens"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tokens WHERE is_active = 1 ORDER BY last_used_at ASC")
            rows = await cursor.fetchall()
            return [self._token_from_row(row) for row in rows]

    async def sync_browser_profiles_from_tokens(self) -> int:
        """Ensure each token has a default browser profile asset row."""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            token_rows = await (await db.execute("""
                SELECT id, email, captcha_proxy_url, current_project_id, is_active, extension_route_key
                FROM tokens
                ORDER BY id ASC
            """)).fetchall()
            existing_rows = await (await db.execute("""
                SELECT token_id, profile_id FROM browser_profiles WHERE token_id IS NOT NULL
            """)).fetchall()
            existing_by_token = {row["token_id"]: row["profile_id"] for row in existing_rows}

            changed = 0
            for row in token_rows:
                token_id = row["id"]
                route_key_hint = str(row["extension_route_key"] or "").strip()
                notes = f"route_hint={route_key_hint}" if route_key_hint else None
                health_status = "provisioned" if bool(row["is_active"]) else "inactive"
                if token_id in existing_by_token:
                    await db.execute("""
                        UPDATE browser_profiles
                        SET expected_email = ?,
                            proxy_binding = ?,
                            last_known_project_id = ?,
                            health_status = ?,
                            updated_at = CURRENT_TIMESTAMP,
                            notes = COALESCE(?, notes)
                        WHERE token_id = ?
                    """, (
                        row["email"],
                        row["captcha_proxy_url"],
                        row["current_project_id"],
                        health_status,
                        notes,
                        token_id,
                    ))
                else:
                    await db.execute("""
                        INSERT INTO browser_profiles (
                            profile_id,
                            token_id,
                            expected_email,
                            proxy_binding,
                            profile_type,
                            last_known_project_id,
                            health_status,
                            notes
                        ) VALUES (?, ?, ?, ?, 'chrome_local', ?, ?, ?)
                    """, (
                        f"token-{token_id}",
                        token_id,
                        row["email"],
                        row["captcha_proxy_url"],
                        row["current_project_id"],
                        health_status,
                        notes,
                    ))
                changed += 1

            await db.commit()
            return changed

    async def get_browser_profile_by_token_id(self, token_id: int) -> Optional[BrowserProfile]:
        """Get the browser profile asset linked to a token."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM browser_profiles WHERE token_id = ?", (token_id,))
            ).fetchone()
            return BrowserProfile(**dict(row)) if row else None

    async def list_browser_profiles(self) -> List[Dict[str, Any]]:
        """List browser profile assets with token linkage."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                SELECT
                    bp.*,
                    t.email AS token_email,
                    t.is_active AS token_is_active,
                    t.current_project_id AS token_current_project_id,
                    t.extension_route_key AS token_extension_route_key
                FROM browser_profiles bp
                LEFT JOIN tokens t ON t.id = bp.token_id
                ORDER BY bp.created_at DESC, bp.profile_id ASC
            """)).fetchall()
            return [dict(row) for row in rows]

    async def upsert_worker_node(
        self,
        *,
        worker_node_id: str,
        machine_label: Optional[str] = None,
        host: Optional[str] = None,
        platform: Optional[str] = None,
        max_slots: int = 1,
        status: str = "online",
        last_heartbeat_at: Optional[datetime] = None,
    ) -> None:
        """Create or update a worker node."""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            existing = await (
                await db.execute("SELECT id FROM worker_nodes WHERE worker_node_id = ?", (worker_node_id,))
            ).fetchone()
            if existing:
                await db.execute("""
                    UPDATE worker_nodes
                    SET machine_label = ?,
                        host = ?,
                        platform = ?,
                        max_slots = ?,
                        status = ?,
                        last_heartbeat_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE worker_node_id = ?
                """, (machine_label, host, platform, max_slots, status, last_heartbeat_at, worker_node_id))
            else:
                await db.execute("""
                    INSERT INTO worker_nodes (
                        worker_node_id,
                        machine_label,
                        host,
                        platform,
                        max_slots,
                        status,
                        last_heartbeat_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (worker_node_id, machine_label, host, platform, max_slots, status, last_heartbeat_at))
            await db.commit()

    async def upsert_worker_slot(
        self,
        *,
        slot_id: str,
        worker_node_id: Optional[str] = None,
        route_key: Optional[str] = None,
        client_label: Optional[str] = None,
        profile_id: Optional[str] = None,
        current_email: Optional[str] = None,
        page_url: Optional[str] = None,
        project_id: Optional[str] = None,
        session_state: Optional[str] = None,
        worker_mode: Optional[str] = None,
        busy: bool = False,
        job_type: Optional[str] = None,
        last_seen_at: Optional[datetime] = None,
    ) -> None:
        """Create or update a worker slot."""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            existing = await (
                await db.execute("SELECT id FROM worker_slots WHERE slot_id = ?", (slot_id,))
            ).fetchone()
            if existing:
                await db.execute("""
                    UPDATE worker_slots
                    SET worker_node_id = ?,
                        route_key = ?,
                        client_label = ?,
                        profile_id = ?,
                        current_email = ?,
                        page_url = ?,
                        project_id = ?,
                        session_state = ?,
                        worker_mode = ?,
                        busy = ?,
                        job_type = ?,
                        last_seen_at = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE slot_id = ?
                """, (
                    worker_node_id,
                    route_key,
                    client_label,
                    profile_id,
                    current_email,
                    page_url,
                    project_id,
                    session_state,
                    worker_mode,
                    busy,
                    job_type,
                    last_seen_at,
                    slot_id,
                ))
            else:
                await db.execute("""
                    INSERT INTO worker_slots (
                        slot_id,
                        worker_node_id,
                        route_key,
                        client_label,
                        profile_id,
                        current_email,
                        page_url,
                        project_id,
                        session_state,
                        worker_mode,
                        busy,
                        job_type,
                        last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    slot_id,
                    worker_node_id,
                    route_key,
                    client_label,
                    profile_id,
                    current_email,
                    page_url,
                    project_id,
                    session_state,
                    worker_mode,
                    busy,
                    job_type,
                    last_seen_at,
                ))
            await db.commit()

    async def clear_worker_slots_for_profile(self, profile_id: str) -> None:
        """Remove stale worker slot rows for a browser profile before relaunch."""
        normalized = str(profile_id or "").strip()
        if not normalized:
            return
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM worker_jobs WHERE profile_id = ?", (normalized,))
            await db.execute("DELETE FROM worker_slots WHERE profile_id = ?", (normalized,))
            await db.commit()

    async def sync_extension_routes_to_worker_slots(self, routes: List[Dict[str, Any]]) -> Dict[str, int]:
        """Project live extension route snapshots into Phase-A worker slot tables."""
        worker_node_id = "extension-local-node"
        await self.upsert_worker_node(
            worker_node_id=worker_node_id,
            machine_label="Extension Browser Workers",
            host="local-extension-cluster",
            platform="chrome-extension",
            max_slots=max(1, len(routes)),
            status="online" if routes else "idle",
            last_heartbeat_at=datetime.utcnow(),
        )

        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            profile_rows = await (await db.execute("""
                SELECT bp.profile_id, bp.expected_email, bp.token_id, t.extension_route_key
                FROM browser_profiles bp
                LEFT JOIN tokens t ON t.id = bp.token_id
            """)).fetchall()
            profiles_by_route = {
                str(row["extension_route_key"] or "").strip(): row["profile_id"]
                for row in profile_rows
                if str(row["extension_route_key"] or "").strip()
            }

            active_slot_ids: list[str] = []
            for route in routes:
                route_key = str(route.get("route_key") or "").strip()
                client_label = str(route.get("client_label") or "").strip()
                slot_id = route_key or f"anonymous:{client_label or 'extension'}"
                active_slot_ids.append(slot_id)

                assigned_tokens = route.get("assigned_tokens") or []
                profile_id = None
                for item in assigned_tokens:
                    token_id = item.get("token_id")
                    if token_id:
                        match = next((row["profile_id"] for row in profile_rows if row["token_id"] == token_id), None)
                        if match:
                            profile_id = match
                            break
                if not profile_id and route_key:
                    profile_id = profiles_by_route.get(route_key)
                last_seen_raw = route.get("last_seen_at")
                last_seen_at = None
                if isinstance(last_seen_raw, (int, float)):
                    try:
                        last_seen_at = datetime.fromtimestamp(last_seen_raw)
                    except Exception:
                        last_seen_at = None

                await db.execute("""
                    INSERT INTO worker_slots (
                        slot_id,
                        worker_node_id,
                        route_key,
                        client_label,
                        profile_id,
                        current_email,
                        page_url,
                        project_id,
                        session_state,
                        worker_mode,
                        busy,
                        job_type,
                        last_seen_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(slot_id) DO UPDATE SET
                        worker_node_id = excluded.worker_node_id,
                        route_key = excluded.route_key,
                        client_label = excluded.client_label,
                        profile_id = excluded.profile_id,
                        current_email = excluded.current_email,
                        page_url = excluded.page_url,
                        project_id = excluded.project_id,
                        session_state = excluded.session_state,
                        worker_mode = excluded.worker_mode,
                        last_seen_at = excluded.last_seen_at,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    slot_id,
                    worker_node_id,
                    route_key,
                    client_label,
                    profile_id,
                    str(route.get("current_email") or "").strip(),
                    str(route.get("page_url") or "").strip(),
                    str(route.get("project_id") or "").strip(),
                    str(route.get("session_state") or "").strip(),
                    str(route.get("worker_mode") or "").strip(),
                    last_seen_at,
                ))

            stale_slot_query = "SELECT slot_id FROM worker_slots WHERE worker_node_id = ?"
            stale_slot_params: list[Any] = [worker_node_id]
            if active_slot_ids:
                placeholders = ",".join("?" for _ in active_slot_ids)
                stale_slot_query += f" AND slot_id NOT IN ({placeholders})"
                stale_slot_params.extend(active_slot_ids)
            stale_slot_rows = await (await db.execute(stale_slot_query, stale_slot_params)).fetchall()
            stale_slot_ids = [str(row["slot_id"] or "").strip() for row in stale_slot_rows if str(row["slot_id"] or "").strip()]

            if stale_slot_ids:
                stale_placeholders = ",".join("?" for _ in stale_slot_ids)
                # Keep historical worker job logs, but detach them from ephemeral slots
                # before deleting stale worker_slots rows to satisfy the FK constraint.
                await db.execute(
                    f"UPDATE worker_jobs SET slot_id = NULL WHERE slot_id IN ({stale_placeholders})",
                    stale_slot_ids,
                )
                await db.execute(
                    f"DELETE FROM worker_slots WHERE slot_id IN ({stale_placeholders})",
                    stale_slot_ids,
                )

            await db.commit()
            return {"worker_nodes": 1, "worker_slots": len(active_slot_ids)}

    async def list_worker_nodes(self) -> List[WorkerNode]:
        """List known worker nodes."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                SELECT * FROM worker_nodes ORDER BY updated_at DESC, worker_node_id ASC
            """)).fetchall()
            return [WorkerNode(**dict(row)) for row in rows]

    async def list_worker_slots(self) -> List[Dict[str, Any]]:
        """List current worker slots with profile linkage."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                SELECT
                    ws.*,
                    bp.expected_email AS profile_expected_email,
                    bp.token_id AS profile_token_id,
                    wn.machine_label AS worker_node_label,
                    wn.status AS worker_node_status
                FROM worker_slots ws
                LEFT JOIN browser_profiles bp ON bp.profile_id = ws.profile_id
                LEFT JOIN worker_nodes wn ON wn.worker_node_id = ws.worker_node_id
                ORDER BY ws.updated_at DESC, ws.slot_id ASC
            """)).fetchall()
            return [dict(row) for row in rows]

    async def list_worker_jobs(self, limit: int = 100) -> List[WorkerJob]:
        """List recent browser worker jobs."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                SELECT * FROM worker_jobs ORDER BY created_at DESC LIMIT ?
            """, (max(1, int(limit)),))).fetchall()
            return [WorkerJob(**dict(row)) for row in rows]

    async def create_worker_job(self, job: WorkerJob) -> int:
        """Create a browser worker job record."""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO worker_jobs (
                    job_id,
                    token_id,
                    profile_id,
                    slot_id,
                    route_key,
                    job_type,
                    status,
                    request_payload,
                    result_payload,
                    error_message,
                    started_at,
                    finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job.job_id,
                job.token_id,
                job.profile_id,
                job.slot_id,
                job.route_key,
                job.job_type,
                job.status,
                job.request_payload,
                job.result_payload,
                job.error_message,
                job.started_at,
                job.finished_at,
            ))
            await db.commit()
            return cursor.lastrowid

    async def update_worker_job(self, job_id: str, **kwargs) -> None:
        """Update a browser worker job record."""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                params.append(job_id)
                await db.execute(
                    f"UPDATE worker_jobs SET {', '.join(updates)} WHERE job_id = ?",
                    params,
                )
                await db.commit()

    async def get_worker_job(self, job_id: str) -> Optional[WorkerJob]:
        """Get a browser worker job by job_id."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM worker_jobs WHERE job_id = ?", (job_id,))
            ).fetchone()
            return WorkerJob(**dict(row)) if row else None

    async def list_token_worker_bindings(self) -> List[Dict[str, Any]]:
        """List token -> profile -> latest slot binding summaries."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                SELECT
                    t.id AS token_id,
                    t.email AS token_email,
                    t.extension_route_key AS token_extension_route_key,
                    bp.profile_id,
                    bp.health_status AS profile_health_status,
                    bp.last_known_project_id AS profile_last_known_project_id,
                    bp.proxy_binding AS profile_proxy_binding,
                    ws.slot_id,
                    ws.worker_node_id,
                    ws.route_key,
                    ws.client_label,
                    ws.current_email,
                    ws.page_url,
                    ws.project_id,
                    ws.session_state,
                    ws.worker_mode,
                    ws.busy,
                    ws.job_type,
                    ws.last_seen_at
                FROM tokens t
                LEFT JOIN browser_profiles bp ON bp.token_id = t.id
                LEFT JOIN worker_slots ws
                  ON ws.id = (
                      SELECT ws2.id
                      FROM worker_slots ws2
                      WHERE ws2.profile_id = bp.profile_id
                        AND (
                            t.extension_route_key IS NULL
                            OR t.extension_route_key = ''
                            OR ws2.route_key = t.extension_route_key
                        )
                      ORDER BY ws2.updated_at DESC, ws2.id DESC
                      LIMIT 1
                  )
                ORDER BY t.id ASC
            """)).fetchall()
            return [dict(row) for row in rows]

    async def get_token_worker_binding(self, token_id: int) -> Optional[Dict[str, Any]]:
        """Get the latest profile/slot binding summary for a token."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("""
                SELECT
                    t.id AS token_id,
                    t.email AS token_email,
                    t.extension_route_key AS token_extension_route_key,
                    bp.profile_id,
                    bp.health_status AS profile_health_status,
                    bp.last_known_project_id AS profile_last_known_project_id,
                    bp.proxy_binding AS profile_proxy_binding,
                    ws.slot_id,
                    ws.worker_node_id,
                    ws.route_key,
                    ws.client_label,
                    ws.current_email,
                    ws.page_url,
                    ws.project_id,
                    ws.session_state,
                    ws.worker_mode,
                    ws.busy,
                    ws.job_type,
                    ws.last_seen_at
                FROM tokens t
                LEFT JOIN browser_profiles bp ON bp.token_id = t.id
                LEFT JOIN worker_slots ws
                  ON ws.id = (
                      SELECT ws2.id
                      FROM worker_slots ws2
                      WHERE ws2.profile_id = bp.profile_id
                        AND (
                            t.extension_route_key IS NULL
                            OR t.extension_route_key = ''
                            OR ws2.route_key = t.extension_route_key
                        )
                      ORDER BY ws2.updated_at DESC, ws2.id DESC
                      LIMIT 1
                  )
                WHERE t.id = ?
                LIMIT 1
            """, (token_id,))).fetchone()
            return dict(row) if row else None

    async def update_token(self, token_id: int, **kwargs):
        """Update token fields"""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                updates.append(f"{key} = ?")
                params.append(value)

            if updates:
                params.append(token_id)
                query = f"UPDATE tokens SET {', '.join(updates)} WHERE id = ?"
                await db.execute(query, params)
                await db.commit()

    async def delete_token(self, token_id: int):
        """Delete token and related data"""
        async with self._connect(write=True) as db:
            cursor = await db.execute(
                "SELECT profile_id FROM browser_profiles WHERE token_id = ?",
                (token_id,),
            )
            profile_rows = await cursor.fetchall()
            profile_ids = [str(row[0] or "").strip() for row in profile_rows if str(row[0] or "").strip()]

            slot_ids: list[str] = []
            if profile_ids:
                placeholders = ",".join("?" for _ in profile_ids)
                cursor = await db.execute(
                    f"SELECT slot_id FROM worker_slots WHERE profile_id IN ({placeholders})",
                    tuple(profile_ids),
                )
                slot_rows = await cursor.fetchall()
                slot_ids = [str(row[0] or "").strip() for row in slot_rows if str(row[0] or "").strip()]

            current_step = "update_request_logs"
            try:
                await db.execute("UPDATE request_logs SET token_id = NULL WHERE token_id = ?", (token_id,))
                current_step = "delete_worker_jobs_by_token"
                await db.execute("DELETE FROM worker_jobs WHERE token_id = ?", (token_id,))
                if profile_ids:
                    placeholders = ",".join("?" for _ in profile_ids)
                    current_step = "delete_worker_jobs_by_profile"
                    await db.execute(
                        f"DELETE FROM worker_jobs WHERE profile_id IN ({placeholders})",
                        tuple(profile_ids),
                    )
                if slot_ids:
                    placeholders = ",".join("?" for _ in slot_ids)
                    current_step = "delete_worker_jobs_by_slot"
                    await db.execute(
                        f"DELETE FROM worker_jobs WHERE slot_id IN ({placeholders})",
                        tuple(slot_ids),
                    )
                if profile_ids:
                    placeholders = ",".join("?" for _ in profile_ids)
                    current_step = "delete_worker_slots_by_profile"
                    await db.execute(
                        f"DELETE FROM worker_slots WHERE profile_id IN ({placeholders})",
                        tuple(profile_ids),
                    )
                current_step = "delete_browser_profiles"
                await db.execute("DELETE FROM browser_profiles WHERE token_id = ?", (token_id,))
                current_step = "delete_tasks"
                await db.execute("DELETE FROM tasks WHERE token_id = ?", (token_id,))
                current_step = "delete_token_stats"
                await db.execute("DELETE FROM token_stats WHERE token_id = ?", (token_id,))
                current_step = "delete_projects"
                await db.execute("DELETE FROM projects WHERE token_id = ?", (token_id,))
                current_step = "delete_tokens"
                await db.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
                current_step = "commit"
                await db.commit()
            except Exception as e:
                raise

    # Project operations
    async def add_project(self, project: Project) -> int:
        """Add a new project"""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO projects (project_id, token_id, project_name, tool_name, is_active)
                VALUES (?, ?, ?, ?, ?)
            """, (project.project_id, project.token_id, project.project_name,
                  project.tool_name, project.is_active))
            await db.commit()
            return cursor.lastrowid

    async def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """Get project by UUID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,))
            row = await cursor.fetchone()
            if row:
                return Project(**dict(row))
            return None

    async def get_projects_by_token(self, token_id: int) -> List[Project]:
        """Get all projects for a token"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM projects WHERE token_id = ? ORDER BY created_at DESC",
                (token_id,)
            )
            rows = await cursor.fetchall()
            return [Project(**dict(row)) for row in rows]

    async def delete_project(self, project_id: str):
        """Delete project"""
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
            await db.commit()

    # Task operations
    async def create_task(self, task: Task) -> int:
        """Create a new task"""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO tasks (task_id, token_id, model, prompt, status, progress, scene_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (task.task_id, task.token_id, task.model, task.prompt,
                  task.status, task.progress, task.scene_id))
            await db.commit()
            return cursor.lastrowid

    async def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = await cursor.fetchone()
            if row:
                task_dict = dict(row)
                # Parse result_urls from JSON
                if task_dict.get("result_urls"):
                    task_dict["result_urls"] = json.loads(task_dict["result_urls"])
                return Task(**task_dict)
            return None

    async def update_task(self, task_id: str, **kwargs):
        """Update task"""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    # Convert list to JSON string for result_urls
                    if key == "result_urls" and isinstance(value, list):
                        value = json.dumps(value)
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                params.append(task_id)
                query = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?"
                await db.execute(query, params)
                await db.commit()

    async def create_batch_job(self, job: BatchJob, items: List[BatchJobItem]) -> int:
        """Create a batch job and all its items."""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO batch_jobs (
                    job_id, source_type, file_name, raw_payload_text, status,
                    total_count, pending_count, running_count, success_count, failed_count,
                    created_by, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job.job_id,
                job.source_type,
                job.file_name,
                job.raw_payload_text,
                job.status,
                job.total_count,
                job.pending_count,
                job.running_count,
                job.success_count,
                job.failed_count,
                job.created_by,
                job.started_at,
                job.finished_at,
            ))

            for item in items:
                await db.execute("""
                    INSERT INTO batch_job_items (
                        job_id, row_index, row_id, task_type, normalized_payload, status,
                        error_code, error_message, result_media_id, result_url,
                        token_id, project_id, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    item.job_id,
                    item.row_index,
                    item.row_id,
                    item.task_type,
                    self._dumps_json_field(item.normalized_payload),
                    item.status,
                    item.error_code,
                    item.error_message,
                    item.result_media_id,
                    item.result_url,
                    item.token_id,
                    item.project_id,
                    item.started_at,
                    item.finished_at,
                ))

            await db.commit()
            return cursor.lastrowid

    async def list_batch_jobs(self, limit: int = 50) -> List[BatchJob]:
        """List recent batch jobs."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                SELECT * FROM batch_jobs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
            """, (max(1, int(limit)),))).fetchall()
            return [self._batch_job_from_row(row) for row in rows]

    async def get_batch_job(self, job_id: str) -> Optional[BatchJob]:
        """Get a batch job by business ID."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute("SELECT * FROM batch_jobs WHERE job_id = ?", (job_id,))
            ).fetchone()
            return self._batch_job_from_row(row) if row else None

    async def delete_batch_job(self, job_id: str) -> int:
        async with self._connect(write=True) as db:
            cursor = await db.execute("DELETE FROM batch_jobs WHERE job_id = ?", (job_id,))
            await db.commit()
            return int(getattr(cursor, "rowcount", 0) or 0)

    async def list_batch_job_items(self, job_id: str) -> List[BatchJobItem]:
        """List batch items for a job."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                SELECT * FROM batch_job_items
                WHERE job_id = ?
                ORDER BY row_index ASC, id ASC
            """, (job_id,))).fetchall()
            return [self._batch_job_item_from_row(row) for row in rows]

    async def update_batch_job(self, job_id: str, **kwargs) -> None:
        """Update batch job fields."""
        async with self._connect(write=True) as db:
            updates = []
            params = []
            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    params.append(value)

            if not updates:
                return

            params.append(job_id)
            await db.execute(
                f"UPDATE batch_jobs SET {', '.join(updates)} WHERE job_id = ?",
                params,
            )
            await db.commit()

    async def update_batch_job_item(self, item_id: int, **kwargs) -> None:
        """Update batch item fields."""
        async with self._connect(write=True) as db:
            updates = []
            params = []
            for key, value in kwargs.items():
                if value is None:
                    continue
                if key == "normalized_payload":
                    value = self._dumps_json_field(value)
                updates.append(f"{key} = ?")
                params.append(value)

            if not updates:
                return

            params.append(item_id)
            await db.execute(
                f"UPDATE batch_job_items SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            await db.commit()

    async def refresh_batch_job_counts(self, job_id: str) -> Dict[str, int]:
        """Recalculate persisted status counters for a batch job from its items."""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute("""
                SELECT status, COUNT(*) AS count
                FROM batch_job_items
                WHERE job_id = ?
                GROUP BY status
            """, (job_id,))).fetchall()
            counts = {str(row["status"] or ""): int(row["count"] or 0) for row in rows}
            pending_count = counts.get("pending", 0) + counts.get("queued", 0)
            running_count = counts.get("running", 0)
            success_count = counts.get("succeeded", 0)
            failed_count = counts.get("failed", 0) + counts.get("cancelled", 0)
            total_count = pending_count + running_count + success_count + failed_count

            await db.execute("""
                UPDATE batch_jobs
                SET total_count = ?,
                    pending_count = ?,
                    running_count = ?,
                    success_count = ?,
                    failed_count = ?
                WHERE job_id = ?
            """, (
                total_count,
                pending_count,
                running_count,
                success_count,
                failed_count,
                job_id,
            ))
            await db.commit()

            return {
                "total_count": total_count,
                "pending_count": pending_count,
                "running_count": running_count,
                "success_count": success_count,
                "failed_count": failed_count,
            }

    # Token stats operations (kept for compatibility, now delegates to specific methods)
    async def increment_token_stats(self, token_id: int, stat_type: str):
        """Increment token statistics (delegates to specific methods)"""
        if stat_type == "image":
            await self.increment_image_count(token_id)
        elif stat_type == "video":
            await self.increment_video_count(token_id)
        elif stat_type == "error":
            await self.increment_error_count(token_id)

    async def get_token_stats(self, token_id: int) -> Optional[TokenStats]:
        """Get token statistics"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()
            if row:
                return TokenStats(**dict(row))
            return None

    async def increment_image_count(self, token_id: int):
        """Increment image generation count with daily reset"""
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's image usage.
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = 1,
                        today_video_count = 0,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET image_count = image_count + 1,
                        today_image_count = today_image_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_video_count(self, token_id: int):
        """Increment video generation count with daily reset"""
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's video usage.
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_image_count = 0,
                        today_video_count = 1,
                        today_error_count = 0,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment both
                await db.execute("""
                    UPDATE token_stats
                    SET video_count = video_count + 1,
                        today_video_count = today_video_count + 1,
                        today_date = ?
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def increment_error_count(self, token_id: int):
        """Increment error count with daily reset

        Updates two counters:
        - error_count: Historical total errors (never reset)
        - consecutive_error_count: Consecutive errors (reset on success/enable)
        - today_error_count: Today's errors (reset on date change)
        """
        async with self._connect(write=True) as db:
            today = self._current_stats_date()
            # Get current stats
            cursor = await db.execute("SELECT today_date FROM token_stats WHERE token_id = ?", (token_id,))
            row = await cursor.fetchone()

            # If date changed, reset all daily counters before recording today's error.
            if row and row[0] != today:
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_image_count = 0,
                        today_video_count = 0,
                        today_error_count = 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            else:
                # Same day, just increment all counters
                await db.execute("""
                    UPDATE token_stats
                    SET error_count = error_count + 1,
                        consecutive_error_count = consecutive_error_count + 1,
                        today_error_count = today_error_count + 1,
                        today_date = ?,
                        last_error_at = CURRENT_TIMESTAMP
                    WHERE token_id = ?
                """, (today, token_id))
            await db.commit()

    async def reset_error_count(self, token_id: int):
        """Reset consecutive error count (only reset consecutive_error_count, keep error_count and today_error_count)

        This is called when:
        - Token is manually enabled by admin
        - Request succeeds (resets consecutive error counter)

        Note: error_count (total historical errors) is NEVER reset
        """
        async with self._connect(write=True) as db:
            await db.execute("""
                UPDATE token_stats SET consecutive_error_count = 0 WHERE token_id = ?
            """, (token_id,))
            await db.commit()

    # Config operations
    async def get_admin_config(self) -> Optional[AdminConfig]:
        """Get admin configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM admin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return AdminConfig(**dict(row))
            return None

    async def update_admin_config(self, **kwargs):
        """Update admin configuration"""
        async with self._connect(write=True) as db:
            updates = []
            params = []

            for key, value in kwargs.items():
                if value is not None:
                    updates.append(f"{key} = ?")
                    params.append(value)

            if updates:
                updates.append("updated_at = CURRENT_TIMESTAMP")
                query = f"UPDATE admin_config SET {', '.join(updates)} WHERE id = 1"
                await db.execute(query, params)
                await db.commit()

    async def get_proxy_config(self) -> Optional[ProxyConfig]:
        """Get proxy configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return ProxyConfig(**dict(row))
            return None

    async def update_proxy_config(
        self,
        enabled: bool,
        proxy_url: Optional[str] = None,
        media_proxy_enabled: Optional[bool] = None,
        media_proxy_url: Optional[str] = None
    ):
        """Update proxy configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM proxy_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_media_proxy_enabled = (
                    media_proxy_enabled
                    if media_proxy_enabled is not None
                    else current.get("media_proxy_enabled", False)
                )
                new_media_proxy_url = (
                    media_proxy_url
                    if media_proxy_url is not None
                    else current.get("media_proxy_url")
                )

                await db.execute("""
                    UPDATE proxy_config
                    SET enabled = ?, proxy_url = ?,
                        media_proxy_enabled = ?, media_proxy_url = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url))
            else:
                new_media_proxy_enabled = media_proxy_enabled if media_proxy_enabled is not None else False
                new_media_proxy_url = media_proxy_url
                await db.execute("""
                    INSERT INTO proxy_config (id, enabled, proxy_url, media_proxy_enabled, media_proxy_url)
                    VALUES (1, ?, ?, ?, ?)
                """, (enabled, proxy_url, new_media_proxy_enabled, new_media_proxy_url))

            await db.commit()

    async def get_generation_config(self) -> Optional[GenerationConfig]:
        """Get generation configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return GenerationConfig(**dict(row))
            return None

    async def update_generation_config(
        self,
        image_timeout: Optional[int] = None,
        video_timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        """Update generation configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM generation_config WHERE id = 1")
            row = await cursor.fetchone()
            current = dict(row) if row else {}

            normalized_image_timeout = (
                image_timeout
                if image_timeout is not None
                else current.get("image_timeout", 300)
            )
            normalized_video_timeout = (
                video_timeout
                if video_timeout is not None
                else current.get("video_timeout", 1500)
            )
            try:
                normalized_max_retries = (
                    max(1, int(max_retries))
                    if max_retries is not None
                    else max(1, int(current.get("max_retries", 3)))
                )
            except Exception:
                normalized_max_retries = 3

            if row:
                await db.execute("""
                    UPDATE generation_config
                    SET image_timeout = ?, video_timeout = ?, max_retries = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (normalized_image_timeout, normalized_video_timeout, normalized_max_retries))
            else:
                await db.execute("""
                    INSERT INTO generation_config (id, image_timeout, video_timeout, max_retries)
                    VALUES (1, ?, ?, ?)
                """, (normalized_image_timeout, normalized_video_timeout, normalized_max_retries))
            await db.commit()

    async def get_call_logic_config(self) -> CallLogicConfig:
        """Get token call logic configuration."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM call_logic_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                row_dict = dict(row)
                mode = row_dict.get("call_mode")
                if mode not in ("default", "polling"):
                    row_dict["call_mode"] = "polling" if row_dict.get("polling_mode_enabled") else "default"
                return CallLogicConfig(**row_dict)
            return CallLogicConfig(call_mode="default", polling_mode_enabled=False)

    async def update_call_logic_config(self, call_mode: str):
        """Update token call logic configuration."""
        normalized = "polling" if call_mode == "polling" else "default"
        polling_mode_enabled = normalized == "polling"
        async with self._connect(write=True) as db:
            await db.execute("""
                INSERT OR REPLACE INTO call_logic_config (id, call_mode, polling_mode_enabled, updated_at)
                VALUES (1, ?, ?, CURRENT_TIMESTAMP)
            """, (normalized, polling_mode_enabled))
            await db.commit()

    async def get_scheduler_config(self) -> SchedulerConfig:
        """Get centralized scheduler strategy configuration."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM scheduler_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return SchedulerConfig(**dict(row))
            return SchedulerConfig()

    async def update_scheduler_config(
        self,
        *,
        exhausted_credit_threshold: Optional[int] = None,
        low_credit_threshold: Optional[int] = None,
        image_slot_wait_timeout: Optional[float] = None,
        video_slot_wait_timeout: Optional[float] = None,
        active_token_credit_refresh_interval_seconds: Optional[int] = None,
        rate_limit_auto_unban_hours: Optional[int] = None,
    ):
        """Update centralized scheduler strategy configuration."""
        current = await self.get_scheduler_config()

        try:
            normalized_exhausted = (
                max(0, int(exhausted_credit_threshold))
                if exhausted_credit_threshold is not None
                else max(0, int(current.exhausted_credit_threshold))
            )
        except Exception:
            normalized_exhausted = 0
        try:
            normalized_low = (
                max(normalized_exhausted, int(low_credit_threshold))
                if low_credit_threshold is not None
                else max(normalized_exhausted, int(current.low_credit_threshold))
            )
        except Exception:
            normalized_low = max(normalized_exhausted, 100)
        try:
            normalized_image_wait = (
                max(1.0, min(600.0, float(image_slot_wait_timeout)))
                if image_slot_wait_timeout is not None
                else max(1.0, min(600.0, float(current.image_slot_wait_timeout)))
            )
        except Exception:
            normalized_image_wait = 120.0
        try:
            normalized_video_wait = (
                max(1.0, min(600.0, float(video_slot_wait_timeout)))
                if video_slot_wait_timeout is not None
                else max(1.0, min(600.0, float(current.video_slot_wait_timeout)))
            )
        except Exception:
            normalized_video_wait = 120.0
        try:
            normalized_credit_refresh_interval = (
                max(0, int(active_token_credit_refresh_interval_seconds))
                if active_token_credit_refresh_interval_seconds is not None
                else max(0, int(current.active_token_credit_refresh_interval_seconds))
            )
        except Exception:
            normalized_credit_refresh_interval = 900
        try:
            normalized_unban_hours = (
                max(1, int(rate_limit_auto_unban_hours))
                if rate_limit_auto_unban_hours is not None
                else max(1, int(current.rate_limit_auto_unban_hours))
            )
        except Exception:
            normalized_unban_hours = 12

        async with self._connect(write=True) as db:
            await db.execute("""
                INSERT OR REPLACE INTO scheduler_config (
                    id,
                    exhausted_credit_threshold,
                    low_credit_threshold,
                    image_slot_wait_timeout,
                    video_slot_wait_timeout,
                    active_token_credit_refresh_interval_seconds,
                    rate_limit_auto_unban_hours,
                    updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                normalized_exhausted,
                normalized_low,
                normalized_image_wait,
                normalized_video_wait,
                normalized_credit_refresh_interval,
                normalized_unban_hours,
            ))
            await db.commit()

    # Request log operations
    async def add_request_log(self, log: RequestLog) -> int:
        """Add request log and return log id"""
        async with self._connect(write=True) as db:
            cursor = await db.execute("""
                INSERT INTO request_logs (token_id, operation, request_body, response_body, status_code, duration, status_text, progress)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                log.token_id,
                log.operation,
                log.request_body,
                log.response_body,
                log.status_code,
                log.duration,
                log.status_text or "",
                log.progress,
            ))
            await db.commit()
            return cursor.lastrowid

    async def update_request_log(self, log_id: int, **kwargs):
        """Update an existing request log row."""
        if not kwargs:
            return

        allowed_fields = {
            "token_id",
            "operation",
            "request_body",
            "response_body",
            "status_code",
            "duration",
            "status_text",
            "progress",
        }
        update_fields = {key: value for key, value in kwargs.items() if key in allowed_fields}
        if not update_fields:
            return

        clauses = []
        values = []
        for key, value in update_fields.items():
            clauses.append(f"{key} = ?")
            values.append(value)
        clauses.append("updated_at = CURRENT_TIMESTAMP")
        values.append(log_id)

        async with self._connect(write=True) as db:
            await db.execute(
                f"UPDATE request_logs SET {', '.join(clauses)} WHERE id = ?",
                values,
            )
            await db.commit()

    async def get_logs(self, limit: int = 100, token_id: Optional[int] = None, include_payload: bool = False):
        """Get request logs with token info, optionally including payload fields"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            payload_columns = "rl.request_body, rl.response_body," if include_payload else ""
            response_excerpt_column = "substr(COALESCE(rl.response_body, ''), 1, 2048) as response_body_excerpt,"
            has_status_text = await self._column_exists(db, "request_logs", "status_text")
            has_progress = await self._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._column_exists(db, "request_logs", "updated_at")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"

            if token_id:
                cursor = await db.execute(f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.operation,
                        {payload_columns}
                        {response_excerpt_column}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    WHERE rl.token_id = ?
                    ORDER BY rl.created_at DESC
                    LIMIT ?
                """, (token_id, limit))
            else:
                cursor = await db.execute(f"""
                    SELECT
                        rl.id,
                        rl.token_id,
                        rl.operation,
                        {payload_columns}
                        {response_excerpt_column}
                        rl.status_code,
                        rl.duration,
                        {status_text_column}
                        {progress_column}
                        rl.created_at,
                        {updated_at_column}
                        t.email as token_email,
                        t.name as token_username
                    FROM request_logs rl
                    LEFT JOIN tokens t ON rl.token_id = t.id
                    ORDER BY rl.created_at DESC
                    LIMIT ?
                """, (limit,))

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_log_detail(self, log_id: int) -> Optional[Dict[str, Any]]:
        """Get single request log detail including payload fields"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            has_status_text = await self._column_exists(db, "request_logs", "status_text")
            has_progress = await self._column_exists(db, "request_logs", "progress")
            has_updated_at = await self._column_exists(db, "request_logs", "updated_at")
            status_text_column = "rl.status_text," if has_status_text else "'' as status_text,"
            progress_column = "rl.progress," if has_progress else "0 as progress,"
            updated_at_column = "rl.updated_at," if has_updated_at else "rl.created_at as updated_at,"
            cursor = await db.execute(f"""
                SELECT
                    rl.id,
                    rl.token_id,
                    rl.operation,
                    rl.request_body,
                    rl.response_body,
                    rl.status_code,
                    rl.duration,
                    {status_text_column}
                    {progress_column}
                    rl.created_at,
                    {updated_at_column}
                    t.email as token_email,
                    t.name as token_username
                FROM request_logs rl
                LEFT JOIN tokens t ON rl.token_id = t.id
                WHERE rl.id = ?
                LIMIT 1
            """, (log_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_all_logs(self):
        """Clear all request logs"""
        async with self._connect(write=True) as db:
            await db.execute("DELETE FROM request_logs")
            await db.commit()

    async def init_config_from_toml(self, config_dict: dict, is_first_startup: bool = True):
        """
        Initialize database configuration from setting.toml

        Args:
            config_dict: Configuration dictionary from setting.toml
            is_first_startup: If True, initialize all config rows from setting.toml.
                            If False (upgrade mode), only ensure missing config rows exist with default values.
        """
        async with self._connect(write=True) as db:
            if is_first_startup:
                # First startup: Initialize all config tables with values from setting.toml
                await self._ensure_config_rows(db, config_dict)
            else:
                # Upgrade mode: Only ensure missing config rows exist (with default values, not from TOML)
                await self._ensure_config_rows(db, config_dict=None)

            await db.commit()

    async def reload_config_to_memory(self):
        """
        Reload all configuration from database to in-memory Config instance.
        This should be called after any configuration update to ensure hot-reload.

        Includes:
        - Admin config (username, password, api_key)
        - Cache config (enabled, timeout, base_url)
        - Generation config (image_timeout, video_timeout)
        - Proxy config will be handled by ProxyManager
        """
        from .config import config

        # Reload admin config
        admin_config = await self.get_admin_config()
        if admin_config:
            config.set_admin_username_from_db(admin_config.username)
            config.set_admin_password_from_db(admin_config.password)
            config.api_key = admin_config.api_key

        # Reload cache config
        cache_config = await self.get_cache_config()
        if cache_config:
            config.set_cache_enabled(cache_config.cache_enabled)
            config.set_cache_timeout(cache_config.cache_timeout)
            config.set_cache_base_url(cache_config.cache_base_url or "")

        # Reload generation config
        generation_config = await self.get_generation_config()
        if generation_config:
            config.set_image_timeout(generation_config.image_timeout)
            config.set_video_timeout(generation_config.video_timeout)
            config.set_flow_max_retries(generation_config.max_retries)

        # Reload call logic config
        call_logic_config = await self.get_call_logic_config()
        if call_logic_config:
            config.set_call_logic_mode(call_logic_config.call_mode)

        scheduler_config = await self.get_scheduler_config()
        if scheduler_config:
            config.set_exhausted_credit_threshold(scheduler_config.exhausted_credit_threshold)
            config.set_low_credit_threshold(scheduler_config.low_credit_threshold)
            config.set_flow_image_slot_wait_timeout(scheduler_config.image_slot_wait_timeout)
            config.set_flow_video_slot_wait_timeout(scheduler_config.video_slot_wait_timeout)
            config.set_active_token_credit_refresh_interval_seconds(
                scheduler_config.active_token_credit_refresh_interval_seconds
            )
            config.set_rate_limit_auto_unban_hours(scheduler_config.rate_limit_auto_unban_hours)

        # Reload debug config
        debug_config = await self.get_debug_config()
        if debug_config:
            config.set_debug_enabled(debug_config.enabled)

        # Reload captcha config
        captcha_config = await self.get_captcha_config()
        if captcha_config:
            config.set_captcha_method(captcha_config.captcha_method)
            config.set_yescaptcha_api_key(captcha_config.yescaptcha_api_key)
            config.set_yescaptcha_base_url(captcha_config.yescaptcha_base_url)
            config.set_yescaptcha_task_type(captcha_config.yescaptcha_task_type)
            config.set_capmonster_api_key(captcha_config.capmonster_api_key)
            config.set_capmonster_base_url(captcha_config.capmonster_base_url)
            config.set_ezcaptcha_api_key(captcha_config.ezcaptcha_api_key)
            config.set_ezcaptcha_base_url(captcha_config.ezcaptcha_base_url)
            config.set_capsolver_api_key(captcha_config.capsolver_api_key)
            config.set_capsolver_base_url(captcha_config.capsolver_base_url)
            config.set_browser_count(captcha_config.browser_count)
            config.set_personal_project_pool_size(captcha_config.personal_project_pool_size)
            config.set_personal_max_resident_tabs(captcha_config.personal_max_resident_tabs)
            config.set_browser_personal_fresh_restart_every_n_solves(
                captcha_config.browser_personal_fresh_restart_every_n_solves
            )
            config.set_personal_idle_tab_ttl_seconds(captcha_config.personal_idle_tab_ttl_seconds)

    # Cache config operations
    async def get_cache_config(self) -> CacheConfig:
        """Get cache configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return CacheConfig(**dict(row))
            # Return default if not found
            return CacheConfig(cache_enabled=False, cache_timeout=7200)

    async def update_cache_config(self, enabled: bool = None, timeout: int = None, base_url: Optional[str] = None):
        """Update cache configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM cache_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("cache_enabled", False)
                new_timeout = timeout if timeout is not None else current.get("cache_timeout", 7200)
                new_base_url = base_url if base_url is not None else current.get("cache_base_url")

                # If base_url is explicitly set to empty string, treat as None
                if base_url == "":
                    new_base_url = None

                await db.execute("""
                    UPDATE cache_config
                    SET cache_enabled = ?, cache_timeout = ?, cache_base_url = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_enabled, new_timeout, new_base_url))
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_timeout = timeout if timeout is not None else 7200
                new_base_url = base_url if base_url is not None else None

                await db.execute("""
                    INSERT INTO cache_config (id, cache_enabled, cache_timeout, cache_base_url)
                    VALUES (1, ?, ?, ?)
                """, (new_enabled, new_timeout, new_base_url))

            await db.commit()

    # Debug config operations
    async def get_debug_config(self) -> 'DebugConfig':
        """Get debug configuration"""
        from .models import DebugConfig
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return DebugConfig(**dict(row))
            # Return default if not found
            return DebugConfig(enabled=False, log_requests=True, log_responses=True, mask_token=True)

    async def update_debug_config(
        self,
        enabled: bool = None,
        log_requests: bool = None,
        log_responses: bool = None,
        mask_token: bool = None
    ):
        """Update debug configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            # Get current values
            cursor = await db.execute("SELECT * FROM debug_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                # Use new values if provided, otherwise keep existing
                new_enabled = enabled if enabled is not None else current.get("enabled", False)
                new_log_requests = log_requests if log_requests is not None else current.get("log_requests", True)
                new_log_responses = log_responses if log_responses is not None else current.get("log_responses", True)
                new_mask_token = mask_token if mask_token is not None else current.get("mask_token", True)

                await db.execute("""
                    UPDATE debug_config
                    SET enabled = ?, log_requests = ?, log_responses = ?, mask_token = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_enabled, new_log_requests, new_log_responses, new_mask_token))
            else:
                # Insert default row if not exists
                new_enabled = enabled if enabled is not None else False
                new_log_requests = log_requests if log_requests is not None else True
                new_log_responses = log_responses if log_responses is not None else True
                new_mask_token = mask_token if mask_token is not None else True

                await db.execute("""
                    INSERT INTO debug_config (id, enabled, log_requests, log_responses, mask_token)
                    VALUES (1, ?, ?, ?, ?)
                """, (new_enabled, new_log_requests, new_log_responses, new_mask_token))

            await db.commit()

    # Captcha config operations
    async def get_captcha_config(self) -> CaptchaConfig:
        """Get captcha configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                data = dict(row)
                if data.get("captcha_method") == "remote_browser":
                    data["captcha_method"] = "personal"
                allowed_fields = getattr(CaptchaConfig, "model_fields", None)
                if allowed_fields is None:
                    allowed_fields = getattr(CaptchaConfig, "__fields__", {})
                filtered = {key: value for key, value in data.items() if key in allowed_fields}
                return CaptchaConfig(**filtered)
            return CaptchaConfig()

    async def update_captcha_config(
        self,
        captcha_method: str = None,
        yescaptcha_api_key: str = None,
        yescaptcha_base_url: str = None,
        yescaptcha_task_type: str = None,
        capmonster_api_key: str = None,
        capmonster_base_url: str = None,
        ezcaptcha_api_key: str = None,
        ezcaptcha_base_url: str = None,
        capsolver_api_key: str = None,
        capsolver_base_url: str = None,
        browser_proxy_enabled: bool = None,
        browser_proxy_url: str = None,
        browser_count: int = None,
        personal_project_pool_size: int = None,
        personal_max_resident_tabs: int = None,
        browser_personal_fresh_restart_every_n_solves: int = None,
        personal_idle_tab_ttl_seconds: int = None
    ):
        """Update captcha configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM captcha_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                current = dict(row)
                new_method = captcha_method if captcha_method is not None else current.get("captcha_method", "yescaptcha")
                if new_method == "remote_browser":
                    new_method = "personal"
                new_yes_key = yescaptcha_api_key if yescaptcha_api_key is not None else current.get("yescaptcha_api_key", "")
                new_yes_url = yescaptcha_base_url if yescaptcha_base_url is not None else current.get("yescaptcha_base_url", "https://api.yescaptcha.com")
                new_yes_task_type = normalize_yescaptcha_task_type(
                    yescaptcha_task_type if yescaptcha_task_type is not None else current.get("yescaptcha_task_type")
                )
                new_cap_key = capmonster_api_key if capmonster_api_key is not None else current.get("capmonster_api_key", "")
                new_cap_url = capmonster_base_url if capmonster_base_url is not None else current.get("capmonster_base_url", "https://api.capmonster.cloud")
                new_ez_key = ezcaptcha_api_key if ezcaptcha_api_key is not None else current.get("ezcaptcha_api_key", "")
                new_ez_url = ezcaptcha_base_url if ezcaptcha_base_url is not None else current.get("ezcaptcha_base_url", "https://api.ez-captcha.com")
                new_cs_key = capsolver_api_key if capsolver_api_key is not None else current.get("capsolver_api_key", "")
                new_cs_url = capsolver_base_url if capsolver_base_url is not None else current.get("capsolver_base_url", "https://api.capsolver.com")
                new_proxy_enabled = browser_proxy_enabled if browser_proxy_enabled is not None else current.get("browser_proxy_enabled", False)
                new_proxy_url = browser_proxy_url if browser_proxy_url is not None else current.get("browser_proxy_url")
                new_browser_count = browser_count if browser_count is not None else current.get("browser_count", 1)
                new_personal_project_pool_size = personal_project_pool_size if personal_project_pool_size is not None else current.get("personal_project_pool_size", 4)
                new_personal_max_tabs = personal_max_resident_tabs if personal_max_resident_tabs is not None else current.get("personal_max_resident_tabs", 5)
                new_personal_fresh_restart_every = (
                    browser_personal_fresh_restart_every_n_solves
                    if browser_personal_fresh_restart_every_n_solves is not None
                    else current.get("browser_personal_fresh_restart_every_n_solves", 10)
                )
                new_personal_idle_ttl = personal_idle_tab_ttl_seconds if personal_idle_tab_ttl_seconds is not None else current.get("personal_idle_tab_ttl_seconds", 600)
                new_browser_count = max(1, min(20, int(new_browser_count)))
                new_personal_project_pool_size = max(1, min(50, int(new_personal_project_pool_size)))
                new_personal_max_tabs = max(1, min(50, int(new_personal_max_tabs)))  # 限制1-50
                new_personal_fresh_restart_every = max(0, int(new_personal_fresh_restart_every))
                new_personal_idle_ttl = max(60, int(new_personal_idle_ttl))  # 最少60秒

                await db.execute("""
                    UPDATE captcha_config
                    SET captcha_method = ?, yescaptcha_api_key = ?, yescaptcha_base_url = ?,
                        yescaptcha_task_type = ?,
                        capmonster_api_key = ?, capmonster_base_url = ?,
                        ezcaptcha_api_key = ?, ezcaptcha_base_url = ?,
                        capsolver_api_key = ?, capsolver_base_url = ?,
                        browser_proxy_enabled = ?, browser_proxy_url = ?, browser_count = ?,
                        personal_project_pool_size = ?,
                        personal_max_resident_tabs = ?,
                        browser_personal_fresh_restart_every_n_solves = ?,
                        personal_idle_tab_ttl_seconds = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (new_method, new_yes_key, new_yes_url, new_yes_task_type,
                      new_cap_key, new_cap_url,
                      new_ez_key, new_ez_url, new_cs_key, new_cs_url,
                      new_proxy_enabled, new_proxy_url, new_browser_count, new_personal_project_pool_size,
                      new_personal_max_tabs, new_personal_fresh_restart_every, new_personal_idle_ttl))
            else:
                new_method = captcha_method if captcha_method is not None else "yescaptcha"
                if new_method == "remote_browser":
                    new_method = "personal"
                new_yes_key = yescaptcha_api_key if yescaptcha_api_key is not None else ""
                new_yes_url = yescaptcha_base_url if yescaptcha_base_url is not None else "https://api.yescaptcha.com"
                new_yes_task_type = normalize_yescaptcha_task_type(yescaptcha_task_type)
                new_cap_key = capmonster_api_key if capmonster_api_key is not None else ""
                new_cap_url = capmonster_base_url if capmonster_base_url is not None else "https://api.capmonster.cloud"
                new_ez_key = ezcaptcha_api_key if ezcaptcha_api_key is not None else ""
                new_ez_url = ezcaptcha_base_url if ezcaptcha_base_url is not None else "https://api.ez-captcha.com"
                new_cs_key = capsolver_api_key if capsolver_api_key is not None else ""
                new_cs_url = capsolver_base_url if capsolver_base_url is not None else "https://api.capsolver.com"
                new_proxy_enabled = browser_proxy_enabled if browser_proxy_enabled is not None else False
                new_proxy_url = browser_proxy_url
                new_browser_count = browser_count if browser_count is not None else 1
                new_personal_project_pool_size = personal_project_pool_size if personal_project_pool_size is not None else 4
                new_personal_max_tabs = personal_max_resident_tabs if personal_max_resident_tabs is not None else 5
                new_personal_fresh_restart_every = (
                    browser_personal_fresh_restart_every_n_solves
                    if browser_personal_fresh_restart_every_n_solves is not None
                    else 10
                )
                new_personal_idle_ttl = personal_idle_tab_ttl_seconds if personal_idle_tab_ttl_seconds is not None else 600
                new_browser_count = max(1, min(20, int(new_browser_count)))
                new_personal_project_pool_size = max(1, min(50, int(new_personal_project_pool_size)))
                new_personal_max_tabs = max(1, min(50, int(new_personal_max_tabs)))
                new_personal_fresh_restart_every = max(0, int(new_personal_fresh_restart_every))
                new_personal_idle_ttl = max(60, int(new_personal_idle_ttl))

                await db.execute("""
                    INSERT INTO captcha_config (id, captcha_method, yescaptcha_api_key, yescaptcha_base_url,
                        yescaptcha_task_type,
                        capmonster_api_key, capmonster_base_url, ezcaptcha_api_key, ezcaptcha_base_url,
                        capsolver_api_key, capsolver_base_url,
                        browser_proxy_enabled, browser_proxy_url, browser_count,
                        personal_project_pool_size,
                        personal_max_resident_tabs, browser_personal_fresh_restart_every_n_solves,
                        personal_idle_tab_ttl_seconds)
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (new_method, new_yes_key, new_yes_url, new_yes_task_type,
                      new_cap_key, new_cap_url,
                      new_ez_key, new_ez_url, new_cs_key, new_cs_url,
                      new_proxy_enabled, new_proxy_url, new_browser_count, new_personal_project_pool_size,
                      new_personal_max_tabs, new_personal_fresh_restart_every, new_personal_idle_ttl))

            await db.commit()

    # Plugin config operations
    async def get_plugin_config(self) -> PluginConfig:
        """Get plugin configuration"""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()
            if row:
                return PluginConfig(**dict(row))
            return PluginConfig()

    async def update_plugin_config(self, connection_token: str, auto_enable_on_update: bool = True):
        """Update plugin configuration"""
        async with self._connect(write=True) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM plugin_config WHERE id = 1")
            row = await cursor.fetchone()

            if row:
                await db.execute("""
                    UPDATE plugin_config
                    SET connection_token = ?, auto_enable_on_update = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = 1
                """, (connection_token, auto_enable_on_update))
            else:
                await db.execute("""
                    INSERT INTO plugin_config (id, connection_token, auto_enable_on_update)
                    VALUES (1, ?, ?)
                """, (connection_token, auto_enable_on_update))

            await db.commit()
