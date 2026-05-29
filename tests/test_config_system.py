import os
import tempfile
import json

import pytest

from config_system import (
    _convert_value,
    _merge_data_into,
    Config,
    DiscordConfig,
    AuthConfig,
    APIConfig,
    ScanConfig,
    TimeThresholds,
    LoggingConfig,
    ConfidenceLevelConfig,
    ReportConfig,
    PerformanceConfig,
    BatchProcessingConfig,
    LoadOptimizerConfig,
    CircuitBreakerConfig,
    BackoffConfig,
)


class TestConvertValue:
    def test_bool_true_variants(self):
        for v in ("1", "true", "yes", "on"):
            assert _convert_value(v, bool) is True

    def test_bool_false(self):
        for v in ("0", "false", "no", "off"):
            assert _convert_value(v, bool) is False

    def test_int(self):
        assert _convert_value("42", int) == 42

    def test_float(self):
        assert _convert_value("3.14", float) == 3.14

    def test_str_by_default(self):
        assert _convert_value("hello", str) == "hello"


class TestConfigDefaults:
    def test_discord_config_defaults(self):
        cfg = DiscordConfig()
        assert cfg.discord_user_token == ""
        assert cfg.target_channel_id == 0
        assert cfg.complaint_channel_ids == []
        assert cfg.message_history_limit == 70000

    def test_auth_config_defaults(self):
        cfg = AuthConfig()
        assert cfg.admin_username == ""
        assert cfg.admin_password == ""

    def test_api_config_defaults(self):
        cfg = APIConfig()
        assert cfg.base_admin_url == "https://admin.deadspace14.net"
        assert cfg.max_concurrent_requests == 4
        assert cfg.request_timeout == 120

    def test_scan_config_defaults(self):
        cfg = ScanConfig()
        assert cfg.message_limit == 10
        assert cfg.check_ban_bypass is False
        assert cfg.max_terms_per_scan == 500

    def test_time_thresholds_defaults(self):
        cfg = TimeThresholds()
        assert cfg.close_time_threshold_minutes == 10
        assert cfg.time_threshold_minutes == 30
        assert cfg.suspicious_time_threshold_minutes == 60

    def test_logging_config_defaults(self):
        cfg = LoggingConfig()
        assert cfg.log_level == "INFO"
        assert cfg.use_colors is True
        assert cfg.max_bytes == 10 * 1024 * 1024

    def test_confidence_level_config_defaults(self):
        cfg = ConfidenceLevelConfig()
        assert cfg.hwid_match == "HWID_MATCH"
        assert cfg.ip_match == "IP_MATCH"
        assert cfg.no_match == "NO_MATCH"

    def test_report_config_defaults(self):
        cfg = ReportConfig()
        assert cfg.html_report_filename == "ban_bypass_report.html"
        assert cfg.json_report_filename == "scan_report.json"

    def test_performance_config_nested(self):
        cfg = PerformanceConfig()
        assert cfg.health_check.check_interval == 300
        assert cfg.emergency.enable_emergency_mode is True
        assert cfg.resources.max_concurrent_searches == 3

    def test_batch_processing_defaults(self):
        cfg = BatchProcessingConfig()
        assert cfg.conservative_batch_size == 5
        assert cfg.aggressive_batch_size == 8
        assert cfg.batch_delay_base == 2.0

    def test_load_optimizer_defaults(self):
        cfg = LoadOptimizerConfig()
        assert cfg.target_latency == 12.0
        assert cfg.min_adjustment_interval == 30

    def test_circuit_breaker_defaults(self):
        cfg = CircuitBreakerConfig()
        assert cfg.failure_threshold == 12
        assert cfg.recovery_timeout == 120

    def test_backoff_defaults(self):
        cfg = BackoffConfig()
        assert cfg.initial_delay == 2.0
        assert cfg.max_delay == 60.0
        assert cfg.multiplier == 1.8
        assert cfg.jitter is True


class TestConfigValidate:
    def test_missing_required_raises(self):
        cfg = Config()
        with pytest.raises(ValueError, match="Missing required configuration"):
            cfg.validate()

    def test_minimal_valid(self):
        cfg = Config()
        cfg.discord.discord_user_token = "token123"
        cfg.discord.target_channel_id = 12345
        cfg.auth.admin_username = "admin"
        cfg.auth.admin_password = "pass"
        cfg.validate()

    def test_operation_timeout_too_low(self):
        cfg = Config()
        cfg.discord.discord_user_token = "token"
        cfg.discord.target_channel_id = 1
        cfg.auth.admin_username = "u"
        cfg.auth.admin_password = "p"
        cfg.api.operation_timeout = 15
        with pytest.raises(ValueError, match="operation_timeout"):
            cfg.validate()

    def test_request_timeout_exceeds_operation(self):
        cfg = Config()
        cfg.discord.discord_user_token = "token"
        cfg.discord.target_channel_id = 1
        cfg.auth.admin_username = "u"
        cfg.auth.admin_password = "p"
        cfg.api.operation_timeout = 60
        cfg.api.request_timeout = 120
        with pytest.raises(ValueError, match="request_timeout"):
            cfg.validate()

    def test_conservative_batch_size_too_low(self):
        cfg = Config()
        cfg.discord.discord_user_token = "token"
        cfg.discord.target_channel_id = 1
        cfg.auth.admin_username = "u"
        cfg.auth.admin_password = "p"
        cfg.scan.batch_processing.conservative_batch_size = 0
        with pytest.raises(ValueError, match="conservative_batch_size"):
            cfg.validate()

    def test_low_latency_not_less_than_high(self):
        cfg = Config()
        cfg.discord.discord_user_token = "token"
        cfg.discord.target_channel_id = 1
        cfg.auth.admin_username = "u"
        cfg.auth.admin_password = "p"
        cfg.api.load_optimizer.low_latency_threshold = 50
        cfg.api.load_optimizer.high_latency_threshold = 20
        with pytest.raises(ValueError, match="low_latency_threshold"):
            cfg.validate()

    def test_max_concurrent_requests_too_high(self):
        cfg = Config()
        cfg.discord.discord_user_token = "token"
        cfg.discord.target_channel_id = 1
        cfg.auth.admin_username = "u"
        cfg.auth.admin_password = "p"
        cfg.api.max_concurrent_requests = 100
        with pytest.raises(ValueError, match="max_concurrent_requests"):
            cfg.validate()


class TestMergeDataInto:
    def test_simple_override(self):
        cfg = Config()
        _merge_data_into(cfg, {"discord": {"discord_user_token": "test_token"}})
        assert cfg.discord.discord_user_token == "test_token"

    def test_nested_override(self):
        cfg = Config()
        data = {
            "api": {
                "load_optimizer": {"target_latency": 5.0},
                "circuit_breaker": {"failure_threshold": 5},
            }
        }
        _merge_data_into(cfg, data)
        assert cfg.api.load_optimizer.target_latency == 5.0
        assert cfg.api.circuit_breaker.failure_threshold == 5

    def test_scan_batch(self):
        cfg = Config()
        data = {"scan": {"batch_processing": {"aggressive_batch_size": 20}}}
        _merge_data_into(cfg, data)
        assert cfg.scan.batch_processing.aggressive_batch_size == 20
