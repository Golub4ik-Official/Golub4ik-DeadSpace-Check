from collections import defaultdict
from typing import List

from config_system import get_config
from models.player import Player
from models.verdict import ConfidenceLevel


class PlayerAnalyzer:
    def __init__(self) -> None:
        cfg = get_config()
        self.confidence_levels = {
            'hwid_match': ConfidenceLevel.HWID_MATCH.value,
            'ip_very_close_time': ConfidenceLevel.IP_VERY_CLOSE_TIME.value,
            'ip_close_time': ConfidenceLevel.IP_CLOSE_TIME.value,
            'ip_moderate_time': ConfidenceLevel.IP_MODERATE_TIME.value,
            'ip_distant_time': ConfidenceLevel.IP_DISTANT_TIME.value,
            'ip_match': ConfidenceLevel.IP_MATCH.value,
            'no_match': ConfidenceLevel.NO_MATCH.value
        }
        self.very_close_time_threshold_minutes = 5
        self.close_time_threshold_minutes = 10
        self.moderate_time_threshold_minutes = 30
        self.distant_time_threshold_minutes = 60

    def group_players_by_nicknames(self, players: List[Player]) -> List[Player]:
        if not players:
            return []
        nickname_to_player_indices = defaultdict(list)
        for i, player in enumerate(players):
            for nickname in player.nicknames:
                nickname_to_player_indices[nickname].append(i)
        visited = set()
        groups = []
        for i, player in enumerate(players):
            if i in visited:
                continue
            group = [i]
            visited.add(i)
            queue = set(player.nicknames)
            processed = set()
            while queue:
                nickname = queue.pop()
                processed.add(nickname)
                for player_idx in nickname_to_player_indices.get(nickname, []):
                    if player_idx not in visited:
                        visited.add(player_idx)
                        group.append(player_idx)
                        new_nicknames = set(players[player_idx].nicknames) - processed
                        queue.update(new_nicknames)
            groups.append(group)
        merged_players = []
        for group in groups:
            if len(group) == 1:
                merged_players.append(players[group[0]])
            else:
                merged_players.append(self._merge_player_group([players[idx] for idx in group]))
        return merged_players

    def _merge_player_group(self, players: List[Player]) -> Player:
        if not players:
            return None

        base_player = players[0]

        all_nicknames = set(base_player.nicknames)
        all_associated_ips = dict(base_player.associated_ips)
        all_associated_hwids = dict(base_player.associated_hwids)
        shared_hwid_nicknames = set(base_player.shared_hwid_nicknames) if hasattr(base_player,
                                                                                  'shared_hwid_nicknames') else set()

        merged_ban_reasons = []
        existing_ban_reasons = set()

        if hasattr(base_player, 'ban_reasons') and base_player.ban_reasons:
            for ban_info in base_player.ban_reasons:
                if isinstance(ban_info, dict) and "reason" in ban_info and "username" in ban_info:
                    key = (ban_info["reason"], ban_info["username"])
                    if key not in existing_ban_reasons:
                        merged_ban_reasons.append(ban_info)
                        existing_ban_reasons.add(key)
                elif isinstance(ban_info, str):
                    key = (ban_info, "Unknown")
                    if key not in existing_ban_reasons:
                        merged_ban_reasons.append({
                            "reason": ban_info,
                            "username": "Unknown"
                        })
                        existing_ban_reasons.add(key)

        status_priority = {'banned': 3, 'suspicious': 2, 'clean': 1, 'unknown': 0}
        highest_status = base_player.status.lower()
        highest_priority = status_priority.get(highest_status, 0)
        max_ban_count = base_player.ban_counts

        for player in players[1:]:
            all_nicknames.update(player.nicknames)

            for ip, nicks in player.associated_ips.items():
                if ip in all_associated_ips:
                    existing_nicks = all_associated_ips[ip]
                    combined_nicks = list(set(existing_nicks) | set(nicks))
                    all_associated_ips[ip] = combined_nicks
                else:
                    all_associated_ips[ip] = nicks

            for hwid, nicks in player.associated_hwids.items():
                if hwid in all_associated_hwids:
                    existing_nicks = all_associated_hwids[hwid]
                    combined_nicks = list(set(existing_nicks) | set(nicks))
                    all_associated_hwids[hwid] = combined_nicks
                else:
                    all_associated_hwids[hwid] = nicks

            if hasattr(player, 'shared_hwid_nicknames'):
                shared_hwid_nicknames.update(player.shared_hwid_nicknames)

            if hasattr(player, 'ban_reasons') and player.ban_reasons:
                for ban_info in player.ban_reasons:
                    if isinstance(ban_info, dict) and "reason" in ban_info and "username" in ban_info:
                        key = (ban_info["reason"], ban_info["username"])
                        if key not in existing_ban_reasons:
                            merged_ban_reasons.append(ban_info)
                            existing_ban_reasons.add(key)
                    elif isinstance(ban_info, str):
                        key = (ban_info, "Unknown")
                        if key not in existing_ban_reasons:
                            merged_ban_reasons.append({
                                "reason": ban_info,
                                "username": "Unknown"
                            })
                            existing_ban_reasons.add(key)

            current_status = player.status.lower()
            current_priority = status_priority.get(current_status, 0)
            if current_priority > highest_priority:
                highest_status = player.status
                highest_priority = current_priority

            max_ban_count = max(max_ban_count, player.ban_counts)

            if hasattr(player, 'hwid_erased') and player.hwid_erased:
                base_player.hwid_erased = True

            if hasattr(player, 'complaint_links') and player.complaint_links:
                if not hasattr(base_player, 'complaint_links'):
                    base_player.complaint_links = []

                existing_complaints = set()
                if base_player.complaint_links:
                    for complaint in base_player.complaint_links:
                        if isinstance(complaint, dict):
                            complaint_tuple = tuple(sorted((k, str(v)) for k, v in complaint.items()))
                            existing_complaints.add(complaint_tuple)

                for complaint in player.complaint_links:
                    if isinstance(complaint, dict):
                        complaint_tuple = tuple(sorted((k, str(v)) for k, v in complaint.items()))
                        if complaint_tuple not in existing_complaints:
                            base_player.complaint_links.append(complaint)
                            existing_complaints.add(complaint_tuple)

        base_player.nicknames = list(all_nicknames)
        base_player.associated_ips = all_associated_ips
        base_player.associated_hwids = all_associated_hwids
        base_player.shared_hwid_nicknames = list(shared_hwid_nicknames)
        base_player.ban_reasons = merged_ban_reasons
        base_player.status = highest_status
        base_player.ban_counts = max_ban_count

        return base_player