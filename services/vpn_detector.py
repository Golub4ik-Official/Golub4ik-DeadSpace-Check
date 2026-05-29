import json
import logging
import os
import time
from typing import Dict, List, Optional, Tuple
from urllib.request import urlopen, Request

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vpn_cache.json")

BATCH_URL = "http://ip-api.com/batch?fields=query,proxy,hosting"


class VPNDetector:
    def __init__(self, cache_path: str = CACHE_FILE):
        self.cache_path = cache_path
        self._cache: Dict[str, dict] = {}
        self._load_cache()

    def _load_cache(self):
        try:
            if os.path.exists(self.cache_path):
                with open(self.cache_path, encoding="utf-8") as f:
                    self._cache = json.load(f)
        except Exception as e:
            logger.debug(f"Failed to load VPN cache: {e}")
            self._cache = {}

    def _save_cache(self):
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"Failed to save VPN cache: {e}")

    def check_ip(self, ip: str) -> dict:
        if ip in self._cache:
            return self._cache[ip]
        result = self._check_batch([ip]).get(ip, {"proxy": False, "hosting": False})
        return result

    def check_ips(self, ips: List[str]) -> Dict[str, dict]:
        uncached = [ip for ip in ips if ip not in self._cache]
        if uncached:
            results = self._check_batch(uncached)
            self._save_cache()
        return {ip: self._cache.get(ip, {"proxy": False, "hosting": False}) for ip in ips}

    def _check_batch(self, ips: List[str]) -> Dict[str, dict]:
        if not ips:
            return {}
        results: Dict[str, dict] = {}
        batch = [ip for ip in ips if ip and ip != "N/A" and self._is_public_ip(ip)]
        if not batch:
            for ip in ips:
                self._cache[ip] = {"proxy": False, "hosting": False}
            return {ip: self._cache[ip] for ip in ips}

        chunk_size = 100
        try:
            for start in range(0, len(batch), chunk_size):
                chunk = batch[start:start + chunk_size]
                data = json.dumps([{"query": ip} for ip in chunk]).encode()
                req = Request(BATCH_URL, data=data, headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=10) as resp:
                    resp_data = json.loads(resp.read().decode())
                for entry in resp_data:
                    ip = entry.get("query", "")
                    if ip:
                        entry.pop("query", None)
                        self._cache[ip] = entry
                        results[ip] = entry
                if start + chunk_size < len(batch):
                    time.sleep(1)
        except Exception as e:
            logger.warning(f"VPN check failed for {len(batch)} IPs: {e}")
            for ip in batch:
                if ip not in self._cache:
                    self._cache[ip] = {"proxy": False, "hosting": False}
                    results[ip] = self._cache[ip]

        for ip in ips:
            if ip not in self._cache:
                self._cache[ip] = {"proxy": False, "hosting": False}
                results[ip] = self._cache[ip]

        return results

    @staticmethod
    def _is_public_ip(ip: str) -> bool:
        try:
            parts = [int(p) for p in ip.split(".")]
            if len(parts) != 4:
                return False
            if parts[0] == 10:
                return False
            if parts[0] == 172 and 16 <= parts[1] <= 31:
                return False
            if parts[0] == 192 and parts[1] == 168:
                return False
            if parts[0] == 127:
                return False
            if parts[0] == 0:
                return False
            if parts[0] == 169 and parts[1] == 254:
                return False
            return True
        except (ValueError, IndexError):
            return False

    def is_vpn(self, ip: str) -> bool:
        info = self.check_ip(ip)
        return bool(info.get("proxy") or info.get("hosting"))

    def get_badge(self, ip: str) -> str:
        info = self.check_ip(ip)
        tags = []
        if info.get("proxy"):
            tags.append("VPN")
        if info.get("hosting"):
            tags.append("Хостинг")
        return " ".join(f'<span class="tag tag-red">{t}</span>' for t in tags) if tags else ""


_vpn_detector: Optional[VPNDetector] = None


def get_vpn_detector() -> VPNDetector:
    global _vpn_detector
    if _vpn_detector is None:
        _vpn_detector = VPNDetector()
    return _vpn_detector


def enrich_ips_with_vpn(ips_data: list) -> list:
    detector = get_vpn_detector()
    all_ips = []
    for entry in ips_data:
        ip = entry.get("ip") or entry.get("direct_ip_connections") or ""
        if ip and ip != "N/A":
            all_ips.append(ip)
    results = detector.check_ips(all_ips)
    for entry in ips_data:
        ip = entry.get("ip") or entry.get("direct_ip_connections") or ""
        entry["vpn_info"] = results.get(ip, {"proxy": False, "hosting": False})
    return ips_data


def enrich_report_data(data: list) -> list:
    all_ips = set()
    detector = get_vpn_detector()
    for item in data:
        typ = item.get("type", "")
        if typ == "associated_ips":
            for ip_entry in item.get("ips", []):
                ip = ip_entry.get("direct_ip_connections", "")
                if ip:
                    all_ips.add(ip)
        elif typ == "denied_login_attempts":
            for attempt in item.get("attempts", []):
                ip = attempt.get("ip_address", "")
                if ip:
                    all_ips.add(ip)
    if not all_ips:
        return data
    results = detector.check_ips(list(all_ips))
    for item in data:
        typ = item.get("type", "")
        if typ == "associated_ips":
            for ip_entry in item.get("ips", []):
                ip = ip_entry.get("direct_ip_connections", "")
                ip_entry["vpn_info"] = results.get(ip, {"proxy": False, "hosting": False})
        elif typ == "denied_login_attempts":
            for attempt in item.get("attempts", []):
                ip = attempt.get("ip_address", "")
                attempt["vpn_info"] = results.get(ip, {"proxy": False, "hosting": False})
    return data
