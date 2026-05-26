from collections import defaultdict
from datetime import datetime, timedelta, timezone
import re
from typing import Dict, List, Any, Tuple, Optional

from models.player import Player
from services.reporting.config import TIME_ANALYSIS_THRESHOLDS, ANALYSIS_CONFIG, BOX_CHARS


def determine_owner(primary_nickname: str, nicknames: List[str], shared_with: List[str],
                    cache: Optional[Dict] = None) -> str:
    cache = cache if cache is not None else {}
    cache_key = (primary_nickname, tuple(sorted(nicknames)), tuple(sorted(shared_with)))

    if cache_key in cache:
        return cache[cache_key]

    if primary_nickname in shared_with:
        owner = primary_nickname
    else:
        found_nick_owner = next((nick for nick in nicknames if nick in shared_with), None)
        if found_nick_owner:
            owner = found_nick_owner
        elif shared_with:
            owner = shared_with[0]
        else:
            owner = "Unknown"

    cache[cache_key] = owner
    return owner


def categorize_associated_nicknames(player: Player, primary_nickname: str) -> Dict[str, Any]:
    categories: Dict[str, Any] = {
        "confirmed_alts": {
            "accounts": set(),
            "direct_hwid": defaultdict(list),
        },
        "alt_to_alt": {
            "hwid_map": defaultdict(set),
        },
        "likely_connections": [],
        "possible_connections": {
            "ip": defaultdict(int),
            "login": set(),
        },
        "other": set(),
        "time_based": {"recent": set(), "historical": set()}
    }

    categorized_nicks_master_set = {primary_nickname}
    player_all_nicks_set = set(getattr(player, 'nicknames', []))

    for hwid, nicks_on_hwid in player.associated_hwids.items():
        if primary_nickname in nicks_on_hwid:
            alts_on_this_hwid = {n for n in nicks_on_hwid if n != primary_nickname and n in player_all_nicks_set}
            if alts_on_this_hwid:
                categories["confirmed_alts"]["accounts"].update(alts_on_this_hwid)
                categories["confirmed_alts"]["direct_hwid"][hwid].extend(list(alts_on_this_hwid))
                categorized_nicks_master_set.update(alts_on_this_hwid)

    player_known_alts_excluding_primary = player_all_nicks_set - {primary_nickname}
    for hwid, nicks_on_hwid in player.associated_hwids.items():
        if primary_nickname not in nicks_on_hwid:
            alts_on_this_hwid_for_alt_to_alt = set(nicks_on_hwid) & player_known_alts_excluding_primary
            if len(alts_on_this_hwid_for_alt_to_alt) >= 2:
                categories["alt_to_alt"]["hwid_map"][hwid].update(alts_on_this_hwid_for_alt_to_alt)
                categorized_nicks_master_set.update(alts_on_this_hwid_for_alt_to_alt)

    account_connection_strength: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"strength": 0.0, "id_details": {"hwid": 0, "ip": 0}}
    )

    confirmed_alts_of_primary_set = categories["confirmed_alts"]["accounts"]
    for hwid, nicks_on_hwid in player.associated_hwids.items():

        shared_confirmed_alts_on_hwid = set(nicks_on_hwid) & confirmed_alts_of_primary_set

        if not shared_confirmed_alts_on_hwid or primary_nickname in nicks_on_hwid:
            continue

        for nick in nicks_on_hwid:
            if nick != primary_nickname and \
                    nick not in confirmed_alts_of_primary_set and \
                    nick not in categorized_nicks_master_set and \
                    nick in player_all_nicks_set:

                account_connection_strength[nick]["id_details"]["hwid"] += 1
                if len(shared_confirmed_alts_on_hwid) > 1:
                    account_connection_strength[nick]["strength"] += ANALYSIS_CONFIG['DIRECT_CONNECTION_STRENGTH']
                else:
                    account_connection_strength[nick]["strength"] += ANALYSIS_CONFIG['SINGLE_CONNECTION_STRENGTH']

    for ip, nicks_on_ip in player.associated_ips.items():
        shared_confirmed_alts_on_ip = set(nicks_on_ip) & confirmed_alts_of_primary_set
        if not shared_confirmed_alts_on_ip or primary_nickname in nicks_on_ip:
            continue

        for nick in nicks_on_ip:
            if nick != primary_nickname and \
                    nick not in confirmed_alts_of_primary_set and \
                    nick not in categorized_nicks_master_set and \
                    nick in player_all_nicks_set:
                account_connection_strength[nick]["id_details"]["ip"] += 1
                account_connection_strength[nick]["strength"] += ANALYSIS_CONFIG['IP_CONNECTION_STRENGTH']

    for nick, data in account_connection_strength.items():
        categories["likely_connections"].append({
            "nickname": nick,
            "strength_str": "Strong" if data["strength"] >= ANALYSIS_CONFIG[
                'STRONG_CONNECTION_THRESHOLD'] else "Moderate",
            "strength_value": data["strength"],
            "id_details": data["id_details"]
        })
        categorized_nicks_master_set.add(nick)

    categories["likely_connections"].sort(key=lambda x: (x["strength_value"], sum(x["id_details"].values())),
                                          reverse=True)

    possible_ip_shared_counts = defaultdict(int)
    nicks_to_add_to_master_after_ip_scan = set()

    for ip_address, nicks_on_this_ip_list in player.associated_ips.items():
        if primary_nickname in nicks_on_this_ip_list:
            for other_nick_on_ip in nicks_on_this_ip_list:
                if other_nick_on_ip != primary_nickname and \
                        other_nick_on_ip in player_all_nicks_set and \
                        other_nick_on_ip not in categorized_nicks_master_set:
                    possible_ip_shared_counts[other_nick_on_ip] += 1
                    nicks_to_add_to_master_after_ip_scan.add(other_nick_on_ip)

    for nick, count in possible_ip_shared_counts.items():
        if count > 0:
            categories["possible_connections"]["ip"][nick] = count

    categorized_nicks_master_set.update(nicks_to_add_to_master_after_ip_scan)

    if hasattr(player, 'nicknames_sources'):
        for nick, source_info in player.nicknames_sources.items():
            source_type = source_info.get('type') if isinstance(source_info, dict) else source_info
            if source_type == "login" and \
                    nick != primary_nickname and \
                    nick not in categorized_nicks_master_set and \
                    nick in player_all_nicks_set:
                categories["possible_connections"]["login"].add(nick)
                categorized_nicks_master_set.add(nick)

    if hasattr(player, 'denied_logins') and player.denied_logins:
        now = datetime.now()
        recent_threshold_dt = now - timedelta(days=TIME_ANALYSIS_THRESHOLDS['RECENT_LOGIN_DAYS'])
        historical_threshold_dt = now - timedelta(days=TIME_ANALYSIS_THRESHOLDS['HISTORICAL_LOGIN_DAYS'])

        for login_attempt in player.denied_logins:
            user_name = login_attempt.get('user_name', '')
            if not user_name or user_name == primary_nickname or \
                    user_name not in player_all_nicks_set or \
                    user_name in categorized_nicks_master_set:
                continue

            try:
                login_time_str = login_attempt.get('time')
                if login_time_str:
                    login_time_dt = datetime.strptime(login_time_str, "%Y-%m-%d %H:%M:%S")

                    added_to_time_category = False
                    if login_time_dt > recent_threshold_dt:
                        categories["time_based"]["recent"].add(user_name)
                        added_to_time_category = True
                    elif login_time_dt > historical_threshold_dt:
                        categories["time_based"]["historical"].add(user_name)
                        added_to_time_category = True

                    if added_to_time_category:
                        categorized_nicks_master_set.add(user_name)
            except ValueError:
                pass

    categories["other"] = player_all_nicks_set - categorized_nicks_master_set

    categories["confirmed_alts"]["accounts"] = sorted(list(categories["confirmed_alts"]["accounts"]))
    for hwid_val in categories["confirmed_alts"]["direct_hwid"]:
        categories["confirmed_alts"]["direct_hwid"][hwid_val] = sorted(
            list(set(categories["confirmed_alts"]["direct_hwid"][hwid_val])))

    hwid_map_sorted = {}
    for hwid_val, alts_set in categories["alt_to_alt"]["hwid_map"].items():
        hwid_map_sorted[hwid_val] = sorted(list(alts_set))
    categories["alt_to_alt"]["hwid_map"] = {k: v for k, v in sorted(hwid_map_sorted.items())}

    categories["possible_connections"]["ip"] = {k: v for k, v in
                                                sorted(categories["possible_connections"]["ip"].items(),
                                                       key=lambda item: item[1], reverse=True)}
    categories["possible_connections"]["login"] = sorted(list(categories["possible_connections"]["login"]))

    categories["time_based"]["recent"] = sorted(list(categories["time_based"]["recent"]))
    categories["time_based"]["historical"] = sorted(list(categories["time_based"]["historical"]))
    categories["other"] = sorted(list(categories["other"]))

    return categories


def analyze_hwids(player: Player, primary_nickname: str) -> Tuple[
    List[Tuple[str, List[str]]], List[Tuple[str, List[str]]], List[Tuple[str, List[str]]]]:
    player_nicknames_set = set(getattr(player, 'nicknames', []))

    owned_hwids: List[Tuple[str, List[str]]] = []
    alt_hwids: List[Tuple[str, List[str]]] = []
    other_hwids: List[Tuple[str, List[str]]] = []

    associated_hwids_data = getattr(player, 'associated_hwids', {})
    for hwid, shared_with_list in associated_hwids_data.items():
        shared_with_set = set(shared_with_list)

        if primary_nickname in shared_with_set:
            owned_hwids.append((hwid, shared_with_list))
        elif player_nicknames_set.intersection(shared_with_set):
            alt_hwids.append((hwid, shared_with_list))
        else:
            other_hwids.append((hwid, shared_with_list))

    owned_hwids.sort(key=lambda x: (-len(x[1]), x[0]))
    alt_hwids.sort(key=lambda x: (-len(x[1]), x[0]))
    other_hwids.sort(key=lambda x: (-len(x[1]), x[0]))

    return owned_hwids, alt_hwids, other_hwids


def analyze_ips(player: Player, primary_nickname: str) -> Tuple[
    List[str], List[Tuple[str, List[str]]], List[Tuple[str, List[str]]], List[Tuple[str, List[str]]]]:
    player_nicknames_set = set(getattr(player, 'nicknames', []))

    original_ips: List[str] = []
    shared_ips: List[Tuple[str, List[str]]] = []
    alt_shared_ips: List[Tuple[str, List[str]]] = []
    multi_user_ips: List[Tuple[str, List[str]]] = []

    associated_ips_data = getattr(player, 'associated_ips', {})
    for ip, shared_with_list in associated_ips_data.items():
        shared_with_set = set(shared_with_list)

        if primary_nickname in shared_with_set:
            if len(shared_with_set) == 1:
                original_ips.append(ip)
            else:
                shared_ips.append((ip, shared_with_list))
        elif player_nicknames_set.intersection(shared_with_set):
            alt_shared_ips.append((ip, shared_with_list))
        elif len(shared_with_set) > 0:
            multi_user_ips.append((ip, shared_with_list))

    original_ips.sort()
    shared_ips.sort(key=lambda x: (-len(x[1]), x[0]))
    alt_shared_ips.sort(key=lambda x: (-len(x[1]), x[0]))
    multi_user_ips.sort(key=lambda x: (-len(x[1]), x[0]))

    return original_ips, shared_ips, alt_shared_ips, multi_user_ips


def analyze_complaints(player: Player, primary_nickname: str) -> Tuple[
    List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    if not hasattr(player, 'complaint_links') or not player.complaint_links:
        return [], [], [], []

    direct_complaints: List[Dict[str, Any]] = []
    subdirect_hwid_complaints: List[Dict[str, Any]] = []
    subdirect_ip_complaints: List[Dict[str, Any]] = []
    other_associated_complaints: List[Dict[str, Any]] = []

    player_all_nicks_set = set(getattr(player, 'nicknames', [primary_nickname]))
    primary_nickname_lower = primary_nickname.lower()

    nicks_direct_hwid_link_to_primary = set()
    if hasattr(player, 'associated_hwids'):
        for hwid, nicks_on_hwid in player.associated_hwids.items():
            if primary_nickname in nicks_on_hwid:
                nicks_direct_hwid_link_to_primary.update(
                    n.lower() for n in nicks_on_hwid
                    if n != primary_nickname and n in player_all_nicks_set
                )

    nicks_direct_ip_link_to_primary = set()
    if hasattr(player, 'associated_ips'):
        for ip, nicks_on_ip in player.associated_ips.items():
            if primary_nickname in nicks_on_ip:
                nicks_direct_ip_link_to_primary.update(
                    n.lower() for n in nicks_on_ip
                    if n != primary_nickname and n in player_all_nicks_set
                )

    other_player_alts_lower = {
        n.lower() for n in player_all_nicks_set
        if n.lower() != primary_nickname_lower
           and n.lower() not in nicks_direct_hwid_link_to_primary
           and n.lower() not in nicks_direct_ip_link_to_primary
    }

    for complaint_data in player.complaint_links:
        raw_mentioned_nicks = complaint_data.get("mentioned_nicknames", [])
        if not isinstance(raw_mentioned_nicks, list): raw_mentioned_nicks = []

        mentioned_nicks_in_complaint_lower = {
            str(n).lower() for n in raw_mentioned_nicks if isinstance(n, (str, int))
        }

        content_lower = str(complaint_data.get("content", "")).lower()

        categorized_this_complaint = False

        if primary_nickname_lower in mentioned_nicks_in_complaint_lower or \
                (content_lower and primary_nickname_lower in content_lower):
            direct_complaints.append(complaint_data)
            categorized_this_complaint = True
            continue

        for hwid_alt_lower in nicks_direct_hwid_link_to_primary:
            if hwid_alt_lower in mentioned_nicks_in_complaint_lower or \
                    (content_lower and hwid_alt_lower in content_lower):
                subdirect_hwid_complaints.append(complaint_data)
                categorized_this_complaint = True
                break
        if categorized_this_complaint: continue

        for ip_alt_lower in nicks_direct_ip_link_to_primary:
            if ip_alt_lower in mentioned_nicks_in_complaint_lower or \
                    (content_lower and ip_alt_lower in content_lower):
                subdirect_ip_complaints.append(complaint_data)
                categorized_this_complaint = True
                break
        if categorized_this_complaint: continue

        for other_alt_lower in other_player_alts_lower:
            if other_alt_lower in mentioned_nicks_in_complaint_lower or \
                    (content_lower and other_alt_lower in content_lower):
                other_associated_complaints.append(complaint_data)
                break

    def sort_key(c: Dict[str, Any]) -> Tuple[datetime, str]:
        primary_sort_dt: datetime = datetime.min.replace(tzinfo=timezone.utc)
        link_str = c.get('link', '')

        content = c.get('content', '')

        match_ddmmyyyy = re.search(r"(?:Выдан|Выдано):\s*(\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}:\d{2})", content,
                                   re.IGNORECASE)
        if match_ddmmyyyy:
            try:
                dt_obj_naive = datetime.strptime(match_ddmmyyyy.group(1), "%d.%m.%Y %H:%M:%S")
                primary_sort_dt = dt_obj_naive.replace(tzinfo=timezone.utc)
                return (primary_sort_dt, link_str)
            except ValueError:
                pass

        match_unix_t = re.search(r"<t:(\d+):R>", content)
        if match_unix_t:
            try:
                epoch_seconds = int(match_unix_t.group(1))
                primary_sort_dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
                return (primary_sort_dt, link_str)
            except ValueError:
                pass

        msg_id_ts_snowflake = c.get('message_id_as_timestamp')
        if isinstance(msg_id_ts_snowflake, int):
            try:
                DISCORD_EPOCH = 1420070400000
                timestamp_ms = (msg_id_ts_snowflake >> 22) + DISCORD_EPOCH
                primary_sort_dt = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc)
                return (primary_sort_dt, link_str)
            except Exception:
                pass

        time_str = c.get('time')
        if time_str:
            try:
                dt_obj_naive = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
                primary_sort_dt = dt_obj_naive.replace(tzinfo=timezone.utc)
                return (primary_sort_dt, link_str)
            except ValueError:
                pass

        return (primary_sort_dt, link_str)

    direct_complaints.sort(key=sort_key, reverse=True)
    subdirect_hwid_complaints.sort(key=sort_key, reverse=True)
    subdirect_ip_complaints.sort(key=sort_key, reverse=True)
    other_associated_complaints.sort(key=sort_key, reverse=True)

    return direct_complaints, subdirect_hwid_complaints, subdirect_ip_complaints, other_associated_complaints


def find_connection_paths(player: Player, primary_nickname: str) -> Optional[Dict[str, Any]]:
    if not hasattr(player, 'nicknames') or not player.nicknames:
        return None

    all_player_nicks_set = set(player.nicknames)
    if len(all_player_nicks_set) <= 1:
        return None

    associated_hwids = getattr(player, 'associated_hwids', {})
    associated_ips = getattr(player, 'associated_ips', {})

    if not associated_hwids and not associated_ips:
        return None

    primary_hwids_set = {hwid for hwid, nicks in associated_hwids.items() if primary_nickname in nicks}
    primary_ips_set = {ip for ip, nicks in associated_ips.items() if primary_nickname in nicks}

    direct_connections: Dict[str, Dict[str, Any]] = {}

    for hwid_val in primary_hwids_set:
        nicks_on_hwid = associated_hwids.get(hwid_val, [])
        for nick in nicks_on_hwid:
            if nick != primary_nickname and nick in all_player_nicks_set:
                if nick not in direct_connections:
                    direct_connections[nick] = {
                        "type": "HWID", "identifier": hwid_val, "confidence": "High",
                        "path": f"{primary_nickname} {BOX_CHARS.get('ARROW', '→')} (HWID: {hwid_val}) {BOX_CHARS.get('ARROW', '→')} {nick}"
                    }

    for ip_val in primary_ips_set:
        nicks_on_ip = associated_ips.get(ip_val, [])
        for nick in nicks_on_ip:
            if nick != primary_nickname and nick in all_player_nicks_set and nick not in direct_connections:
                direct_connections[nick] = {
                    "type": "IP", "identifier": ip_val, "confidence": "Medium",
                    "path": f"{primary_nickname} {BOX_CHARS.get('ARROW', '→')} (IP: {ip_val}) {BOX_CHARS.get('ARROW', '→')} {nick}"
                }

    indirect_connections: Dict[str, Dict[str, Any]] = {}
    indirect_by_via: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: {"hwid": [], "ip": []})

    directly_connected_alts = set(direct_connections.keys())

    for via_alt_nick in directly_connected_alts:
        for hwid_val, nicks_on_hwid in associated_hwids.items():
            if via_alt_nick in nicks_on_hwid and primary_nickname not in nicks_on_hwid:
                for target_nick in nicks_on_hwid:
                    if target_nick != via_alt_nick and target_nick in all_player_nicks_set and \
                            target_nick != primary_nickname and \
                            target_nick not in direct_connections and target_nick not in indirect_connections:
                        connection_info = {"nick": target_nick, "identifier": hwid_val}
                        indirect_by_via[via_alt_nick]["hwid"].append(connection_info)

                        indirect_connections[target_nick] = {
                            "type": "HWID-Indirect", "identifier": hwid_val, "via": via_alt_nick,
                            "confidence": "Medium",
                            "path": f"{primary_nickname} {BOX_CHARS.get('ARROW', '→')} {via_alt_nick} {BOX_CHARS.get('ARROW', '→')} (HWID: {hwid_val}) {BOX_CHARS.get('ARROW', '→')} {target_nick}"
                        }

        for ip_val, nicks_on_ip in associated_ips.items():
            if via_alt_nick in nicks_on_ip and primary_nickname not in nicks_on_ip:
                for target_nick in nicks_on_ip:
                    if target_nick != via_alt_nick and target_nick in all_player_nicks_set and \
                            target_nick != primary_nickname and \
                            target_nick not in direct_connections and target_nick not in indirect_connections:
                        connection_info = {"nick": target_nick, "identifier": ip_val}
                        indirect_by_via[via_alt_nick]["ip"].append(connection_info)

                        indirect_connections[target_nick] = {
                            "type": "IP-Indirect", "identifier": ip_val, "via": via_alt_nick, "confidence": "Low",
                            "path": f"{primary_nickname} {BOX_CHARS.get('ARROW', '→')} {via_alt_nick} {BOX_CHARS.get('ARROW', '→')} (IP: {ip_val}) {BOX_CHARS.get('ARROW', '→')} {target_nick}"
                        }

    sorted_direct_connections = {k: v for k, v in sorted(direct_connections.items())}
    sorted_indirect_connections = {k: v for k, v in sorted(indirect_connections.items())}

    for via_nick_val in indirect_by_via:
        indirect_by_via[via_nick_val]["hwid"].sort(key=lambda x: x["identifier"])
        indirect_by_via[via_nick_val]["ip"].sort(key=lambda x: x["identifier"])
    sorted_indirect_by_via = {k: v for k, v in sorted(indirect_by_via.items())}

    return {
        "direct_connections": sorted_direct_connections,
        "indirect_connections": sorted_indirect_connections,
        "indirect_by_via": sorted_indirect_by_via,
    }