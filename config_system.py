import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Type, TypeVar, Optional, Dict, List

T = TypeVar('T')


def _convert_value(value: str, target_type: Type[Any]) -> Any:
    if target_type is bool:
        return value.lower() in ('1', 'true', 'yes', 'on')
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _merge_data_into(instance: T, data: Dict[str, Any]) -> None:
    for f in fields(instance):
        if f.name in data:
            raw = data[f.name]
            if is_dataclass(f.type) and isinstance(raw, dict):
                _merge_data_into(getattr(instance, f.name), raw)
            else:
                setattr(instance, f.name, raw)
        elif is_dataclass(f.type):
            nested = getattr(instance, f.name)
            _merge_data_into(nested, data)


def load_env_into(instance: T, prefix: str = '') -> None:
    for f in fields(instance):
        env_key = (prefix + f.name).upper()
        if raw := os.getenv(env_key):
            try:
                converted = _convert_value(raw, f.type)
                setattr(instance, f.name, converted)
            except Exception:
                pass
        elif is_dataclass(f.type):
            load_env_into(getattr(instance, f.name), env_key + '_')


def load_file(path: str, instance: T) -> None:
    ext = os.path.splitext(path)[1].lower()
    if ext in ('.yaml', '.yml'):
        try:
            import yaml
        except ImportError:
            raise ImportError("PyYAML is required for YAML config files")
        loader = yaml.safe_load
    elif ext == '.json':
        loader = json.load
    elif ext == '.py':
        spec = importlib.util.spec_from_file_location('_config', path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules['_config'] = module
            spec.loader.exec_module(module)
            data = {k.lower(): getattr(module, k) for k in dir(module) if k.isupper()}
            _merge_data_into(instance, data)
        return
    else:
        raise ValueError(f"Unsupported config file type: {ext}")

    with open(path, 'r') as f:
        data = loader(f)
        if not isinstance(data, dict):
            raise ValueError("Config file must contain a top-level mapping")
        _merge_data_into(instance, data)


@dataclass
class TimeThresholds:
    close_time_threshold_minutes: int = 10
    time_threshold_minutes: int = 30
    suspicious_time_threshold_minutes: int = 60
    ip_match_timedelta_minutes: int = 30


@dataclass
class LoadOptimizerConfig:
    high_latency_threshold: float = 20.0
    very_high_latency_threshold: float = 35.0
    low_latency_threshold: float = 8.0
    target_latency: float = 12.0
    min_adjustment_interval: int = 30
    max_consecutive_adjustments: int = 5


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 12
    recovery_timeout: int = 120
    half_open_max_calls: int = 3


@dataclass
class BackoffConfig:
    initial_delay: float = 2.0
    max_delay: float = 60.0
    multiplier: float = 1.8
    jitter: bool = True

@dataclass
class APIConfig:
    base_admin_url: str = "https://admin.deadspace14.net/admin"
    account_url: str = "https://account.spacestation14.com"

    operation_timeout: int = 300
    request_timeout: int = 120
    search_timeout: int = 300
    batch_timeout: int = 600
    term_timeout: int = 150

    max_concurrent_requests: int = 4

    login_retry_limit: int = 3
    cooldown_duration: int = 60

    load_optimizer: LoadOptimizerConfig = field(default_factory=LoadOptimizerConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    backoff: BackoffConfig = field(default_factory=BackoffConfig)

@dataclass
class BatchProcessingConfig:
    conservative_batch_size: int = 5
    aggressive_batch_size: int = 8
    batch_delay_base: float = 2.0
    max_batch_retries: int = 2
    batch_retry_delay_multiplier: int = 3

@dataclass
class ScanConfig:
    message_limit: int = 10
    username: Optional[str] = None
    check_ban_bypass: bool = False
    ban_bypass_pages: int = 3

    max_terms_per_scan: int = 500

    auto_ban_enabled: bool = False
    html_report_mode: bool = True
    auto_ban_reason: str = "Ban bypass detected (HWID/IP match)"
    auto_ban_minutes: int = 0
    auto_ban_min_confidence: str = "HWID_MATCH"

    bypass_search_max_depth: int = 1
    search_max_depth: int = 2
    search_limit_root: int = 3
    search_limit_level1: int = 2
    search_limit_level2: int = 1
    search_limit_default: int = 1

    search_batch_size: int = 2

    search_cache_max_size: int = 6000
    search_cache_ttl: int = 7200

    batch_processing: BatchProcessingConfig = field(default_factory=BatchProcessingConfig)


@dataclass
class RetryConfig:
    max_retries_per_batch: int = 2
    max_retries_per_term: int = 1
    retry_delay_multiplier: int = 3
    timeout_recovery_delay: int = 5
    consecutive_timeout_limit: int = 5


@dataclass
class HealthCheckConfig:
    check_interval: int = 300
    max_error_rate: float = 0.3
    min_success_rate: float = 0.7
    max_response_time: float = 30.0
    min_throughput: int = 10


@dataclass
class EmergencyConfig:
    enable_emergency_mode: bool = True
    emergency_batch_size: int = 1
    emergency_delay: float = 10.0
    emergency_timeout: int = 300
    consecutive_failures_trigger: int = 10
    high_latency_trigger: float = 60.0
    error_rate_trigger: float = 0.5


@dataclass
class ResourceManagementConfig:
    max_concurrent_searches: int = 3
    max_queue_size: int = 50
    memory_warning_threshold: int = 512
    memory_critical_threshold: int = 1024
    auto_cleanup_enabled: bool = True
    cleanup_interval: int = 3600
    cache_size_limit: int = 1000


@dataclass
class PerformanceConfig:
    latency_history_size: int = 50
    recent_latency_size: int = 10
    cache_cleanup_interval: int = 1800
    max_memory_cache_size: int = 100

    health_check: HealthCheckConfig = field(default_factory=HealthCheckConfig)

    emergency: EmergencyConfig = field(default_factory=EmergencyConfig)

    resources: ResourceManagementConfig = field(default_factory=ResourceManagementConfig)

    retry: RetryConfig = field(default_factory=RetryConfig)


@dataclass
class LoggingConfig:
    log_file: Optional[str] = None
    log_level: str = "INFO"
    log_dir: Optional[str] = None
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    use_colors: bool = True

    performance_log_interval: int = 300
    log_slow_operations: bool = True
    slow_operation_threshold: float = 15.0
    log_error_statistics: bool = True
    error_stats_interval: int = 600


@dataclass
class DiscordConfig:
    discord_user_token: str = ""
    target_channel_id: int = 0
    complaint_channel_ids: List[int] = field(default_factory=list)
    message_history_limit: int = 70000


@dataclass
class AuthConfig:
    admin_username: str = ""
    admin_password: str = ""


@dataclass
class ConfidenceLevelConfig:
    hwid_match: str = "HWID_MATCH"
    ip_very_close_time: str = "IP_VERY_CLOSE_TIME"
    ip_close_time: str = "IP_CLOSE_TIME"
    ip_moderate_time: str = "IP_MODERATE_TIME"
    ip_distant_time: str = "IP_DISTANT_TIME"
    ip_time_close_match: str = "IP_TIME_CLOSE_MATCH"
    ip_time_match: str = "IP_TIME_MATCH"
    ip_match: str = "IP_MATCH"
    no_match: str = "NO_MATCH"


@dataclass
class ReportConfig:
    html_report_filename: str = "ban_bypass_report.html"
    json_report_filename: str = "scan_report.json"
    report_dir: Optional[str] = None
    graph_format: Optional[str] = None
    graph_output: Optional[str] = None


@dataclass
class Config:
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    api: APIConfig = field(default_factory=APIConfig)
    time_thresholds: TimeThresholds = field(default_factory=TimeThresholds)
    scan: ScanConfig = field(default_factory=ScanConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    confidence_levels: ConfidenceLevelConfig = field(default_factory=ConfidenceLevelConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)

    def validate(self) -> None:
        missing = []

        if not self.discord.discord_user_token:
            missing.append('discord.token')
        if not self.discord.target_channel_id:
            missing.append('discord.target_channel_id')
        if not self.auth.admin_username or not self.auth.admin_password:
            missing.append('auth.admin_username and auth.admin_password')

        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")

        validation_errors = []

        if self.api.operation_timeout < 30:
            validation_errors.append("api.operation_timeout should be at least 30 seconds")
        if self.api.request_timeout > self.api.operation_timeout:
            validation_errors.append("api.request_timeout should not exceed operation_timeout")

        if self.scan.batch_processing.conservative_batch_size < 1:
            validation_errors.append("scan.batch_processing.conservative_batch_size must be at least 1")
        if self.scan.batch_processing.aggressive_batch_size < self.scan.batch_processing.conservative_batch_size:
            validation_errors.append("aggressive_batch_size should be >= conservative_batch_size")

        if self.api.circuit_breaker.failure_threshold < 1:
            validation_errors.append("circuit_breaker.failure_threshold must be at least 1")
        if self.api.circuit_breaker.recovery_timeout < 10:
            validation_errors.append("circuit_breaker.recovery_timeout should be at least 10 seconds")

        if self.api.load_optimizer.low_latency_threshold >= self.api.load_optimizer.high_latency_threshold:
            validation_errors.append("low_latency_threshold should be less than high_latency_threshold")

        if self.api.max_concurrent_requests < 1:
            validation_errors.append("max_concurrent_requests must be at least 1")
        if self.api.max_concurrent_requests > 50:
            validation_errors.append("max_concurrent_requests should not exceed 50 for stability")

        if self.scan.max_terms_per_scan < 10:
            validation_errors.append("max_terms_per_scan should be at least 10")

        if self.scan.search_cache_ttl < 300:
            validation_errors.append("search_cache_ttl should be at least 300 seconds")

        if validation_errors:
            raise ValueError(f"Configuration validation errors: {'; '.join(validation_errors)}")


config = Config()


def initialize(config_file: Optional[str] = None) -> Config:
    if config_file:
        load_file(config_file, config)
    load_env_into(config)
    config.validate()
    return config


def get_config() -> Config:
    return config