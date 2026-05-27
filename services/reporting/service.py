import json
import os
from collections import defaultdict, Counter
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple, Callable

from models.message import ScanResult
from models.player import Player
from services.reporting.config import (
    ReportConfig, DISPLAY_LIMITS, LAYOUT_CONFIG,
    TIME_ANALYSIS_THRESHOLDS, PLAYER_STATUS
)
from services.reporting.formatter import ReportFormatter
from services.reporting.utils import (
    determine_owner, categorize_associated_nicknames,
    analyze_hwids, analyze_ips, analyze_complaints, find_connection_paths
)
from utils.logging_utils import get_logger

logger = get_logger(__name__)


class ReportService:

    def __init__(self, config: Optional[ReportConfig] = None) -> None:
        self.config = config or ReportConfig()
        self.formatter = ReportFormatter(self.config)
        self.cache: Dict[Any, Any] = {}

        os.makedirs(self.config.report_output_dir, exist_ok=True)

    def write_json_report(self, data: List[Dict[str, Any]], filename: Optional[str] = None) -> bool:
        report_file = filename or os.path.join(self.config.report_output_dir, self.config.report_filename)
        try:
            with open(report_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            logger.info(f"Report saved to '{report_file}' ({len(data)} items)")
            return True
        except IOError as e:
            logger.error(f"Could not write report to '{report_file}': {e}")
            return False

    def _player_to_dict(self, player: Player) -> Dict[str, Any]:
        primary_nickname = getattr(player, 'primary_nickname', None) or \
                           (player.nicknames[0] if hasattr(player, 'nicknames') and player.nicknames else "Unknown")

        enhanced_ips = {}
        if hasattr(player, 'associated_ips'):
            for ip, shared_with in player.associated_ips.items():
                owner = determine_owner(primary_nickname, getattr(player, 'nicknames', []), shared_with, self.cache)
                enhanced_ips[ip] = {
                    "owner": owner, "shared_with": [nick for nick in shared_with if nick != owner], "raw_users": shared_with
                }

        enhanced_hwids = {}
        if hasattr(player, 'associated_hwids'):
            for hwid, shared_with in player.associated_hwids.items():
                owner = determine_owner(primary_nickname, getattr(player, 'nicknames', []), shared_with, self.cache)
                enhanced_hwids[hwid] = {
                    "owner": owner, "shared_with": [nick for nick in shared_with if nick != owner], "raw_users": shared_with
                }
        
        return {
            "initial_account": {
                "user_id": getattr(player, 'user_id', None),
                "nicknames": getattr(player, 'nicknames', []),
                "primary_nickname": primary_nickname,
                "status": getattr(player, 'status', 'unknown'),
                "ban_counts": getattr(player, 'ban_counts', 0),
                "ban_reasons": getattr(player, 'ban_reasons', []),
                "connection_link": getattr(player, 'connection_link', ""),
                "associated_ips": getattr(player, 'associated_ips', {}),
                "associated_hwids": getattr(player, 'associated_hwids', {}),
                "shared_hwid_nicknames": getattr(player, 'shared_hwid_nicknames', [])
            },
            "ip_data": enhanced_ips,
            "hwid_data": enhanced_hwids,
            "raw_ip_nicks": getattr(player, 'associated_ips', {}),
            "raw_hwid_nicks": getattr(player, 'associated_hwids', {}),
            "nicknames": getattr(player, 'nicknames', []),
            "hwid_erased": getattr(player, 'hwid_erased', False),
            "complaint_links": getattr(player, 'complaint_links', []),
            "timestamp": datetime.now().isoformat(),
            "scan_version": "2.1" 
        }

    def generate_message_scan_report(self, scan_results: List[ScanResult]) -> List[Dict[str, Any]]:
        report_data = []
        for result in scan_results:
            message = result.message
            players_data = [self._player_to_dict(player) for player in result.players if player] 

            message_data = {
                "message_id": message.id, "message_link": message.link,
                "author_name": message.author_name, "author_id": message.author_id,
                "scan_time": result.scan_time.isoformat(),
                "results": players_data, "scan_version": "2.1"
            }
            report_data.append(message_data)

            banned_count = sum(1 for p_data in players_data if p_data["initial_account"]["status"] == PLAYER_STATUS['BANNED'])
            suspicious_count = sum(1 for p_data in players_data if p_data["initial_account"]["status"] == PLAYER_STATUS['SUSPICIOUS'])
            logger.info(
                f"Report item: Message {message.id} by {message.author_name}: "
                f"Found {len(players_data)} players ({banned_count} banned, {suspicious_count} suspicious)"
            )
        self.print_message_scan_results(scan_results)
        return report_data

    def generate_nickname_search_report(self, nickname: str, player: Player, gui_mode: bool = False) -> List[Dict[str, Any]]:
        report_data = []
        player_info = {
            "type": "player_info", "nickname": nickname,
            "status": getattr(player, 'status', 'unknown'),
            "ban_counts": getattr(player, 'ban_counts', 0),
            "ban_reasons": getattr(player, 'ban_reasons', []),
            "hwid_erased": getattr(player, 'hwid_erased', False),
            "timestamp": datetime.now().isoformat(), "scan_version": "2.1"
        }
        report_data.append(player_info)

        if hasattr(player, 'nicknames') and player.nicknames and len(player.nicknames) > 1:
            report_data.append({"type": "associated_accounts", "nicknames": player.nicknames})
        if hasattr(player, 'denied_logins') and player.denied_logins:
            report_data.append({"type": "denied_login_attempts", "attempts": player.denied_logins})
        if hasattr(player, 'associated_ips') and player.associated_ips:
            report_data.append(self._generate_ip_data(nickname, player))
        if hasattr(player, 'associated_hwids') and player.associated_hwids:
            report_data.append(self._generate_hwid_data(nickname, player))
        if hasattr(player, 'complaint_links') and player.complaint_links:
            report_data.append({"type": "complaints", "links": player.complaint_links})

        self._print_nickname_search_results(nickname, player, gui_mode=gui_mode)
        return report_data

    def _generate_ip_data(self, nickname: str, player: Player) -> Dict[str, Any]:
        ip_data = {"type": "associated_ips", "ips": []}
        denied_logins_by_ip = defaultdict(list)
        if hasattr(player, 'denied_logins'):
            for login in player.denied_logins:
                ip = login.get("ip_address")
                if ip: denied_logins_by_ip[ip].append(login)

        for ip, shared_with in getattr(player, 'associated_ips', {}).items():
            owner = determine_owner(nickname, getattr(player, 'nicknames', []), shared_with, self.cache)
            others = [n for n in shared_with if n != owner]
            ip_entry = {
                "direct_ip_connections": ip, "owner": owner,
                "owned_by_primary": owner == nickname,
                "owned_by_alt": owner in getattr(player, 'nicknames', []) and owner != nickname,
                "shared_with": others, "raw_users": shared_with
            }
            if denied_logins_by_ip.get(ip): ip_entry["denied_logins"] = denied_logins_by_ip[ip]
            ip_data["ips"].append(ip_entry)
        return ip_data

    def _generate_hwid_data(self, nickname: str, player: Player) -> Dict[str, Any]:
        hwid_data = {"type": "associated_hwids", "hwids": []}
        denied_logins_by_hwid = defaultdict(list)
        if hasattr(player, 'denied_logins'):
            for login in player.denied_logins:
                hwid = login.get("hwid")
                if hwid: denied_logins_by_hwid[hwid].append(login)

        for hwid, shared_with in getattr(player, 'associated_hwids', {}).items():
            owner = determine_owner(nickname, getattr(player, 'nicknames', []), shared_with, self.cache)
            others = [n for n in shared_with if n != owner]
            hwid_entry = {
                "hwid": hwid, "owner": owner,
                "owned_by_primary": owner == nickname,
                "owned_by_alt": owner in getattr(player, 'nicknames', []) and owner != nickname,
                "shared_with": others, "raw_users": shared_with
            }
            if denied_logins_by_hwid.get(hwid): hwid_entry["denied_logins"] = denied_logins_by_hwid[hwid]
            hwid_data["hwids"].append(hwid_entry)
        return hwid_data

    def _get_indent_str(self, level: int = 1) -> str:
        return LAYOUT_CONFIG['DEFAULT_INDENT_STRING'] * level

    def _print_nickname_search_results(self, nickname: str, player: Player, gui_mode: bool = False) -> None:
        fmt = self.formatter.fmt
        content_indent = self._get_indent_str(1)

        self.formatter.print_header(f"РЕЗУЛЬТАТЫ ПОИСКА ДЛЯ: {fmt['BRIGHT_YELLOW_BOLD']}{nickname}{fmt['END']}", width=self.config.box_width_large)

        status_str = self.formatter.format_status(getattr(player,'status', 'unknown'), getattr(player, 'hwid_erased', False))
        ban_counts = getattr(player, 'ban_counts', 0)
        print(f"{content_indent}{fmt['WHITE_BOLD']}Статус:{fmt['END']} {status_str} {fmt['GRAY']}|{fmt['END']} {fmt['WHITE_BOLD']}Наказаний:{fmt['END']} {self.formatter.format_count(ban_counts)}")

        if gui_mode:
            return

        if hasattr(player, 'ban_reasons') and player.ban_reasons:
            self._print_ban_reasons(player, base_indent_str=content_indent)

        if hasattr(player, 'nicknames') and player.nicknames and len(player.nicknames) > 1:
            self._print_associated_nicknames_section(player, nickname, base_indent_str=content_indent)
        
        self._print_connection_paths_section(player, nickname, base_indent_str=content_indent)
        self._print_complaints_section(player, nickname, base_indent_str=content_indent)
        self._print_ip_section(player, nickname, base_indent_str=content_indent)
        self._print_hwid_section(player, nickname, base_indent_str=content_indent)
        self._print_denied_logins_section(player, nickname, base_indent_str=content_indent)
        
        print()

    def _print_section_box_start(self, title: str, base_indent_str: str, box_width: int,
                                 title_color_keys: Tuple[str, ...] = ('WHITE', 'BOLD'),
                                 box_char_set: str = 'SINGLE',
                                 box_line_color_keys: Tuple[str, ...] = ('BOLD',)) -> Tuple[str, Callable[[], None]]:
        h_bar_len = box_width - 2
        if h_bar_len < 0: h_bar_len = 0
        
        box_color_outer_str = self.formatter._get_fmt(*box_line_color_keys)
        title_str_colored = f"{self.formatter._get_fmt(*title_color_keys)}{title}{self.formatter.fmt['END']}"
        
        char_prefix = "DOUBLE_" if box_char_set == 'DOUBLE' else ""
        v_char = self.formatter.box[f'{char_prefix}V']
        tl_char = self.formatter.box[f'{char_prefix}TL']
        tr_char = self.formatter.box[f'{char_prefix}TR']
        h_char = self.formatter.box[f'{char_prefix}H']
        vr_char = self.formatter.box[f'{char_prefix}VR']
        vl_char = self.formatter.box[f'{char_prefix}VL']
        bl_char = self.formatter.box[f'{char_prefix}BL']
        br_char = self.formatter.box[f'{char_prefix}BR']

        print(f"\n{base_indent_str}{box_color_outer_str}{tl_char}{h_char * h_bar_len}{tr_char}{self.formatter.fmt['END']}")
        
        title_plain_len = len(title)
        space_for_title_and_padding = box_width - 2
        
        centering_padding_total = space_for_title_and_padding - title_plain_len
        if centering_padding_total < 0: centering_padding_total = 0
        
        left_padding_for_title = centering_padding_total // 2
        right_padding_for_title = centering_padding_total - left_padding_for_title
        
        print(f"{base_indent_str}{box_color_outer_str}{v_char}{self.formatter.fmt['END']}"
              f"{' ' * left_padding_for_title}{title_str_colored}{' ' * right_padding_for_title}"
              f"{box_color_outer_str}{v_char}{self.formatter.fmt['END']}")
        
        print(f"{base_indent_str}{box_color_outer_str}{vr_char}{h_char * h_bar_len}{vl_char}{self.formatter.fmt['END']}")

        content_v_char_colored = f"{box_color_outer_str}{v_char}{self.formatter.fmt['END']}"

        def end_section_box():
            print(f"{base_indent_str}{box_color_outer_str}{bl_char}{h_char * h_bar_len}{br_char}{self.formatter.fmt['END']}")
        
        return content_v_char_colored, end_section_box


    def _print_associated_nicknames_section(self, player: Player, nickname: str, base_indent_str: str) -> None:
        fmt = self.formatter.fmt
        box_width = self.config.box_width_medium
        item_indent_str = self._get_indent_str(1)
        
        categorized = categorize_associated_nicknames(player, nickname)
        num_confirmed = len(categorized["confirmed_alts"]["accounts"])
        num_alt_to_alt_unique = set()
        if categorized["alt_to_alt"]["hwid_map"]:
            for alts in categorized["alt_to_alt"]["hwid_map"].values():
                num_alt_to_alt_unique.update(alts)

        total_associated_nicks = (
            num_confirmed + len(num_alt_to_alt_unique) +
            len(categorized["likely_connections"]) +
            len(categorized["possible_connections"]["ip"]) +
            len(categorized["possible_connections"]["login"]) +
            len(categorized["time_based"]["recent"]) +
            len(categorized["time_based"]["historical"]) +
            len(categorized["other"])
        )

        if not total_associated_nicks and not categorized["confirmed_alts"]["accounts"] : return

        box_v_char_colored, end_box = self._print_section_box_start(
            f"СВЯЗАННЫЕ НИКНЕЙМЫ (Основной: {nickname})",
            base_indent_str, box_width, title_color_keys=('BRIGHT_YELLOW', 'BOLD'), box_char_set='SINGLE'
        )
        
        line_padding_in_box = 1


        def print_cat_header(cat_title, color_keys=('WHITE','BOLD',)):
            self.formatter.print_line_in_box(f"{item_indent_str}{self.formatter._get_fmt(*color_keys)}{self.formatter.box['FILLED_CIRCLE']} {cat_title.upper()}:{fmt['END']}",
                                             box_v_char_colored, base_indent_str, line_padding_in_box)
        
        sub_item_indent_str = item_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
        sub_sub_item_indent_str = sub_item_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
        list_display_limit = self.config.get_specific_display_limit('NICKNAME_DISPLAY_LIMIT')

        if categorized["confirmed_alts"]["accounts"]:
            print_cat_header(f"ПОДТВЕРЖДЁННЫЕ АЛЬТЫ (Напрямую связаны с {nickname})", ('RED', 'BOLD'))
            self.formatter.print_line_in_box(
                f"{sub_item_indent_str}{fmt['WHITE_BOLD']}Accounts ({len(categorized['confirmed_alts']['accounts'])}):{fmt['END']} "
                f"{self.formatter.truncate_list([fmt['WHITE'] + acc + fmt['END'] for acc in categorized['confirmed_alts']['accounts']], list_display_limit)}",
                box_v_char_colored, base_indent_str, line_padding_in_box
            )
            if categorized["confirmed_alts"]["direct_hwid"]:
                hwid_data = categorized["confirmed_alts"]["direct_hwid"]
                self.formatter.print_line_in_box(
                    f"{sub_item_indent_str}{fmt['WHITE_BOLD']}Evidence (Shared HWIDs with {nickname} - {len(hwid_data)}):{fmt['END']}",
                    box_v_char_colored, base_indent_str, line_padding_in_box
                )
                display_hwid_limit = self.config.get_specific_display_limit('HWID_OWNED_DISPLAY_LIMIT')
                count = 0
                for hwid_val, alts_on_hwid in hwid_data.items():
                    if count >= display_hwid_limit: break
                    self.formatter.print_line_in_box(
                        f"{sub_sub_item_indent_str}{self.formatter.box['SUB_ARROW']} {self.formatter.format_hwid(hwid_val)} links to: "
                        f"{self.formatter.truncate_list([fmt['WHITE'] + alt + fmt['END'] for alt in alts_on_hwid], list_display_limit)}",
                        box_v_char_colored, base_indent_str, line_padding_in_box
                    )
                    count +=1
                if len(hwid_data) > count:
                     self.formatter.print_line_in_box(
                         f"{sub_sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and {len(hwid_data) - count} more HWIDs{fmt['END']}",
                         box_v_char_colored, base_indent_str, line_padding_in_box
                     )
            self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)

        if categorized["alt_to_alt"]["hwid_map"]:
            print_cat_header(f"ALT-TO-ALT CONNECTIONS (Between {nickname}'s alts)", ('YELLOW', 'BOLD'))
            hwid_map = categorized['alt_to_alt']['hwid_map']
            num_hwids_involved = len(hwid_map)
            unique_alts_in_map = set()
            for alts_list_val in hwid_map.values(): unique_alts_in_map.update(alts_list_val)


            summary = (f"{fmt['WHITE']}{len(unique_alts_in_map)}{fmt['END']} alts interconnected by {fmt['WHITE']}{num_hwids_involved}{fmt['END']} HWIDs "
                       f"(not directly involving {nickname})")
            self.formatter.print_line_in_box(f"{sub_item_indent_str}{summary}", box_v_char_colored, base_indent_str, line_padding_in_box)
            
            display_alt_hwid_limit = self.config.get_specific_display_limit('MULTI_ALT_DISPLAY_LIMIT')
            count = 0
            for hwid_val, alts_list in hwid_map.items():
                if count >= display_alt_hwid_limit: break
                self.formatter.print_line_in_box(
                    f"{sub_sub_item_indent_str}{self.formatter.box['SUB_ARROW']} {self.formatter.format_hwid(hwid_val)} links alts: "
                    f"{self.formatter.truncate_list([fmt['WHITE'] + alt + fmt['END'] for alt in alts_list], list_display_limit)}",
                    box_v_char_colored, base_indent_str, line_padding_in_box
                )
                count += 1
            if num_hwids_involved > count:
                self.formatter.print_line_in_box(
                    f"{sub_sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and {num_hwids_involved - count} more HWIDs{fmt['END']}",
                    box_v_char_colored, base_indent_str, line_padding_in_box
                )
            self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)

        if categorized["likely_connections"]:
            print_cat_header(f"ВЕРОЯТНЫЕ СВЯЗИ (Через альтов {nickname})", ('YELLOW',))
            for conn in categorized["likely_connections"][:list_display_limit]:
                evidence_parts = []
                if conn['id_details']['hwid'] > 0:
                    evidence_parts.append(f"{fmt['WHITE']}{conn['id_details']['hwid']}{fmt['END']} HWID(s)")
                if conn['id_details']['ip'] > 0:
                    evidence_parts.append(f"{fmt['WHITE']}{conn['id_details']['ip']}{fmt['END']} IP(s)")
                evidence_str = ", ".join(evidence_parts) if evidence_parts else f"{fmt['GRAY']}N/A{fmt['END']}"
                
                line = (f"{fmt['WHITE']}{conn['nickname']}{fmt['END']}: {self.formatter.format_confidence(conn['strength_str'])} "
                        f"({fmt['GRAY']}Evidence: {evidence_str}{fmt['END']})")
                self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['RIGHT_ARROW']} {line}", box_v_char_colored, base_indent_str, line_padding_in_box)
            if len(categorized["likely_connections"]) > list_display_limit:
                 self.formatter.print_line_in_box(
                     f"{sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and "
                     f"{len(categorized['likely_connections']) - list_display_limit} more{fmt['END']}",
                     box_v_char_colored, base_indent_str, line_padding_in_box
                 )
            self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)

        if categorized["possible_connections"]["ip"] or categorized["possible_connections"]["login"]:
            print_cat_header(f"ВОЗМОЖНЫЕ СВЯЗИ (Напрямую с {nickname})", ('CYAN',))
            if categorized["possible_connections"]["login"]:
                 logins = list(categorized['possible_connections']['login'])
                 self.formatter.print_line_in_box(
                     f"{sub_item_indent_str}{fmt['WHITE_BOLD']}Via Login Data ({len(logins)}):{fmt['END']} "
                     f"{self.formatter.truncate_list([fmt['WHITE'] + l + fmt['END'] for l in logins], list_display_limit)}",
                     box_v_char_colored, base_indent_str, line_padding_in_box
                 )
            if categorized["possible_connections"]["ip"]:
                 ip_matches = categorized['possible_connections']['ip']
                 self.formatter.print_line_in_box(
                     f"{sub_item_indent_str}{fmt['WHITE_BOLD']}Via Shared IPs with {nickname} ({len(ip_matches)}):{fmt['END']}",
                     box_v_char_colored, base_indent_str, line_padding_in_box
                 )
                 count = 0
                 for nick_val, num_ips in ip_matches.items():
                     if count >= list_display_limit: break
                     self.formatter.print_line_in_box(
                         f"{sub_sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['WHITE']}{nick_val}{fmt['END']} ({fmt['GRAY']}{num_ips} shared IP(s) with {nickname}{fmt['END']})",
                         box_v_char_colored, base_indent_str, line_padding_in_box
                     )
                     count +=1
                 if len(ip_matches) > count:
                     self.formatter.print_line_in_box(
                         f"{sub_sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and "
                         f"{len(ip_matches) - count} more IP-connected accounts{fmt['END']}",
                         box_v_char_colored, base_indent_str, line_padding_in_box
                     )
            self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)
        
        time_based_nicks = categorized["time_based"]["recent"] + categorized["time_based"]["historical"]
        if time_based_nicks:
            print_cat_header("ВРЕМЕННЫЕ СВЯЗИ (Из отклонённых входов)", ("GRAY", "BOLD"))
            if categorized["time_based"]["recent"]:
                 self.formatter.print_line_in_box(
                     f"{sub_item_indent_str}{fmt['WHITE_BOLD']}Recent:{fmt['END']} "
                     f"{self.formatter.truncate_list([fmt['WHITE'] + tbr + fmt['END'] for tbr in categorized['time_based']['recent']], list_display_limit)}",
                     box_v_char_colored, base_indent_str, line_padding_in_box
                 )
            if categorized["time_based"]["historical"]:
                 self.formatter.print_line_in_box(
                     f"{sub_item_indent_str}{fmt['WHITE_BOLD']}Historical:{fmt['END']} "
                     f"{self.formatter.truncate_list([fmt['WHITE'] + tbh + fmt['END'] for tbh in categorized['time_based']['historical']], list_display_limit)}",
                     box_v_char_colored, base_indent_str, line_padding_in_box
                 )
            self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)

        if categorized["other"]:
             print_cat_header("ДРУГИЕ НИКНЕЙМЫ (Слабые/неясные связи)", ('GRAY', 'BOLD'))
             self.formatter.print_line_in_box(
                 f"{sub_item_indent_str}{self.formatter.truncate_list([fmt['WHITE'] + o + fmt['END'] for o in list(categorized['other'])], list_display_limit)}",
                 box_v_char_colored, base_indent_str, line_padding_in_box
             )

        end_box()

    def _print_ban_reasons(self, player: Player, base_indent_str: str) -> None:
        if not hasattr(player, 'ban_reasons') or not player.ban_reasons: return

        box_width = self.config.box_width_medium - 10
        indent_str = self._get_indent_str(1)
        fmt = self.formatter.fmt
        primary_nick = getattr(player, 'primary_nickname', player.nicknames[0] if player.nicknames else 'Unknown')
        limit = self.config.get_specific_display_limit('BAN_REASON_DISPLAY_LIMIT')

        for i, ban_info in enumerate(player.ban_reasons[:limit]):
            reason_text = ""
            admin_name = "N/A"
            if isinstance(ban_info, dict) and "reason" in ban_info:
                reason_text = ban_info["reason"]
                admin_name = ban_info.get("username", "N/A")
            else:
                reason_text = str(ban_info)

            block_title = f"НАКАЗАНИЕ #{i+1}"
            box_v, end_box = self._print_section_box_start(
                block_title, base_indent_str, box_width,
                title_color_keys=('RED', 'BOLD'), box_char_set='SINGLE'
            )
            line_pad = 1

            self.formatter.print_key_value_in_box(
                "Игрок", primary_nick, box_v, base_indent_str,
                key_width=14, key_color_keys=('WHITE', 'BOLD'),
                value_color_keys=('WHITE',), line_padding=line_pad
            )
            status_str = self.formatter.format_status(getattr(player, 'status', 'unknown'))
            self.formatter.print_line_in_box(
                f"{indent_str}{fmt['WHITE_BOLD']}{'Статус:':<16}{fmt['END']} {status_str}",
                box_v, base_indent_str, line_pad
            )
            self.formatter.print_key_value_in_box(
                "Причина", reason_text, box_v, base_indent_str,
                key_width=14, key_color_keys=('WHITE', 'BOLD'),
                value_color_keys=('YELLOW',), line_padding=line_pad
            )
            self.formatter.print_key_value_in_box(
                "Выдал", admin_name, box_v, base_indent_str,
                key_width=14, key_color_keys=('WHITE', 'BOLD'),
                value_color_keys=('BRIGHT_BLUE',), line_padding=line_pad
            )

            end_box()
            print()

        if len(player.ban_reasons) > limit:
            print(f"{base_indent_str}{fmt['GRAY']}... и ещё {len(player.ban_reasons) - limit} наказаний{fmt['END']}")
            print()


    def _print_connection_paths_section(self, player: Player, nickname: str, base_indent_str: str) -> None:
        connection_data = find_connection_paths(player, nickname)
        if not connection_data or (not connection_data["direct_connections"] and not connection_data["indirect_connections"]):
            return

        box_width = self.config.box_width_medium
        item_indent_str = self._get_indent_str(1)
        sub_item_indent_str = item_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
        fmt = self.formatter.fmt
        
        box_v_char_colored, end_box = self._print_section_box_start(
            "СВЯЗИ (Пути к известным альтам)", 
            base_indent_str, box_width, title_color_keys=('BRIGHT_YELLOW', 'BOLD'), box_char_set='SINGLE'
        )
        line_padding_in_box = 1

        total_conn = len(connection_data["direct_connections"]) + len(connection_data["indirect_connections"])
        self.formatter.print_line_in_box(f"{item_indent_str}{fmt['WHITE_BOLD']}Overview:{fmt['END']} {fmt['WHITE']}{total_conn}{fmt['END']} alts connected via explicit paths", box_v_char_colored, base_indent_str, line_padding_in_box)
        
        if connection_data["direct_connections"]:
            self.formatter.print_line_in_box(f"{item_indent_str}{self.formatter._get_fmt('RED', 'BOLD')}{self.formatter.box['FILLED_CIRCLE']} DIRECT CONNECTIONS ({len(connection_data['direct_connections'])}):{self.formatter.fmt['END']}",
                                             box_v_char_colored, base_indent_str, line_padding_in_box)
            limit = self.config.get_specific_display_limit('CONNECTION_PATH_DISPLAY_LIMIT')
            count = 0
            for target_nick, info in connection_data["direct_connections"].items():
                if count >= limit: break
                path_line = f"{self.formatter.box['SUB_ARROW']} {info['path']} ({self.formatter.format_confidence(info['confidence'])})"
                self.formatter.print_line_in_box(f"{sub_item_indent_str}{path_line}", box_v_char_colored, base_indent_str, line_padding_in_box)
                count +=1
            if len(connection_data["direct_connections"]) > count:
                 self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and {len(connection_data['direct_connections'])-count} more{fmt['END']}", box_v_char_colored, base_indent_str, line_padding_in_box)   
            self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)

        if connection_data["indirect_connections"]:
            self.formatter.print_line_in_box(f"{item_indent_str}{self.formatter._get_fmt('YELLOW', 'BOLD')}{self.formatter.box['FILLED_CIRCLE']} INDIRECT CONNECTIONS ({len(connection_data['indirect_connections'])}):{self.formatter.fmt['END']}",
                                             box_v_char_colored, base_indent_str, line_padding_in_box)
            limit_via_groups = self.config.get_specific_display_limit('CONNECTION_PATH_DISPLAY_LIMIT') 
            paths_per_via_limit = DISPLAY_LIMITS['SMALL']
            
            displayed_via_groups = 0
            for via_nick, data in connection_data["indirect_by_via"].items():
                if displayed_via_groups >= limit_via_groups: break
                self.formatter.print_line_in_box(f"{sub_item_indent_str}Through {self.formatter._get_fmt('WHITE_BOLD')}{via_nick}{self.formatter.fmt['END']}:",
                                                  box_v_char_colored, base_indent_str, line_padding_in_box)
                displayed_paths_for_this_via = 0
                for conn_type in ["hwid", "ip"]: 
                    for conn_detail in data[conn_type]:
                        if displayed_paths_for_this_via >= paths_per_via_limit: break
                        target_nick = conn_detail["nick"]
                        full_info = connection_data["indirect_connections"].get(target_nick)
                        if full_info and full_info['via'] == via_nick : 
                            path_line = f"  {self.formatter.box['SUB_ARROW']} {full_info['path']} ({self.formatter.format_confidence(full_info['confidence'])})"
                            self.formatter.print_line_in_box(f"{sub_item_indent_str}{path_line}", box_v_char_colored, base_indent_str, line_padding_in_box)
                            displayed_paths_for_this_via +=1
                
                total_paths_for_this_via = sum(len(data[ct_key]) for ct_key in data)
                if total_paths_for_this_via > displayed_paths_for_this_via:
                    self.formatter.print_line_in_box(f"{sub_item_indent_str}    {self.formatter.box['BULLET']} {fmt['GRAY']}...and {total_paths_for_this_via - displayed_paths_for_this_via} more paths via {via_nick}{fmt['END']}", box_v_char_colored, base_indent_str, line_padding_in_box)
                
                displayed_via_groups +=1
                if displayed_via_groups < len(connection_data["indirect_by_via"]) and displayed_via_groups < limit_via_groups:
                    self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)

            if len(connection_data["indirect_by_via"]) > displayed_via_groups :
                self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and connections through {len(connection_data['indirect_by_via'])-displayed_via_groups} more accounts{fmt['END']}", box_v_char_colored, base_indent_str, line_padding_in_box)   

        end_box()

    def _print_complaints_subsection(self, complaints: List[Dict[str, Any]], title_prefix: str,
                                    box_v_char_colored: str, base_indent_str: str, item_indent_str: str,
                                    content_area_width: int,
                                    limit_key: str, title_color_keys: Tuple[str, ...] = ('GREEN', 'BOLD')) -> None:
        if not complaints:
            return
        
        fmt = self.formatter.fmt
        line_padding_in_box = 1
        sub_item_indent_str = item_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
        content_display_indent_str = sub_item_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING'] 

        self.formatter.print_line_in_box(
            f"{item_indent_str}{self.formatter._get_fmt(*title_color_keys)}{self.formatter.box['FILLED_CIRCLE']} {title_prefix} ({fmt['WHITE']}{len(complaints)}{fmt['END']}):{self.formatter.fmt['END']}",
            box_v_char_colored, base_indent_str, line_padding_in_box
        )
        limit = self.config.get_specific_display_limit(limit_key)

        for i, complaint in enumerate(complaints[:limit]):
            link_prefix_text = f"{i+1}. "
            full_link_prefix_for_line = f"{sub_item_indent_str}{link_prefix_text}"
            raw_link = complaint.get('link', 'N/A')
            
            link_text_available_width = content_area_width - len(full_link_prefix_for_line)
            if link_text_available_width < 20: link_text_available_width = 20

            wrapped_link_parts = self.formatter.get_wrapped_lines(
                raw_link, width=link_text_available_width,
                initial_indent="", subsequent_indent=""
            )

            for k, link_part_text in enumerate(wrapped_link_parts):
                colored_link_part = f"{fmt['BLUE_UNDERLINE']}{link_part_text}{fmt['END']}"
                if k == 0:
                    line_to_print = f"{full_link_prefix_for_line}{colored_link_part}"
                else:
                    indented_link_part = f"{' ' * len(full_link_prefix_for_line)}{colored_link_part}"
                    line_to_print = indented_link_part
                self.formatter.print_line_in_box(line_to_print, box_v_char_colored, base_indent_str, line_padding_in_box)
            
            author_channel_info = []
            if complaint.get('channel'): author_channel_info.append(f"{fmt['GRAY']}Channel: {fmt['WHITE']}{complaint.get('channel')}{fmt['END']}")
            if complaint.get('author'): author_channel_info.append(f"{fmt['GRAY']}Author: {fmt['WHITE']}{complaint.get('author')}{fmt['END']}")
            if author_channel_info:
                self.formatter.print_line_in_box(
                    f"{content_display_indent_str}{fmt['GRAY']} | {fmt['END']}".join(author_channel_info), 
                    box_v_char_colored, base_indent_str, line_padding_in_box
                )
            
            content = complaint.get("content", "")
            if content:
                self.formatter.print_line_in_box(f"{content_display_indent_str}{fmt['WHITE_BOLD']}Content:{fmt['END']}", 
                                                 box_v_char_colored, base_indent_str, line_padding_in_box)
                
                eff_text_width_for_content = content_area_width - len(content_display_indent_str) - len(LAYOUT_CONFIG['DEFAULT_INDENT_STRING'])
                if eff_text_width_for_content < 10: eff_text_width_for_content = 10

                wrapped_lines = self.formatter.get_wrapped_lines(content, eff_text_width_for_content, initial_indent="", subsequent_indent="")
                for line_idx, line in enumerate(wrapped_lines):
                    self.formatter.print_line_in_box(f"{content_display_indent_str}{LAYOUT_CONFIG['DEFAULT_INDENT_STRING']}{fmt['WHITE']}{line}{fmt['END']}", 
                                                     box_v_char_colored, base_indent_str, line_padding_in_box)
        if len(complaints) > limit:
            self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}... and {len(complaints) - limit} more{fmt['END']}", 
                                             box_v_char_colored, base_indent_str, line_padding_in_box)
        self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)



    def _print_complaints_section(self, player: Player, nickname: str, base_indent_str: str) -> None:
        if not hasattr(player, 'complaint_links') or not player.complaint_links:
            return
        
        direct_complaints, sub_hwid_complaints, sub_ip_complaints, other_player_complaints = analyze_complaints(player, nickname)
        
        total_complaints_to_display = len(direct_complaints) + len(sub_hwid_complaints) + len(sub_ip_complaints) + len(other_player_complaints)
        if not total_complaints_to_display:
            return
            
        box_width = self.config.box_width_medium
        item_indent_str = self._get_indent_str(1)

        box_v_char_colored, end_box = self._print_section_box_start(
            f"ЖАЛОБЫ ({total_complaints_to_display})", 
            base_indent_str, box_width, title_color_keys=('BRIGHT_RED', 'BOLD'), box_char_set='SINGLE'
        )
        
        line_padding_in_box = 1
        content_area_width = box_width - 2 - (line_padding_in_box * 2)

        self._print_complaints_subsection(
            direct_complaints, f"DIRECT COMPLAINTS (Targeting {nickname})",
            box_v_char_colored, base_indent_str, item_indent_str, content_area_width,
            'COMPLAINT_LIMIT', ('RED', 'BOLD')
        )
        self._print_complaints_subsection(
            sub_hwid_complaints, "SUB-DIRECT COMPLAINTS (Via HWID-Linked Accounts)",
            box_v_char_colored, base_indent_str, item_indent_str, content_area_width,
            'SUB-COMPLAINT_HWID_LIMIT', ('YELLOW', 'BOLD')
        )
        self._print_complaints_subsection(
            sub_ip_complaints, "SUB-DIRECT COMPLAINTS (Via IP-Linked Accounts)",
            box_v_char_colored, base_indent_str, item_indent_str, content_area_width,
            'SUB-COMPLAINT_IP_LIMIT', ('YELLOW',)
        )
        self._print_complaints_subsection(
            other_player_complaints, "OTHER ASSOCIATED COMPLAINTS (Targeting Other Known Alts)",
            box_v_char_colored, base_indent_str, item_indent_str, content_area_width,
            'COMPLAINT_SAMPLE_LIMIT', ('CYAN',)
        )
        end_box()

    def _print_ip_hwid_list(self, title: str, items: list, formatter_func: Callable, nickname_for_context: str,
                            box_v_char_colored: str, base_indent_str: str, item_indent_str: str, 
                            limit_name: str, color_keys: Tuple[str, ...] = ('WHITE','BOLD',), show_only_user_if_single: bool = False,
                            player_obj_for_nicks: Optional[Player] = None):
        if not items: return
        fmt = self.formatter.fmt
        line_padding_in_box = 1
        sub_item_indent_str = item_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
        details_indent_str = sub_item_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']

        self.formatter.print_line_in_box(f"{item_indent_str}{self.formatter._get_fmt(*color_keys)}{self.formatter.box['FILLED_CIRCLE']} {title} ({fmt['WHITE']}{len(items)}{fmt['END']}):{self.formatter.fmt['END']}",
                                         box_v_char_colored, base_indent_str, line_padding_in_box)
        limit = self.config.get_specific_display_limit(limit_name)
        
        player_nicks_set = set(getattr(player_obj_for_nicks, 'nicknames', [nickname_for_context])) if player_obj_for_nicks else {nickname_for_context}


        for i, item_data in enumerate(items[:limit]):
            if isinstance(item_data, tuple) and len(item_data) == 2:
                identifier_val, users_list = item_data
            else:
                identifier_val, users_list = item_data, [nickname_for_context]

            self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['BULLET']} {formatter_func(identifier_val)}", 
                                             box_v_char_colored, base_indent_str, line_padding_in_box)
            
            users_set = set(users_list)
            primary_user = nickname_for_context
            primary_present = primary_user in users_set
            alts_present = sorted(list({u for u in users_list if u in player_nicks_set and u != primary_user}))
            others_present = sorted(list({u for u in users_list if u != primary_user and u not in player_nicks_set}))

            user_list_trunc_limit = DISPLAY_LIMITS['SMALL'] 

            if show_only_user_if_single and len(users_set) == 1 and primary_present:
                self.formatter.print_line_in_box(
                    f"{details_indent_str}{self.formatter.box['SUB_ARROW']} {self.formatter._get_fmt('GREEN')}Only user ({fmt['WHITE']}{primary_user}{fmt['END']}){self.formatter.fmt['END']}",
                    box_v_char_colored, base_indent_str, line_padding_in_box)
            else:
                if primary_present:
                    self.formatter.print_line_in_box(
                        f"{details_indent_str}{self.formatter.box['SUB_ARROW']} {self.formatter._get_fmt('GREEN')}Used by {fmt['WHITE']}{primary_user}{fmt['END']}{self.formatter.fmt['END']}",
                        box_v_char_colored, base_indent_str, line_padding_in_box)
                if alts_present:
                    alt_display_list = [fmt['WHITE'] + alt + fmt['END'] for alt in alts_present]
                    self.formatter.print_line_in_box(
                        f"{details_indent_str}{self.formatter.box['SUB_ARROW']} {self.formatter._get_fmt('YELLOW')}Shared with Alt(s): {self.formatter.truncate_list(alt_display_list, user_list_trunc_limit)}{self.formatter.fmt['END']}",
                        box_v_char_colored, base_indent_str, line_padding_in_box)
                if others_present:
                    others_display_list = [fmt['WHITE'] + other + fmt['END'] for other in others_present]
                    label_color = 'GRAY' if (primary_present or alts_present) else 'WHITE'
                    label = "Shared with Others" if (primary_present or alts_present) else "Users"
                    self.formatter.print_line_in_box(
                        f"{details_indent_str}{self.formatter.box['SUB_ARROW']} {self.formatter._get_fmt(label_color)}{label}: {self.formatter.truncate_list(others_display_list, user_list_trunc_limit)}{self.formatter.fmt['END']}",
                        box_v_char_colored, base_indent_str, line_padding_in_box)


        if len(items) > limit:
            self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and {len(items)-limit} more{fmt['END']}", 
                                             box_v_char_colored, base_indent_str, line_padding_in_box)
        self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)


    def _print_ip_section(self, player: Player, nickname: str, base_indent_str: str) -> None:
        if not hasattr(player, 'associated_ips') or not player.associated_ips: return
        original_ips, shared_ips, alt_shared_ips, multi_user_ips = analyze_ips(player, nickname)
        total_ips = len(original_ips) + len(shared_ips) + len(alt_shared_ips) + len(multi_user_ips)
        if not total_ips: return

        box_width = self.config.box_width_medium
        item_indent_str = self._get_indent_str(1)
        fmt = self.formatter.fmt

        box_v_char_colored, end_box = self._print_section_box_start(
            f"СВЯЗАННЫЕ IP ({total_ips})", base_indent_str, box_width, 
            title_color_keys=('BRIGHT_CYAN', 'BOLD'), box_char_set='SINGLE'
            )
        line_padding_in_box = 1
        
        if original_ips:
            self.formatter.print_line_in_box(f"{item_indent_str}{self.formatter._get_fmt('GREEN', 'BOLD')}{self.formatter.box['FILLED_CIRCLE']} PRIMARY IPs ({fmt['WHITE']}{len(original_ips)}{fmt['END']}) - Used only by {nickname}:{self.formatter.fmt['END']}", 
                                             box_v_char_colored, base_indent_str, line_padding_in_box)
            limit = self.config.get_specific_display_limit('IP_OWNED_DISPLAY_LIMIT')
            sub_item_indent_str = item_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
            for i, ip_addr in enumerate(original_ips[:limit]):
                self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['BULLET']} {self.formatter._get_fmt('BRIGHT_CYAN')}{ip_addr}{self.formatter.fmt['END']}", 
                                                 box_v_char_colored, base_indent_str, line_padding_in_box)
            if len(original_ips) > limit:
                self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and {len(original_ips)-limit} more primary IPs{fmt['END']}", 
                                                 box_v_char_colored, base_indent_str, line_padding_in_box)
            self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)

        self._print_ip_hwid_list(f"ОБЩИЕ IP - {nickname} и другие", shared_ips, lambda x: self.formatter._get_fmt('BRIGHT_CYAN') + x + self.formatter.fmt['END'], nickname, box_v_char_colored, base_indent_str, item_indent_str, 'IP_OWNED_DISPLAY_LIMIT', ('YELLOW','BOLD'), player_obj_for_nicks=player)
        self._print_ip_hwid_list(f"IP АЛЬТОВ - Использованы альтами {nickname}", alt_shared_ips, lambda x: self.formatter._get_fmt('BRIGHT_CYAN') + x + self.formatter.fmt['END'], nickname, box_v_char_colored, base_indent_str, item_indent_str, 'IP_ALT_DISPLAY_LIMIT', ('YELLOW','BOLD'), player_obj_for_nicks=player)
        self._print_ip_hwid_list(f"ДРУГИЕ IP - Не {nickname} и не альты", multi_user_ips, lambda x: self.formatter._get_fmt('BRIGHT_CYAN') + x + self.formatter.fmt['END'], nickname, box_v_char_colored, base_indent_str, item_indent_str, 'IP_OTHER_DISPLAY_LIMIT', ('GRAY','BOLD'), player_obj_for_nicks=player)
        end_box()

    def _print_hwid_section(self, player: Player, nickname: str, base_indent_str: str) -> None:
        if not hasattr(player, 'associated_hwids') or not player.associated_hwids: return
        owned_hwids, alt_hwids, other_hwids = analyze_hwids(player, nickname)
        total_hwids = len(owned_hwids) + len(alt_hwids) + len(other_hwids)
        if not total_hwids: return
            
        box_width = self.config.box_width_medium
        item_indent_str = self._get_indent_str(1)
        box_v_char_colored, end_box = self._print_section_box_start(
            f"СВЯЗАННЫЕ HWID ({total_hwids})", base_indent_str, box_width,
            title_color_keys=('BRIGHT_CYAN', 'BOLD'), box_char_set='SINGLE'
            )
        
        self._print_ip_hwid_list(f"ОСНОВНЫЕ HWID - Использованы {nickname}", owned_hwids, self.formatter.format_hwid, nickname, box_v_char_colored, base_indent_str, item_indent_str, 'HWID_OWNED_DISPLAY_LIMIT', ('GREEN','BOLD'), show_only_user_if_single=True, player_obj_for_nicks=player)
        self._print_ip_hwid_list(f"HWID АЛЬТОВ - Использованы альтами {nickname}", alt_hwids, self.formatter.format_hwid, nickname, box_v_char_colored, base_indent_str, item_indent_str, 'HWID_ALT_DISPLAY_LIMIT', ('YELLOW','BOLD'), player_obj_for_nicks=player)
        self._print_ip_hwid_list(f"ДРУГИЕ HWID - Не {nickname} и не альты", other_hwids, self.formatter.format_hwid, nickname, box_v_char_colored, base_indent_str, item_indent_str, 'HWID_OTHER_DISPLAY_LIMIT', ('GRAY','BOLD'), player_obj_for_nicks=player)
        end_box()

    def _print_denied_logins_section(self, player: Player, nickname: str, base_indent_str: str) -> None:
        if not hasattr(player, 'denied_logins') or not player.denied_logins: return

        box_width = self.config.box_width_medium
        item_indent_str = self._get_indent_str(1)
        sub_item_indent_str = item_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
        line_padding_in_box = 1
        fmt = self.formatter.fmt

        box_v_char_colored, end_box = self._print_section_box_start(
            f"ОТКЛОНЁННЫЕ ВХОДЫ ({len(player.denied_logins)})", 
            base_indent_str, box_width, title_color_keys=('BRIGHT_RED', 'BOLD'), box_char_set='SINGLE'
        )
        
        limit = self.config.get_specific_display_limit('LOGIN_DISPLAY_LIMIT')
        now = datetime.now()
        recent_threshold_dt = now - timedelta(days=TIME_ANALYSIS_THRESHOLDS['RECENT_LOGIN_DAYS'])
        
        recent_logins, older_logins = [], []
        for l_entry in player.denied_logins: 
            try: 
                login_time_str = l_entry.get("time", "1970-01-01 00:00:00")
                login_dt = datetime.strptime(login_time_str, "%Y-%m-%d %H:%M:%S")
                (recent_logins if login_dt > recent_threshold_dt else older_logins).append(l_entry)
            except ValueError: older_logins.append(l_entry)
        
        recent_logins.sort(key=lambda x: x.get('time', ''), reverse=True)
        older_logins.sort(key=lambda x: x.get('time', ''), reverse=True)


        displayed_count = 0
        if recent_logins:
            self.formatter.print_line_in_box(f"{item_indent_str}{fmt['WHITE_BOLD']}Recent (last {TIME_ANALYSIS_THRESHOLDS['RECENT_LOGIN_DAYS']} days):{fmt['END']}",
                                             box_v_char_colored, base_indent_str, line_padding_in_box)
            for i, login in enumerate(recent_logins):
                if displayed_count >= limit: break
                login_line = (f"{sub_item_indent_str}{fmt['WHITE']}{i+1}.{fmt['END']} "
                              f"{fmt['GRAY']}Time: {fmt['WHITE']}{login.get('time','N/A')}{fmt['END']} "
                              f"{fmt['GRAY']}| IP: {self.formatter._get_fmt('BRIGHT_CYAN')}{login.get('ip_address','N/A')}{self.formatter.fmt['END']} "
                              f"{fmt['GRAY']}| Server: {fmt['WHITE']}{login.get('server','N/A')}{fmt['END']}")
                self.formatter.print_line_in_box(login_line, box_v_char_colored, base_indent_str, line_padding_in_box)
                
                if login.get('user_name', nickname) != nickname:
                     self.formatter.print_line_in_box(f"{sub_item_indent_str}   {self.formatter.box['SUB_ARROW']} {fmt['GRAY']}Attempted as: {self.formatter._get_fmt('YELLOW')}{login.get('user_name')}{self.formatter.fmt['END']}", 
                                                      box_v_char_colored, base_indent_str, line_padding_in_box)
                displayed_count += 1
            
            if len(recent_logins) > displayed_count and displayed_count >= limit :
                self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and {len(recent_logins)-displayed_count} more recent logins{fmt['END']}", 
                                                 box_v_char_colored, base_indent_str, line_padding_in_box)

            self.formatter.print_line_in_box("", box_v_char_colored, base_indent_str, line_padding_in_box)


        if older_logins and displayed_count < limit:
            self.formatter.print_line_in_box(f"{item_indent_str}{fmt['WHITE_BOLD']}Older logins:{fmt['END']}",
                                             box_v_char_colored, base_indent_str, line_padding_in_box)
            older_to_show = limit - displayed_count
            for i, login in enumerate(older_logins[:older_to_show]):
                login_line = (f"{sub_item_indent_str}{fmt['WHITE']}{i+1}.{fmt['END']} "
                              f"{fmt['GRAY']}Time: {fmt['WHITE']}{login.get('time','N/A')}{fmt['END']} "
                              f"{fmt['GRAY']}| IP: {self.formatter._get_fmt('BRIGHT_CYAN')}{login.get('ip_address','N/A')}{self.formatter.fmt['END']} "
                              f"{fmt['GRAY']}| Server: {fmt['WHITE']}{login.get('server','N/A')}{fmt['END']}")
                self.formatter.print_line_in_box(login_line, box_v_char_colored, base_indent_str, line_padding_in_box)

                if login.get('user_name', nickname) != nickname:
                     self.formatter.print_line_in_box(f"{sub_item_indent_str}   {self.formatter.box['SUB_ARROW']} {fmt['GRAY']}Attempted as: {self.formatter._get_fmt('YELLOW')}{login.get('user_name')}{self.formatter.fmt['END']}", 
                                                      box_v_char_colored, base_indent_str, line_padding_in_box)
                displayed_count +=1 
            
            if len(older_logins) > older_to_show:
                 self.formatter.print_line_in_box(f"{sub_item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and {len(older_logins)-older_to_show} more older logins{fmt['END']}", 
                                                  box_v_char_colored, base_indent_str, line_padding_in_box)
        
        if len(player.denied_logins) > limit and displayed_count >= limit : 
             self.formatter.print_line_in_box(f"{item_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...displaying {limit} of {len(player.denied_logins)} total logins.{fmt['END']}", 
                                              box_v_char_colored, base_indent_str, line_padding_in_box)
        end_box()

    def _print_player_ban_summary_message_scan(self, player: Player, primary_nickname: str, indent_str: str) -> None:
        fmt = self.formatter.fmt
        if not hasattr(player, 'ban_reasons') or not player.ban_reasons:
            return

        total_ban_reasons = len(player.ban_reasons)
        print(f"{indent_str}{fmt['WHITE_BOLD']}Ban Reasons Summary ({self.formatter.format_count(total_ban_reasons, threshold_medium=1, threshold_high=3)} total):{fmt['END']}")

        limit = self.config.get_specific_display_limit('COMPLAINT_SAMPLE_LIMIT')
        
        sub_indent_str = indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']

        for i, ban_info in enumerate(player.ban_reasons[:limit]):
            reason_text_full = ""
            account_name_str_colored = ""

            if isinstance(ban_info, dict):
                reason_text_full = ban_info.get("reason", str(ban_info))
                banned_username = ban_info.get('username')
                if banned_username:
                    if banned_username.lower() != primary_nickname.lower():
                        account_name_str_colored = f"({fmt['YELLOW']}Account: {banned_username}{fmt['END']})"
                    else:
                        account_name_str_colored = f"({fmt['GREEN']}Account: {primary_nickname}{fmt['END']})"
            else:
                reason_text_full = str(ban_info)
            
            
            reason_lines = reason_text_full.splitlines()
            first_reason_line_colored = fmt['WHITE'] + (reason_lines[0] if reason_lines else "No reason text") + fmt['END']
            
            display_line_first = f"{sub_indent_str}{self.formatter.box['SUB_ARROW']} {first_reason_line_colored}"
            if account_name_str_colored:
                display_line_first += f" {account_name_str_colored}"
            print(display_line_first)

            if len(reason_lines) > 1:
                additional_reason_indent = sub_indent_str + "  "
                second_line_text = fmt['GRAY'] + reason_lines[1] + fmt['END']
                if len(reason_lines[1]) < 60 :
                    print(f"{additional_reason_indent}{second_line_text}")
                else:
                    print(f"{additional_reason_indent}{fmt['GRAY']}... (further details truncated){fmt['END']}")


        if total_ban_reasons > limit:
            print(f"{sub_indent_str}{self.formatter.box['BULLET']} {fmt['GRAY']}...and {total_ban_reasons - limit} more ban reasons.{fmt['END']}")

    def _print_player_complaints_details_message_scan(self, player: Player, primary_nickname: str,
                                                      indent_str: str) -> int:
        fmt = self.formatter.fmt
        if not hasattr(player, 'complaint_links') or not player.complaint_links:
            return 0

        direct, sub_hwid, sub_ip, other_assoc = analyze_complaints(player, primary_nickname)
        total_complaints_for_player = len(direct) + len(sub_hwid) + len(sub_ip) + len(other_assoc)

        if total_complaints_for_player == 0:
            return 0

        category_counts_parts = []
        if direct: category_counts_parts.append(f"{fmt['RED']}Direct: {len(direct)}{fmt['END']}")
        if sub_hwid: category_counts_parts.append(f"{fmt['YELLOW']}Via HWID-Alts: {len(sub_hwid)}{fmt['END']}")
        if sub_ip: category_counts_parts.append(f"{fmt['YELLOW']}Via IP-Alts: {len(sub_ip)}{fmt['END']}")
        if other_assoc: category_counts_parts.append(f"{fmt['CYAN']}Other Player Alts: {len(other_assoc)}{fmt['END']}")

        category_summary_str = f"{fmt['GRAY']}, {fmt['END']}".join(category_counts_parts)
        print(
            f"{indent_str}{fmt['WHITE_BOLD']}Complaints:{fmt['END']} Total {self.formatter.format_count(total_complaints_for_player)} ({category_summary_str})")

        sub_indent_str = indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
        content_link_indent_str = sub_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
        content_snippet_indent_str = content_link_indent_str + "  "

        overall_content_width_guide = self.formatter.config.box_width_medium

        def print_complaint_samples(complaints_list: List[Dict[str, Any]],
                                    cat_name_str: str,
                                    cat_color_keys: Tuple[str, ...],
                                    display_limit_key: str,  # Changed to accept key name
                                    is_other_player_alts_category: bool):
            if not complaints_list: return

            # Get the specific limit using the key
            num_complaints_to_show = self.config.get_specific_display_limit(display_limit_key)

            print(
                f"{sub_indent_str}{self.formatter._get_fmt(*cat_color_keys)}{self.formatter.box['BULLET']} {cat_name_str} ({fmt['WHITE']}{len(complaints_list)}{fmt['END']}):{fmt['END']}")

            # num_complaints_to_show is already fetched above

            for c_idx, c in enumerate(complaints_list[:num_complaints_to_show]):
                link_str_raw = c.get('link', 'N/A')
                plain_prefix_for_link = f"{content_link_indent_str}{self.formatter.box['SUB_ARROW']} "

                width_for_link_text = overall_content_width_guide - len(plain_prefix_for_link)
                if width_for_link_text < 20: width_for_link_text = 20

                wrapped_link_parts = self.formatter.get_wrapped_lines(
                    link_str_raw, width=width_for_link_text,
                    initial_indent="", subsequent_indent=""
                )

                for k_link, link_part_text in enumerate(wrapped_link_parts):
                    colored_link_part = f"{fmt['BLUE_UNDERLINE']}{link_part_text}{fmt['END']}"
                    if k_link == 0:
                        print(f"{content_link_indent_str}{self.formatter.box['SUB_ARROW']} {colored_link_part}")
                    else:
                        print(f"{' ' * len(plain_prefix_for_link)}{colored_link_part}")

                raw_content = c.get('content', 'No content')
                if raw_content and raw_content != "No content":
                    available_width_for_text = overall_content_width_guide - len(content_snippet_indent_str)
                    if available_width_for_text < 20:
                        available_width_for_text = 20
                    wrapped_content_lines = self.formatter.get_wrapped_lines(
                        raw_content,
                        width=available_width_for_text,
                        initial_indent="",
                        subsequent_indent=""
                    )
                    for line_content in wrapped_content_lines:
                        print(f"{content_snippet_indent_str}{fmt['WHITE']}{line_content}{fmt['END']}")

            if len(complaints_list) > num_complaints_to_show:
                print(
                    f"{content_snippet_indent_str}{fmt['WHITE']}...and {len(complaints_list) - num_complaints_to_show} more complaints in this category.{fmt['END']}")

        # Use specific limit keys now
        print_complaint_samples(direct, "Direct", ('RED', 'BOLD'), 'COMPLAINT_LIMIT',
                                False)  # Assuming 'COMPLAINT_LIMIT' for direct ones
        print_complaint_samples(sub_hwid, "Via HWID-Alts", ('YELLOW', 'BOLD'), 'SUB-COMPLAINT_HWID_LIMIT', False)
        print_complaint_samples(sub_ip, "Via IP-Alts", ('YELLOW',), 'SUB-COMPLAINT_IP_LIMIT',
                                False)  # <-- THIS IS THE KEY CHANGE
        print_complaint_samples(other_assoc, "Other Player Alts", ('CYAN',), 'COMPLAINT_SAMPLE_LIMIT',
                                True)  # 'COMPLAINT_SAMPLE_LIMIT' can be kept for this or another specific key.

        return total_complaints_for_player


    def _print_player_ip_details_message_scan(self, player: Player, primary_nickname: str, indent_str: str):
        fmt = self.formatter.fmt
        if not hasattr(player, 'associated_ips') or not player.associated_ips:
            print(f"{indent_str}{fmt['WHITE_BOLD']}Associated IPs:{fmt['END']} {self.formatter.format_count(0)}")
            return

        original_ips, shared_ips, alt_shared_ips, multi_user_ips = analyze_ips(player, primary_nickname)
        total_ips = len(player.associated_ips) 
        
        print(f"{indent_str}{fmt['WHITE_BOLD']}Associated IPs:{fmt['END']} {self.formatter.format_count(total_ips)}")

        is_any_ip_shared = any(len(users) > 1 for _, users in player.associated_ips.items())
        list_trunc_limit = self.config.get_specific_display_limit('SMALL') 


        if total_ips > 0 :
            sub_indent_str = indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
            details_indent_str = sub_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING'] 
            
            if original_ips:
                print(f"{sub_indent_str}{fmt['GREEN_BOLD']}{self.formatter.box['BULLET']} Primary IPs ({fmt['WHITE']}{len(original_ips)}{fmt['END']}):{fmt['END']} {self.formatter.truncate_list([self.formatter._get_fmt('BRIGHT_CYAN') + ip + self.formatter.fmt['END'] for ip in original_ips], list_trunc_limit)}")
            
            if shared_ips:
                print(f"{sub_indent_str}{fmt['YELLOW_BOLD']}{self.formatter.box['BULLET']} Shared with {primary_nickname} ({fmt['WHITE']}{len(shared_ips)}{fmt['END']}):{fmt['END']}")
                for i, (ip, users) in enumerate(shared_ips[:list_trunc_limit]):
                    others = [u for u in users if u != primary_nickname]
                    print(f"{details_indent_str}{self.formatter._get_fmt('BRIGHT_CYAN')}{ip}{self.formatter.fmt['END']} ({fmt['GRAY']}with: {self.formatter.truncate_list([fmt['WHITE'] + o + fmt['END'] for o in others], list_trunc_limit)}{fmt['END']})")
                if len(shared_ips) > list_trunc_limit: print(f"{details_indent_str}{fmt['GRAY']}...and {len(shared_ips)-list_trunc_limit} more.{fmt['END']}")

            if alt_shared_ips:
                print(f"{sub_indent_str}{fmt['YELLOW_BOLD']}{self.formatter.box['BULLET']} Shared with Player's Alts ({fmt['WHITE']}{len(alt_shared_ips)}{fmt['END']}):{fmt['END']}")
                player_nicks_set = set(getattr(player, 'nicknames', []))
                for i, (ip, users) in enumerate(alt_shared_ips[:list_trunc_limit]):
                    alt_users_on_ip = sorted(list({u for u in users if u in player_nicks_set and u != primary_nickname}))
                    other_users_on_ip = sorted(list({u for u in users if u not in player_nicks_set}))
                    shared_desc_parts = []
                    if alt_users_on_ip: shared_desc_parts.append(f"{fmt['YELLOW']}Alts: {self.formatter.truncate_list([fmt['WHITE'] + alt + fmt['END'] for alt in alt_users_on_ip], list_trunc_limit)}{fmt['END']}")
                    if other_users_on_ip: shared_desc_parts.append(f"{fmt['GRAY']}Others: {self.formatter.truncate_list([fmt['WHITE'] + o + fmt['END'] for o in other_users_on_ip], list_trunc_limit)}{fmt['END']}")
                    print(f"{details_indent_str}{self.formatter._get_fmt('BRIGHT_CYAN')}{ip}{self.formatter.fmt['END']} ({fmt['GRAY']}{'; '.join(shared_desc_parts)}{fmt['END']})")
                if len(alt_shared_ips) > list_trunc_limit: print(f"{details_indent_str}{fmt['GRAY']}...and {len(alt_shared_ips)-list_trunc_limit} more.{fmt['END']}")
            
            if multi_user_ips:
                print(f"{sub_indent_str}{fmt['GRAY_BOLD']}{self.formatter.box['BULLET']} Other Multi-User IPs ({fmt['WHITE']}{len(multi_user_ips)}{fmt['END']}):{fmt['END']}")
                for i, (ip, users) in enumerate(multi_user_ips[:list_trunc_limit]):
                     print(f"{details_indent_str}{self.formatter._get_fmt('BRIGHT_CYAN')}{ip}{self.formatter.fmt['END']} ({fmt['GRAY']}users: {self.formatter.truncate_list([fmt['WHITE'] + u + fmt['END'] for u in sorted(list(set(users)))], list_trunc_limit)}{fmt['END']})")
                if len(multi_user_ips) > list_trunc_limit: print(f"{details_indent_str}{fmt['GRAY']}...and {len(multi_user_ips)-list_trunc_limit} more.{fmt['END']}")


    def _print_player_hwid_details_message_scan(self, player: Player, primary_nickname: str, indent_str: str):
        fmt = self.formatter.fmt
        if not hasattr(player, 'associated_hwids') or not player.associated_hwids:
            print(f"{indent_str}{fmt['WHITE_BOLD']}Associated HWIDs:{fmt['END']} {self.formatter.format_count(0)}")
            return

        owned_hwids, alt_hwids, other_hwids = analyze_hwids(player, primary_nickname)
        total_hwids = len(player.associated_hwids)

        print(f"{indent_str}{fmt['WHITE_BOLD']}Associated HWIDs:{fmt['END']} {self.formatter.format_count(total_hwids)}")
        
        is_any_hwid_shared = any(len(users) > 1 for _, users in player.associated_hwids.items())
        list_trunc_limit = self.config.get_specific_display_limit('SMALL')

        if total_hwids > 0 : 
            sub_indent_str = indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
            details_indent_str = sub_indent_str + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']

            if owned_hwids: 
                print(f"{sub_indent_str}{fmt['GREEN_BOLD']}{self.formatter.box['BULLET']} Primary HWIDs ({fmt['WHITE']}{len(owned_hwids)}{fmt['END']}):{fmt['END']}")
                for i, (hwid, users) in enumerate(owned_hwids[:list_trunc_limit]):
                    others = [u for u in users if u != primary_nickname]
                    if not others: 
                        print(f"{details_indent_str}{self.formatter.format_hwid(hwid)} ({fmt['GREEN']}only {fmt['WHITE']}{primary_nickname}{fmt['END']})")
                    else: 
                        print(f"{details_indent_str}{self.formatter.format_hwid(hwid)} ({fmt['GRAY']}with: {self.formatter.truncate_list([fmt['WHITE'] + o + fmt['END'] for o in sorted(list(set(others)))], list_trunc_limit)}{fmt['END']})")
                if len(owned_hwids) > list_trunc_limit: print(f"{details_indent_str}{fmt['GRAY']}...and {len(owned_hwids)-list_trunc_limit} more.{fmt['END']}")
            
            if alt_hwids:
                print(f"{sub_indent_str}{fmt['YELLOW_BOLD']}{self.formatter.box['BULLET']} Player's Alt HWIDs ({fmt['WHITE']}{len(alt_hwids)}{fmt['END']}):{fmt['END']}")
                player_nicks_set = set(getattr(player, 'nicknames', []))
                for i, (hwid, users) in enumerate(alt_hwids[:list_trunc_limit]):
                    alt_users_on_hwid = sorted(list({u for u in users if u in player_nicks_set and u != primary_nickname}))
                    other_users_on_hwid = sorted(list({u for u in users if u not in player_nicks_set}))
                    shared_desc_parts = []
                    if alt_users_on_hwid: shared_desc_parts.append(f"{fmt['YELLOW']}Alts: {self.formatter.truncate_list([fmt['WHITE'] + alt + fmt['END'] for alt in alt_users_on_hwid], list_trunc_limit)}{fmt['END']}")
                    if other_users_on_hwid: shared_desc_parts.append(f"{fmt['GRAY']}Others: {self.formatter.truncate_list([fmt['WHITE'] + o + fmt['END'] for o in other_users_on_hwid], list_trunc_limit)}{fmt['END']}")
                    print(f"{details_indent_str}{self.formatter.format_hwid(hwid)} ({fmt['GRAY']}{'; '.join(shared_desc_parts)}{fmt['END']})")
                if len(alt_hwids) > list_trunc_limit: print(f"{details_indent_str}{fmt['GRAY']}...and {len(alt_hwids)-list_trunc_limit} more.{fmt['END']}")

            if other_hwids:
                print(f"{sub_indent_str}{fmt['GRAY_BOLD']}{self.formatter.box['BULLET']} Other Shared HWIDs ({fmt['WHITE']}{len(other_hwids)}{fmt['END']}):{fmt['END']}")
                for i, (hwid, users) in enumerate(other_hwids[:list_trunc_limit]):
                     print(f"{details_indent_str}{self.formatter.format_hwid(hwid)} ({fmt['GRAY']}users: {self.formatter.truncate_list([fmt['WHITE'] + u + fmt['END'] for u in sorted(list(set(users)))], list_trunc_limit)}{fmt['END']})")
                if len(other_hwids) > list_trunc_limit: print(f"{details_indent_str}{fmt['GRAY']}...and {len(other_hwids)-list_trunc_limit} more.{fmt['END']}")


    def print_message_scan_results(self, scan_results: List[ScanResult]) -> None:
        fmt = self.formatter.fmt
        overall_indent = self._get_indent_str(0) 
        message_content_indent = self._get_indent_str(1) 
        player_section_base_indent = self._get_indent_str(1) 
        player_content_indent = player_section_base_indent + LAYOUT_CONFIG['DEFAULT_INDENT_STRING'] 

        self.formatter.print_header(f"SCAN RESULTS - {fmt['WHITE']}{len(scan_results)}{fmt['END']} messages processed", self.config.box_width_large)
        
        summary_data = {
            "total_players": 0, "banned": 0, "suspicious": 0, "clean": 0, "unknown": 0,
            "total_complaints": 0, "unique_hwids": set(), "unique_ips": set(),
            "problematic_players": []
        }

        for idx, result in enumerate(scan_results):
            message = result.message
            self.formatter.print_section(
                f"MESSAGE ({fmt['WHITE']}{idx+1}/{len(scan_results)}{fmt['END']}): {fmt['BLUE_UNDERLINE']}{message.link}{fmt['END']}",
                width=self.config.box_width_large
            )
            print(f"{message_content_indent}{fmt['WHITE_BOLD']}AUTHOR:{fmt['END']} {fmt['BRIGHT_WHITE']}{message.author_name}{fmt['END']}")
            if hasattr(result, 'scan_time') and result.scan_time:
                scan_time_str = result.scan_time.strftime("%Y-%m-%d %H:%M:%S")
                print(f"{message_content_indent}{fmt['WHITE_BOLD']}SCANNED:{fmt['END']} {fmt['WHITE']}{scan_time_str}{fmt['END']}")

            valid_players = [p for p in result.players if p and getattr(p, 'primary_nickname', None) and p.primary_nickname != "Unknown"]
            if not valid_players:
                print(f"{message_content_indent}{fmt['GRAY']}No valid players found in this message.{fmt['END']}")
            
            for player_idx, player in enumerate(valid_players):
                primary_nick = getattr(player, 'primary_nickname', 'Unknown')
                
                summary_data["total_players"] += 1
                status_lower = getattr(player, 'status', 'unknown').lower()
                summary_data[status_lower] = summary_data.get(status_lower, 0) + 1
                ban_count = getattr(player, 'ban_counts', 0)

                if status_lower in [PLAYER_STATUS['BANNED'], PLAYER_STATUS['SUSPICIOUS']]:
                     summary_data["problematic_players"].append((primary_nick, player.status.upper(), ban_count))

                print(f"\n{player_section_base_indent}{self.formatter._get_fmt('BRIGHT_YELLOW','BOLD')}PLAYER: {primary_nick}{fmt['END']}")
                
                status_str = self.formatter.format_status(player.status, getattr(player, 'hwid_erased', False))
                print(f"{player_content_indent}{fmt['WHITE_BOLD']}STATUS:{fmt['END']} {status_str} {fmt['GRAY']}|{fmt['END']} {fmt['WHITE_BOLD']}BANS:{fmt['END']} {self.formatter.format_count(ban_count)}")


                if hasattr(player, 'ban_reasons') and player.ban_reasons:
                    self._print_player_ban_summary_message_scan(player, primary_nick, player_content_indent)
                
                player_total_complaints = self._print_player_complaints_details_message_scan(player, primary_nick, player_content_indent)
                
                self._print_player_ip_details_message_scan(player, primary_nick, player_content_indent)
                self._print_player_hwid_details_message_scan(player, primary_nick, player_content_indent)
                
                if hasattr(player, 'complaint_links'): 
                    summary_data["total_complaints"] += len(player.complaint_links) 
                if hasattr(player, 'associated_ips'): 
                    summary_data["unique_ips"].update(player.associated_ips.keys())
                if hasattr(player, 'associated_hwids'): 
                    summary_data["unique_hwids"].update(player.associated_hwids.keys())

            if idx < len(scan_results) - 1:
                print() 
                self.formatter.print_horizontal_line(self.config.box_width_large, indent_str=overall_indent, char_key='H', color_keys=('GRAY',))
        
        self.formatter.print_header("SCAN SUMMARY", self.config.box_width_large)
        stats = {
            "Messages Processed": len(scan_results),
            "Total Players Found": summary_data["total_players"],
            "Banned Players": summary_data["banned"],
            "Suspicious Players": summary_data["suspicious"],
            "Clean Players": summary_data["clean"],
            "Unknown Status": summary_data["unknown"],
            "Total Complaints Linked": summary_data["total_complaints"], 
            "Unique HWIDs Detected": len(summary_data["unique_hwids"]),
            "Unique IPs Detected": len(summary_data["unique_ips"]),
        }
        self.formatter.print_stats_box("Overall Statistics", stats, 
                                       base_indent_str=message_content_indent, 
                                       width=self.config.box_width_medium, 
                                       columns=2)

        if summary_data["problematic_players"]:
            print(f"\n{message_content_indent}{self.formatter._get_fmt('BRIGHT_YELLOW', 'BOLD')}Problematic Players Summary ({fmt['WHITE']}{len(summary_data['problematic_players'])}{fmt['END']}):{fmt['END']}")
            limit = self.config.get_specific_display_limit('SUMMARY_LIST_LIMIT')
            problematic_content_indent = message_content_indent + LAYOUT_CONFIG['DEFAULT_INDENT_STRING']
            for p_nick, p_status, p_bans in summary_data["problematic_players"][:limit]:
                p_status_fmt = self.formatter.format_status(p_status) 
                print(f"{problematic_content_indent}{self.formatter.box['BULLET']} {fmt['WHITE']}{p_nick}{fmt['END']}: {p_status_fmt} ({fmt['GRAY']}Bans: {self.formatter.format_count(p_bans)}{fmt['END']})")
            if len(summary_data["problematic_players"]) > limit:
                print(f"{problematic_content_indent}{self.formatter.box['BULLET']} {fmt['GRAY']}... and {len(summary_data['problematic_players']) - limit} more.{fmt['END']}")
        print()