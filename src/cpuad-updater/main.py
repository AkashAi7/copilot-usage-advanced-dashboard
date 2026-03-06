import json
import requests
import os
import hashlib
import math
from elasticsearch import Elasticsearch, NotFoundError
from datetime import datetime, timedelta
from log_utils import configure_logger, current_time
import time
from metrics_2_usage_convertor import convert_metrics_to_usage
import traceback
from zoneinfo import ZoneInfo
from create_user_summary import create_user_summaries
from create_user_top_by_day import create_user_top_by_day


def get_utc_offset():
    tz_name = os.environ.get("TZ", "GMT")
    try:
        local_tz = ZoneInfo(tz_name)
    except Exception:
        local_tz = ZoneInfo("GMT")
    now = datetime.now(local_tz)
    offset_sec = now.utcoffset().total_seconds()
    offset_hours = int(offset_sec // 3600)
    offset_minutes = int((offset_sec % 3600) // 60)
    offset_str = f"{offset_hours:+03}:{abs(offset_minutes):02}"
    return offset_str


def calculate_top_values(user_data):
    """Calculate top model, language, and feature from user metrics data"""
    
    # Initialize counters
    model_counts = {}
    language_counts = {}
    feature_counts = {}
    
    # Extract from totals_by_language_model
    for entry in user_data.get('totals_by_language_model', []):
        language = entry.get('language', 'unknown')
        model = entry.get('model', 'unknown')
        activity_count = entry.get('code_generation_activity_count', 0)
        
        language_counts[language] = language_counts.get(language, 0) + activity_count
        model_counts[model] = model_counts.get(model, 0) + activity_count
    
    # Extract from totals_by_feature
    for entry in user_data.get('totals_by_feature', []):
        feature = entry.get('feature', 'unknown')
        activity_count = entry.get('code_generation_activity_count', 0) + entry.get('user_initiated_interaction_count', 0)
        
        feature_counts[feature] = feature_counts.get(feature, 0) + activity_count
    
    # Extract from totals_by_language_feature (additional language data)
    for entry in user_data.get('totals_by_language_feature', []):
        language = entry.get('language', 'unknown')
        activity_count = entry.get('code_generation_activity_count', 0)
        
        language_counts[language] = language_counts.get(language, 0) + activity_count
    
    # Find top values (most used)
    top_model = max(model_counts.items(), key=lambda x: x[1])[0] if model_counts else 'unknown'
    top_language = max(language_counts.items(), key=lambda x: x[1])[0] if language_counts else 'unknown'
    top_feature = max(feature_counts.items(), key=lambda x: x[1])[0] if feature_counts else 'unknown'
    
    # Map feature names to more user-friendly names
    feature_mapping = {
        'chat_panel_ask_mode': 'Chat',
        'chat_panel_agent_mode': 'Agent',
        'agent_edit': 'Agent',
        'code_completion': 'Code Completion',
        'inline_chat': 'Inline Chat'
    }
    
    top_feature = feature_mapping.get(top_feature, top_feature)
    
    return {
        'top_model': top_model,
        'top_language': top_language, 
        'top_feature': top_feature
    }


class Paras:

    @staticmethod
    def date_str():
        return current_time()[:10]

    # GitHub
    github_pat = os.getenv("GITHUB_PAT")
    organization_slugs = os.getenv("ORGANIZATION_SLUGS")

    # ElasticSearch
    primary_key = os.getenv("PRIMARY_KEY", "unique_hash")
    elasticsearch_url = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
    elasticsearch_user = os.getenv("ELASTICSEARCH_USER", None)
    elasticsearch_pass = os.getenv("ELASTICSEARCH_PASS", None)

    # Log path
    log_path = os.getenv("LOG_PATH", "logs")

    @staticmethod
    def get_log_path():
        return os.path.join(Paras.log_path, Paras.date_str())

    # Execution interval HOURS
    execution_interval = int(os.getenv("EXECUTION_INTERVAL", 6))


class Indexes:
    index_seat_info = os.getenv("INDEX_SEAT_INFO", "copilot_seat_info_settings")
    index_seat_assignments = os.getenv(
        "INDEX_SEAT_ASSIGNMENTS", "copilot_seat_assignments"
    )
    index_name_total = os.getenv("INDEX_NAME_TOTAL", "copilot_usage_total")
    index_name_breakdown = os.getenv("INDEX_NAME_BREAKDOWN", "copilot_usage_breakdown")
    index_name_breakdown_chat = os.getenv(
        "INDEX_NAME_BREAKDOWN_CHAT", "copilot_usage_breakdown_chat"
    )
    index_user_metrics = os.getenv("INDEX_USER_METRICS", "copilot_user_metrics")
    index_user_adoption = os.getenv("INDEX_USER_ADOPTION", "copilot_user_adoption")


logger = configure_logger(log_path=Paras.log_path)
logger.info("-----------------Starting-----------------")


# Validate github_pat and organization_slugs, if not present, log an error and exit
if not Paras.github_pat:
    logger.error("GitHub PAT not found, exiting...")
    exit(1)

if not Paras.organization_slugs:
    logger.error("Organization slugs not found, exiting...")
    exit(1)


def github_api_request_handler(url, error_return_value=[]):
    logger.info(f"Requesting URL: {url}")
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {Paras.github_pat}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    
    try:
        response = requests.get(url, headers=headers)
        logger.info(f"Response status code: {response.status_code}")
        
        if response.status_code != 200:
            logger.error(f"HTTP {response.status_code} error for URL: {url}")
            logger.error(f"Response text: {response.text}")
            return error_return_value
        
        data = response.json()
        logger.info(f"Successfully received data from: {url}")
        
        if isinstance(data, dict) and data.get("status", "200") != "200":
            logger.error(f"Request failed reason: {data}")
            return error_return_value
        return data
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Request exception for URL {url}: {e}")
        return error_return_value
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error for URL {url}: {e}")
        return error_return_value


def dict_save_to_json_file(
    data, file_name, logs_path=Paras.get_log_path(), save_to_json=True
):
    if not data:
        logger.warning(f"No data to save for {file_name}")
        return
    if save_to_json:
        if not os.path.exists(logs_path):
            os.makedirs(logs_path)
        with open(
            f"{logs_path}/{file_name}_{Paras.date_str()}.json", "w", encoding="utf8"
        ) as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        logger.info(f"Data saved to {logs_path}/{file_name}_{Paras.date_str()}.json")


def generate_unique_hash(data, key_properties=[]):
    key_elements = []
    for key_property in key_properties:
        value = data.get(key_property)
        key_elements.append(str(value) if value is not None else "")
    key_string = "-".join(key_elements)
    unique_hash = hashlib.sha256(key_string.encode()).hexdigest()
    return unique_hash


def _compute_percentile(sorted_values, percentile):
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * (percentile / 100)
    lower = math.floor(k)
    upper = math.ceil(k)
    if lower == upper:
        return float(sorted_values[int(k)])
    lower_value = sorted_values[lower]
    upper_value = sorted_values[upper]
    weight_upper = k - lower
    weight_lower = upper - k
    return float(lower_value) * weight_lower + float(upper_value) * weight_upper


def _robust_scale(value, lower, upper):
    if upper <= lower:
        return 1.0
    return max(0.0, min(1.0, (value - lower) / (upper - lower)))


def build_user_adoption_leaderboard(metrics_data, organization_slug, slug_type, top_n=10):
    if not metrics_data:
        return []

    grouped = {}
    report_start_days = set()
    report_end_days = set()

    for record in metrics_data:
        login = record.get("user_login") or "unknown"
        entry = grouped.setdefault(login, {
            "events_logged": 0,
            "volume": 0,
            "code_generation": 0,
            "code_acceptance": 0,
            "loc_added": 0,
            "loc_suggested": 0,
            "agent_usage": 0,
            "chat_usage": 0,
            "days": set(),
        })

        entry["events_logged"] += 1
        entry["volume"] += record.get("user_initiated_interaction_count", 0)
        entry["code_generation"] += record.get("code_generation_activity_count", 0)
        entry["code_acceptance"] += record.get("code_acceptance_activity_count", 0)
        entry["loc_added"] += record.get("loc_added_sum", 0)
        entry["loc_suggested"] += record.get("loc_suggested_to_add_sum", 0)
        if record.get("used_agent"):
            entry["agent_usage"] += 1
        if record.get("used_chat"):
            entry["chat_usage"] += 1
        day_val = record.get("day")
        if day_val:
            entry["days"].add(day_val)

        start_day = record.get("report_start_day")
        if start_day:
            report_start_days.add(start_day)
        end_day = record.get("report_end_day")
        if end_day:
            report_end_days.add(end_day)

    global_start_day = min(report_start_days) if report_start_days else None
    global_end_day = max(report_end_days) if report_end_days else None

    summaries = []
    for login, stats in grouped.items():
        active_days = len(stats["days"])
        interaction_per_day = (
            stats["volume"] / active_days if active_days else 0.0
        )
        acceptance_rate = (
            stats["code_acceptance"] / stats["code_generation"]
            if stats["code_generation"]
            else 0.0
        )
        average_loc_added = (
            stats["loc_added"] / active_days if active_days else 0.0
        )
        feature_breadth = stats["agent_usage"] + stats["chat_usage"]

        # Stamp a day for Grafana time filtering: prefer global_end_day, fallback to current UTC day
        stamped_day = (
            global_end_day if global_end_day else datetime.utcnow().strftime("%Y-%m-%d")
        )

        summary = {
            "user_login": login,
            "organization_slug": organization_slug,
            "slug_type": slug_type,
            "events_logged": stats["events_logged"],
            "volume": stats["volume"],
            "code_generation_activity_count": stats["code_generation"],
            "code_acceptance_activity_count": stats["code_acceptance"],
            "loc_added_sum": stats["loc_added"],
            "loc_suggested_to_add_sum": stats["loc_suggested"],
            "average_loc_added": average_loc_added,
            "interactions_per_day": interaction_per_day,
            "acceptance_rate": acceptance_rate,
            "feature_breadth": feature_breadth,
            "agent_usage": stats["agent_usage"],
            "chat_usage": stats["chat_usage"],
            "active_days": active_days,
            "report_start_day": global_start_day,
            "report_end_day": global_end_day,
            "day": stamped_day,
            "bucket_type": "user",
            "is_top10": False,
            "rank": None,
        }

        summary["unique_hash"] = generate_unique_hash(
            summary,
            key_properties=[
                "organization_slug",
                "user_login",
                "report_start_day",
                "report_end_day",
                "bucket_type",
            ],
        )

        summaries.append(summary)

    if not summaries:
        return []

    signals = {
        "volume": [entry["volume"] for entry in summaries],
        "interactions_per_day": [entry["interactions_per_day"] for entry in summaries],
        "acceptance_rate": [entry["acceptance_rate"] for entry in summaries],
        "average_loc_added": [entry["average_loc_added"] for entry in summaries],
        "feature_breadth": [entry["feature_breadth"] for entry in summaries],
    }

    bounds = {}
    for key, values in signals.items():
        sorted_values = sorted(values)
        lower = _compute_percentile(sorted_values, 5)
        upper = _compute_percentile(sorted_values, 95)
        bounds[key] = (lower, upper)

    for entry in summaries:
        norm_volume = _robust_scale(entry["volume"], *bounds["volume"])
        norm_interactions = _robust_scale(
            entry["interactions_per_day"], *bounds["interactions_per_day"]
        )
        norm_acceptance = _robust_scale(
            entry["acceptance_rate"], *bounds["acceptance_rate"]
        )
        norm_loc_added = _robust_scale(
            entry["average_loc_added"], *bounds["average_loc_added"]
        )
        norm_feature = _robust_scale(
            entry["feature_breadth"], *bounds["feature_breadth"]
        )

        base_score = (
            0.2 * norm_volume
            + 0.2 * norm_interactions
            + 0.2 * norm_acceptance
            + 0.2 * norm_loc_added
            + 0.2 * norm_feature
        )
        entry["_base_score"] = base_score

    max_active_days = max(entry["active_days"] for entry in summaries)
    for entry in summaries:
        bonus = 0.1 * (entry["active_days"] / max_active_days) if max_active_days else 0.0
        bonus = min(bonus, 0.1)
        entry["consistency_bonus"] = bonus
        entry["adoption_score"] = entry["_base_score"] * (1 + bonus)

    max_score = max(entry["adoption_score"] for entry in summaries)
    for entry in summaries:
        entry["adoption_pct"] = (
            round(entry["adoption_score"] / max_score * 100, 1)
            if max_score
            else 0.0
        )

    summaries.sort(key=lambda e: e["adoption_pct"], reverse=True)
    leaderboard = summaries[:top_n]
    for rank, entry in enumerate(leaderboard, start=1):
        entry["rank"] = rank
        entry["is_top10"] = True

    entries = []
    for entry in leaderboard:
        entry["bucket_type"] = "user"
        entries.append(entry)

    others = summaries[top_n:]
    if others:
        others_count = len(others)
        # Stamp a day for Grafana time filtering: prefer global_end_day, fallback to current UTC day
        stamped_day = (
            global_end_day if global_end_day else datetime.utcnow().strftime("%Y-%m-%d")
        )

        others_entry = {
            "user_login": "Others",
            "organization_slug": organization_slug,
            "slug_type": slug_type,
            "events_logged": sum(o["events_logged"] for o in others),
            "volume": sum(o["volume"] for o in others),
            "code_generation_activity_count": sum(
                o["code_generation_activity_count"] for o in others
            ),
            "code_acceptance_activity_count": sum(
                o["code_acceptance_activity_count"] for o in others
            ),
            "loc_added_sum": sum(o["loc_added_sum"] for o in others),
            "loc_suggested_to_add_sum": sum(
                o["loc_suggested_to_add_sum"] for o in others
            ),
            "average_loc_added": sum(o["average_loc_added"] for o in others) / others_count,
            "interactions_per_day": sum(
                o["interactions_per_day"] for o in others
            )
            / others_count,
            "acceptance_rate": sum(o["acceptance_rate"] for o in others) / others_count,
            "feature_breadth": sum(o["feature_breadth"] for o in others) / others_count,
            "agent_usage": sum(o["agent_usage"] for o in others),
            "chat_usage": sum(o["chat_usage"] for o in others),
            "active_days": sum(o["active_days"] for o in others),
            "report_start_day": global_start_day,
            "report_end_day": global_end_day,
            "day": stamped_day,
            "bucket_type": "others",
            "is_top10": False,
            "rank": None,
            "others_count": others_count,
            "consistency_bonus": 0.0,
        }

        others_entry["adoption_score"] = (
            sum(o["adoption_score"] for o in others) / others_count
        )
        score_scale = max_score if max_score else 1
        others_entry["adoption_pct"] = round(
            others_entry["adoption_score"] / score_scale * 100, 1
        )
        others_entry["unique_hash"] = generate_unique_hash(
            others_entry,
            key_properties=[
                "organization_slug",
                "user_login",
                "report_start_day",
                "report_end_day",
                "bucket_type",
            ],
        )
        entries.append(others_entry)

    for entry in entries:
        entry.pop("_base_score", None)
    return entries

def assign_position_in_tree(nodes):
    # Create a dictionary with node id as key and node data as value
    node_dict = {node["id"]: node for node in nodes}

    # Create sets to store all node ids and child node ids
    all_ids = set(node_dict.keys())
    child_ids = set()

    # Build parent-child relationships
    for node in nodes:
        parent = node.get("parent")
        if parent and "id" in parent:
            parent_id = parent["id"]
            child_ids.add(node["id"])
            # Add child node list to parent node
            parent_node = node_dict.get(parent_id)
            if parent_node:
                parent_node.setdefault("children", []).append(node["id"])

    # Find root nodes (nodes that are not child nodes)
    root_ids = all_ids - child_ids

    # Mark the position of all nodes
    for node_id in all_ids:
        node = node_dict[node_id]
        children = node.get("children", [])
        if not children:
            node["position_in_tree"] = "leaf_team"
        elif node_id in root_ids:
            node["position_in_tree"] = "root_team"
        else:
            node["position_in_tree"] = "trunk_team"

    return nodes


class GitHubEnterpriseManager:

    # Question: Teams under the same Enterprise can be duplicated in different orgs, so isn't there a problem with the API like this?
    # https://docs.github.com/en/enterprise-cloud@latest/rest/copilot/copilot-usage?apiVersion=2022-11-28#get-a-summary-of-copilot-usage-for-an-enterprise-team

    def __init__(self, token, enterprise_slug, save_to_json=True):
        self.token = token
        self.enterprise_slug = enterprise_slug
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
        }
        self.url = "https://api.github.com/graphql"
        self.orgs = self._fetch_all_organizations(save_to_json=save_to_json)
        self.orgs_slugs = [org["login"] for org in self.orgs]
        self.github_organization_managers = {
            orgs_slug: GitHubOrganizationManager(self.token, orgs_slug)
            for orgs_slug in self.orgs_slugs
        }
        logger.info(
            f"Initialized GitHubEnterpriseManager for enterprise: {enterprise_slug}"
        )

    def _fetch_all_organizations(self, save_to_json=False):

        # GraphQL query
        query = (
            """
        {
            enterprise(slug: "%s") {
                organizations(first: 100) {
                    nodes {
                        login
                        name
                        description
                        email
                        isVerified
                        location
                        websiteUrl
                        createdAt
                        updatedAt
                        membersWithRole {
                            totalCount
                        }
                        teams {
                            totalCount
                        }
                        repositories {
                            totalCount
                        }
                    }
                }
            }
        }
        """
            % self.enterprise_slug
        )

        # Send POST request
        logger.info(
            f"Fetching all organizations for enterprise: {self.enterprise_slug}"
        )
        response = requests.post(self.url, json={"query": query}, headers=self.headers)

        # Check response status code
        if response.status_code == 200:
            data = response.json()
            # print(data)
            if "errors" in data:
                print(f'query failed, error message: {data["errors"][0]["message"]}')
                return {}
            all_orgs = (
                data["data"]
                .get("enterprise", {})
                .get("organizations", {})
                .get("nodes", [])
            )

            dict_save_to_json_file(
                all_orgs,
                f"{self.enterprise_slug}_all_organizations",
                save_to_json=save_to_json,
            )
            logger.info(f"Fetched {len(all_orgs)} organizations")
            return all_orgs
        else:
            print(f"request failed, error code: {response.status_code}")
            logger.error(f"Request failed with status code: {response.status_code}")
            return {}


class GitHubOrganizationManager:

    def __init__(self, organization_slug, save_to_json=True, is_standalone=False):
        self.slug_type = "Standalone" if is_standalone else "Organization"
        self.api_type = "enterprises" if is_standalone else "orgs"
        self.organization_slug = organization_slug
        self.teams = self._fetch_all_teams(save_to_json=save_to_json)
        self.utc_offset = get_utc_offset()
        logger.info(
            f"Initialized GitHubOrganizationManager for {self.slug_type}: {organization_slug}"
        )

    def _fetch_report_data(self, url, label="metrics"):
        """
        Fetch report data from a GitHub Copilot metrics report endpoint (new API, 2026+).

        The endpoint returns signed download URLs; this method downloads and parses
        the NDJSON (newline-delimited JSON) content from each link.

        New endpoints used:
          - /orgs/{org}/copilot/metrics/reports/organization-28-day/latest
          - /enterprises/{enterprise}/copilot/metrics/reports/enterprise-28-day/latest

        Reference: https://docs.github.com/en/enterprise-cloud@latest/rest/copilot/copilot-usage-metrics
        """
        logger.info(f"Fetching report download links from: {url}")
        api_response = github_api_request_handler(url, error_return_value={})

        if not api_response or "download_links" not in api_response:
            logger.warning(f"No download links received from {label} report API at: {url}")
            return []

        download_links = api_response.get("download_links", [])
        report_start = api_response.get("report_start_day", "unknown")
        report_end = api_response.get("report_end_day", "unknown")
        logger.info(
            f"Found {len(download_links)} download links for {label} "
            f"(period: {report_start} to {report_end})"
        )

        all_records = []
        for i, download_link in enumerate(download_links, 1):
            try:
                logger.info(f"Downloading {label} data from link {i}/{len(download_links)}")
                # Do NOT send Authorization header to signed blob storage URLs
                response = requests.get(download_link, headers={"Accept": "application/json"})

                if response.status_code != 200:
                    logger.error(
                        f"Download link {i} failed with HTTP {response.status_code}: {response.text[:200]}"
                    )
                    continue

                if not response.content:
                    logger.warning(f"Download link {i} returned empty content, skipping")
                    continue

                # Try standard JSON first, fall back to NDJSON (newline-delimited)
                try:
                    parsed = response.json()
                    if isinstance(parsed, list):
                        all_records.extend(parsed)
                        logger.info(f"Download link {i}: parsed JSON array with {len(parsed)} records")
                    elif isinstance(parsed, dict):
                        all_records.append(parsed)
                        logger.info(f"Download link {i}: parsed JSON object, wrapped as single record")
                except json.JSONDecodeError:
                    logger.info(f"Download link {i}: not standard JSON, attempting NDJSON parse")
                    count = 0
                    for line in response.text.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            all_records.append(json.loads(line))
                            count += 1
                        except json.JSONDecodeError as e:
                            logger.error(f"Download link {i}: failed to parse NDJSON line — {e}")
                    logger.info(f"Download link {i}: parsed {count} NDJSON records")

            except requests.exceptions.RequestException as req_error:
                logger.error(f"Request error for download link {i}: {req_error}")
            except Exception as e:
                logger.error(f"Unexpected error processing download link {i}: {e}")

        logger.info(f"Total records fetched for {label}: {len(all_records)}")
        return all_records

    def get_copilot_usages(
        self,
        team_slug="all",      # kept for API compatibility; team-level not available in new API
        save_to_json=True,
        position_in_tree="leaf_team",  # kept for API compatibility
        usage_or_metrics="metrics",    # kept for API compatibility
    ):
        """
        Fetch Copilot usage metrics using the new report-based API endpoints.

        Replaces the legacy endpoints deprecated January 29, 2026 (sunset April 2, 2026):
          - Legacy: GET /orgs/{org}/copilot/metrics          (direct JSON response)
          - Legacy: GET /orgs/{org}/team/{team}/copilot/metrics

        New endpoints used (X-GitHub-Api-Version: 2022-11-28):
          - Org:        GET /orgs/{org}/copilot/metrics/reports/organization-28-day/latest
          - Enterprise: GET /enterprises/{ent}/copilot/metrics/reports/enterprise-28-day/latest

        The new API returns signed download URLs; actual data is NDJSON files.
        Team-level breakdown is no longer available via the new API — data is org-level only.

        Reference:
          https://docs.github.com/en/enterprise-cloud@latest/rest/copilot/copilot-usage-metrics
          https://github.blog/changelog/2026-01-29-closing-down-notice-of-legacy-copilot-metrics-apis/
        """
        # Build the correct report endpoint for org vs enterprise
        if self.api_type == "enterprises":
            report_path = "enterprise-28-day"
        else:
            report_path = "organization-28-day"

        url = (
            f"https://api.github.com/{self.api_type}/{self.organization_slug}"
            f"/copilot/metrics/reports/{report_path}/latest"
        )

        logger.info(
            f"Fetching Copilot metrics for {self.slug_type}: {self.organization_slug} "
            f"via new report API — team-level breakdown not available in new API"
        )

        raw_data = self._fetch_report_data(
            url, label=f"{self.organization_slug} {self.slug_type} metrics"
        )

        # ── Legacy API fallback ────────────────────────────────────────────────
        # The new report endpoint may return metadata (report_start_day /
        # report_end_day) but empty download_links when the enterprise has not
        # yet been migrated to the report-based delivery model.  Fall back to
        # the classic Metrics API which is still active until April 2026.
        if not raw_data:
            legacy_url = (
                f"https://api.github.com/{self.api_type}/{self.organization_slug}"
                f"/copilot/metrics"
            )
            logger.warning(
                f"New report API returned no data for {self.slug_type} "
                f"{self.organization_slug}. Falling back to legacy metrics "
                f"endpoint: {legacy_url}"
            )
            raw_data = github_api_request_handler(legacy_url, error_return_value=[])
            if raw_data:
                logger.info(
                    f"Legacy metrics endpoint returned {len(raw_data)} days of "
                    f"data for {self.slug_type}: {self.organization_slug}"
                )
                dict_save_to_json_file(
                    raw_data,
                    f"{self.organization_slug}_no-team_copilot_metrics_legacy",
                    save_to_json=save_to_json,
                )
            else:
                logger.warning(
                    f"Legacy metrics endpoint also returned no data for "
                    f"{self.slug_type}: {self.organization_slug}"
                )

        dict_save_to_json_file(
            raw_data,
            f"{self.organization_slug}_no-team_copilot_metrics",
            save_to_json=save_to_json,
        )

        # Convert Metrics API format → Usage format consumed by DataSplitter
        usage_data = convert_metrics_to_usage(raw_data)

        dict_save_to_json_file(
            usage_data,
            f"{self.organization_slug}_no-team_copilot_usage",
            save_to_json=save_to_json,
        )

        # Return in the same dict structure expected by the caller
        datas = {
            "no-team": {
                "position_in_tree": "root_team",
                "copilot_usage_data": usage_data,
            }
        }

        dict_save_to_json_file(
            datas,
            f"{self.organization_slug}_all_teams_copilot_usage",
            save_to_json=save_to_json,
        )

        logger.info(
            f"Fetched {len(usage_data)} days of metrics for "
            f"{self.slug_type}: {self.organization_slug}"
        )
        return datas

    def get_seat_info_settings_standalone(self, save_to_json=True):
        # only for Standalone
        # todo: no API for Standalone, need to caculate the data from other APIs
        url = f"https://api.github.com/{self.api_type}/{self.organization_slug}/copilot/billing/seats"
        data_seats = github_api_request_handler(url, error_return_value={})
        if not data_seats:
            return data_seats

        data = {
            "seat_management_setting": "assign_selected",
            "public_code_suggestions": "allow",
            "ide_chat": "enabled",
            "cli": "enabled",
            "plan_type": "business",
            "seat_total": data_seats.get("total_seats", 0),
            "seat_added_this_cycle": 0,  # caculated
            "seat_pending_invitation": 0,  # always 0
            "seat_pending_cancellation": 0,  # caculated
            "seat_active_this_cycle": 0,  # caculated
            "seat_inactive_this_cycle": 0,
        }

        for data_seat in data_seats.get("seats", []):
            # format: 2024-07-03T03:02:57+08:00
            seat_created_at = data_seat.get("created_at")
            if seat_created_at:
                created_date = datetime.strptime(seat_created_at, "%Y-%m-%dT%H:%M:%S%z")
                start_of_yesterday = datetime.now(created_date.tzinfo).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) - timedelta(days=1)
                if created_date >= start_of_yesterday:
                    data["seat_added_this_cycle"] += 1

            seat_pending_cancellation_date = data_seat.get("pending_cancellation_date")
            if seat_pending_cancellation_date:
                data["seat_pending_cancellation"] += 1

            seat_last_activity_at = data_seat.get("last_activity_at")
            if seat_last_activity_at:
                last_activity_date = datetime.strptime(
                    seat_last_activity_at, "%Y-%m-%dT%H:%M:%S%z"
                )
                start_of_yesterday = datetime.now(last_activity_date.tzinfo).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ) - timedelta(days=1)
                if last_activity_date >= start_of_yesterday:
                    data["seat_active_this_cycle"] += 1

        data["seat_inactive_this_cycle"] = (
            data["seat_total"] - data["seat_active_this_cycle"]
        )

        # Inject organization_slug and today's date in the format 2024-12-15, and a hash value based on these two values
        data["organization_slug"] = self.organization_slug
        data["day"] = current_time()[:10]
        data["unique_hash"] = generate_unique_hash(
            data, key_properties=["organization_slug", "day"]
        )

        dict_save_to_json_file(
            data,
            f"{self.organization_slug}_seat_info_settings",
            save_to_json=save_to_json,
        )
        logger.info(
            f"Fetching seat info settings for {self.slug_type}: {self.organization_slug}"
        )
        return data

    def get_seat_info_settings(self, save_to_json=True):
        # only for organization
        url = f"https://api.github.com/{self.api_type}/{self.organization_slug}/copilot/billing"
        data = github_api_request_handler(url, error_return_value={})
        if not data:
            return data
        # sample
        # {
        #     "seat_breakdown": {
        #         "total": 36,
        #         "added_this_cycle": 2,
        #         "pending_invitation": 0,
        #         "pending_cancellation": 36,
        #         "active_this_cycle": 30,
        #         "inactive_this_cycle": 6
        #     },
        #     "seat_management_setting": "assign_selected",
        #     "public_code_suggestions": "allow",
        #     "ide_chat": "enabled",
        #     "cli": "enabled",
        #     "plan_type": "business"
        # }
        # Needs to be converted to the following format
        # {
        #     "seat_management_setting": "assign_selected",
        #     "public_code_suggestions": "allow",
        #     "ide_chat": "enabled",
        #     "cli": "enabled",
        #     "plan_type": "business",
        #     "seat_total": 36,
        #     "seat_added_this_cycle": 2,
        #     "seat_pending_invitation": 0,
        #     "seat_pending_cancellation": 36,
        #     "seat_active_this_cycle": 30,
        #     "seat_inactive_this_cycle": 6,
        #     "organization_slug": "CopilotNext",
        #     "day": "2024-12-15"
        # }

        seat_breakdown = data.get("seat_breakdown", {})
        for k, v in seat_breakdown.items():
            data[f"seat_{k}"] = v
        data.pop("seat_breakdown", None)

        # Inject organization_slug and today's date in the format 2024-12-15, and a hash value based on these two values
        data["organization_slug"] = self.organization_slug
        data["day"] = current_time()[:10]
        data["unique_hash"] = generate_unique_hash(
            data, key_properties=["organization_slug", "day"]
        )

        dict_save_to_json_file(
            data,
            f"{self.organization_slug}_seat_info_settings",
            save_to_json=save_to_json,
        )
        logger.info(
            f"Fetching seat info settings for {self.slug_type}: {self.organization_slug}"
        )
        return data

    def get_seat_assignments(self, save_to_json=True):
        url = f"https://api.github.com/{self.api_type}/{self.organization_slug}/copilot/billing/seats"
        datas = []
        page = 1
        per_page = 50
        while True:
            paginated_url = f"{url}?page={page}&per_page={per_page}"
            data = github_api_request_handler(paginated_url, error_return_value={})
            seats = data.get("seats", [])
            logger.info(f"Current page seats count: {len(seats)}")
            if not seats:
                break
            for seat in seats:
                if not seat.get("assignee"):
                    continue
                # assignee sub dict
                seat["assignee_login"] = seat.get("assignee", {}).get("login")
                # if organization_slug is CopilotNext, then assignee_login
                if self.organization_slug == "CopilotNext":
                    seat["assignee_login"] = "".join(
                        [chr(ord(c) + 1) for c in seat["assignee_login"]]
                    )

                seat["assignee_html_url"] = seat.get("assignee", {}).get("html_url")
                seat.pop("assignee", None)

                # assigning_team sub dict
                seat["assignee_team_slug"] = seat.get("assigning_team", {}).get(
                    "slug", "no-team"
                )
                seat["assignee_team_html_url"] = seat.get("assigning_team", {}).get(
                    "html_url"
                )
                seat.pop("assigning_team", None)

                seat["organization_slug"] = self.organization_slug
                # seat['day'] = current_time()[:10] # 2025-04-02T08:00:00+08:00 seat['updated_at'][:10]
                seat["day"] = datetime.now(
                    datetime.strptime(seat["updated_at"], "%Y-%m-%dT%H:%M:%S%z").tzinfo
                ).strftime("%Y-%m-%d %H:%M:%S.%f")[:10]
                seat["unique_hash"] = generate_unique_hash(
                    seat, key_properties=["organization_slug", "assignee_login", "day"]
                )

                last_activity_at = seat.get(
                    "last_activity_at"
                )  # 2025-04-02T00:22:35+08:00
                if last_activity_at:
                    last_activity_date = datetime.strptime(
                        last_activity_at, "%Y-%m-%dT%H:%M:%S%z"
                    )
                    days_since_last_activity = (
                        datetime.now(last_activity_date.tzinfo) - last_activity_date
                    ).days
                    # Create updated_at_date with the same timezone as last_activity_date
                    updated_at_date = datetime.now(last_activity_date.tzinfo)
                    is_active_today = (
                        1
                        if (last_activity_date.date() == updated_at_date.date())
                        else 0
                    )
                    seat["is_active_today"] = is_active_today
                else:
                    days_since_last_activity = -1
                    seat["is_active_today"] = 0
                seat["days_since_last_activity"] = days_since_last_activity
                datas.append(seat)
            page += 1

        dict_save_to_json_file(
            datas,
            f"{self.organization_slug}_seat_assignments",
            save_to_json=save_to_json,
        )
        logger.info(
            f"Fetching seat assignments for {self.slug_type}: {self.organization_slug}"
        )
        return datas

    def _fetch_all_teams(self, save_to_json=True):
        # Teams under the same org are essentially at the same level because the URL does not reflect the nested relationship, so team names cannot be duplicated

        url = f"https://api.github.com/{self.api_type}/{self.organization_slug}/teams"
        teams = []
        page = 1
        per_page = 50
        while True:
            paginated_url = f"{url}?page={page}&per_page={per_page}"
            page_teams = github_api_request_handler(
                paginated_url, error_return_value=[]
            )
            logger.info(f"Current page teams count: {len(page_teams)}")
            # if credential is expired, the return value is:
            # {'message': 'Bad credentials', 'documentation_url': 'https://docs.github.com/rest', 'status': '401'}
            if isinstance(page_teams, dict) and page_teams.get("status") == "401":
                logger.error(
                    f"Bad credentials for {self.slug_type}: {self.organization_slug}"
                )
                return []
            if not page_teams:
                break
            teams.extend(page_teams)
            page += 1

        teams = self._add_fullpath_slug(teams)
        teams = assign_position_in_tree(teams)
        dict_save_to_json_file(
            teams, f"{self.organization_slug}_all_teams", save_to_json=save_to_json
        )
        logger.info(
            f"Fetching all teams for {self.slug_type}: {self.organization_slug}"
        )

        return teams

    def _metrics_to_synthetic_user_records(self, metrics_days, team_lookup=None):
        """
        Convert classic /copilot/metrics day-level aggregate data into synthetic
        per-day "enterprise-aggregate" user records that `create_breakdown_from_user_metrics`
        and the adoption leaderboard can consume.

        Each element of `metrics_days` looks like:
          {
            "date": "2025-11-26",
            "copilot_ide_code_completions": { "editors": [ {...} ] },
            "copilot_ide_chat": { "editors": [ {...} ] },
            "total_active_users": 171,
            ...
          }

        One synthetic user record is emitted per day with
        user_login = "enterprise-aggregate".
        """
        records = []
        current_time_str = current_time()

        for day_data in metrics_days:
            day = day_data.get("date")
            if not day:
                continue

            # ── Build totals_by_language_model from code completions ────────────
            totals_by_language_model = []
            totals_by_ide = []
            total_gen = 0
            total_accept = 0
            total_chat = 0

            code_completions = day_data.get("copilot_ide_code_completions") or {}
            for editor in code_completions.get("editors", []):
                editor_name = editor.get("name", "unknown")
                for model_entry in editor.get("models", []):
                    model_name = model_entry.get("name", "unknown")
                    for lang in model_entry.get("languages", []):
                        suggestions = lang.get("total_code_suggestions", 0)
                        acceptances = lang.get("total_code_acceptances", 0)
                        totals_by_language_model.append({
                            "language": lang.get("name", "unknown"),
                            "model": model_name,
                            "code_generation_activity_count": suggestions,
                            "code_acceptance_activity_count": acceptances,
                            "loc_suggested_to_add_sum": lang.get("total_code_lines_suggested", 0),
                            "loc_added_sum": lang.get("total_code_lines_accepted", 0),
                        })
                        total_gen += suggestions
                        total_accept += acceptances

            # ── Build totals_by_ide from IDE chat data ──────────────────────────
            ide_chat = day_data.get("copilot_ide_chat") or {}
            for editor in ide_chat.get("editors", []):
                editor_name = editor.get("name", "unknown")
                editor_chats = sum(
                    m.get("total_chats", 0)
                    for m in editor.get("models", [])
                )
                totals_by_ide.append({
                    "ide": editor_name,
                    "user_initiated_interaction_count": editor_chats,
                    "code_acceptance_activity_count": 0,
                })
                total_chat += editor_chats

            record = {
                "user_login": "enterprise-aggregate",
                "day": day,
                "organization_slug": self.organization_slug,
                "slug_type": self.slug_type,
                "last_updated_at": current_time_str,
                "utc_offset": self.utc_offset,
                "team_slug": "no-team",
                "totals_by_language_model": totals_by_language_model,
                "totals_by_language_feature": [],
                "totals_by_ide": totals_by_ide,
                "code_generation_activity_count": total_gen,
                "code_acceptance_activity_count": total_accept,
                "user_initiated_interaction_count": total_chat,
                "loc_suggested_to_add_sum": sum(
                    e.get("loc_suggested_to_add_sum", 0)
                    for e in totals_by_language_model
                ),
                "loc_added_sum": sum(
                    e.get("loc_added_sum", 0)
                    for e in totals_by_language_model
                ),
                "used_chat": total_chat > 0,
                "used_agent": False,
                "is_synthetic": True,
            }
            record["unique_hash"] = generate_unique_hash(
                record,
                key_properties=["organization_slug", "user_login", "day"],
            )
            records.append(record)

        return records

    def get_copilot_user_metrics(self, save_to_json=True, team_lookup=None):
        """
        Fetch Copilot user metrics for the last 28 days.
        Uses the /copilot/metrics/reports/users-28-day/latest endpoint.
        The API returns signed download URLs containing NDJSON user-level data.

        Args:
            save_to_json: Whether to persist raw/processed data to JSON log files.
            team_lookup: Optional dict mapping user_login -> team_slug, built from
                         seat assignments. When provided, each user record is enriched
                         with a `team_slug` field so Grafana team filters still work
                         even though the new metrics API no longer provides team-level
                         aggregates. Users not found in the lookup get 'no-team'.
        """
        # If a local metrics file is provided (for troubleshooting/demo), use it directly
        local_path = os.getenv("LOCAL_USER_METRICS_FILE")
        if local_path and os.path.exists(local_path):
            logger.info(f"Using LOCAL_USER_METRICS_FILE instead of download links: {local_path}")
            records = []
            try:
                with open(local_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to parse line as JSON, skipping. Error: {e}")
                            continue

                        rec["organization_slug"] = self.organization_slug
                        rec["slug_type"] = self.slug_type
                        rec["last_updated_at"] = current_time()
                        rec["utc_offset"] = self.utc_offset

                        # Enrich with team info from seat assignments lookup
                        user_login = rec.get("user_login", "")
                        rec["team_slug"] = (
                            team_lookup.get(user_login, "no-team")
                            if team_lookup
                            else "no-team"
                        )

                        hash_properties = ["organization_slug", "user_login", "day"]
                        if "user_login" in rec and "day" in rec:
                            rec["unique_hash"] = generate_unique_hash(rec, hash_properties)
                        else:
                            fallback_properties = [
                                "organization_slug",
                                "last_updated_at",
                            ]
                            rec["unique_hash"] = generate_unique_hash(
                                rec, fallback_properties
                            )

                        records.append(rec)
                logger.info(
                    f"Loaded {len(records)} user metrics records from LOCAL_USER_METRICS_FILE"
                )
            except Exception as e:
                logger.error(
                    f"Error reading LOCAL_USER_METRICS_FILE {local_path}: {e}"
                )
                records = []

            dict_save_to_json_file(
                records,
                f"{self.organization_slug}_copilot_user_metrics_local",
                save_to_json=save_to_json,
            )
            return records

        url = f"https://api.github.com/{self.api_type}/{self.organization_slug}/copilot/metrics/reports/users-28-day/latest"
        
        logger.info(f"Fetching user metrics download links from: {url}")
        api_response = github_api_request_handler(url, error_return_value={})
        
        if not api_response or 'download_links' not in api_response:
            logger.warning(
                f"No download links received from user metrics report API for "
                f"{self.slug_type}: {self.organization_slug}. "
                f"Attempting legacy metrics endpoint to synthesise aggregate records."
            )
            # ── Legacy fallback: synthesise per-day aggregate user records ──────
            # The classic /copilot/metrics endpoint returns day-level aggregate data
            # (not per user), but we can convert each day into a single synthetic
            # "enterprise-aggregate" record enriched with language/model and IDE
            # breakdowns so that create_breakdown_from_user_metrics() can still
            # populate the Languages/Editors/Teams Grafana panels.
            legacy_url = (
                f"https://api.github.com/{self.api_type}/{self.organization_slug}"
                f"/copilot/metrics"
            )
            legacy_metrics = github_api_request_handler(legacy_url, error_return_value=[])
            if legacy_metrics:
                logger.info(
                    f"Legacy metrics endpoint returned {len(legacy_metrics)} days; "
                    f"synthesising aggregate user records for breakdown panels."
                )
                synthetic_records = self._metrics_to_synthetic_user_records(
                    legacy_metrics, team_lookup=team_lookup
                )
                # Persist synthetic records so other parts of the pipeline can
                # inspect them if needed.
                dict_save_to_json_file(
                    synthetic_records,
                    f"{self.organization_slug}_copilot_user_metrics_synthetic",
                    save_to_json=save_to_json,
                )
                logger.info(
                    f"Created {len(synthetic_records)} synthetic aggregate records "
                    f"from legacy metrics for {self.slug_type}: {self.organization_slug}"
                )
                return synthetic_records
            logger.warning(
                f"Legacy metrics endpoint also returned no data for "
                f"{self.slug_type}: {self.organization_slug}. User metrics will be empty."
            )
            return []
        
        download_links = api_response.get('download_links', [])
        logger.info(f"Found {len(download_links)} download links for user metrics")
        
        processed_data = []
        current_time_str = current_time()
        
        # Process each download link to get the actual user metrics data
        for i, download_link in enumerate(download_links, 1):
            try:
                logger.info(f"Downloading user metrics data from link {i}/{len(download_links)}")
                
                # Download JSON data from the link with better error handling
                try:
                    logger.info(f"Requesting download link: {download_link}")
                    # Do NOT send Authorization header to Azure Blob Storage
                    headers = {
                        "Accept": "application/json"
                    }
                    response = requests.get(download_link, headers=headers)
                    
                    logger.info(f"Download link {i} response status: {response.status_code}")
                    logger.info(f"Download link {i} response headers: {dict(response.headers)}")
                    logger.info(f"Download link {i} response content length: {len(response.content)}")
                    
                    if response.status_code != 200:
                        logger.error(f"Download link {i} failed with status {response.status_code}: {response.text}")
                        continue
                    
                    if not response.content:
                        logger.warning(f"Download link {i} returned empty content")
                        continue
                    
                    # Try to parse as JSON (handle NDJSON line-by-line)
                    try:
                        user_metrics_response = response.json()
                    except json.JSONDecodeError as json_error:
                        # Likely NDJSON (newline-delimited JSON), parse line-by-line
                        logger.info(f"Download link {i} appears to be NDJSON, parsing line-by-line")
                        user_metrics_response = []
                        for line in response.text.splitlines():
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                user_metrics_response.append(json.loads(line))
                            except json.JSONDecodeError as line_error:
                                logger.error(f"Failed to parse NDJSON line: {line_error}")
                                continue
                        if not user_metrics_response:
                            logger.error(f"Download link {i} returned non-parseable content. Original error: {json_error}")
                            logger.error(f"Response content preview (first 500 chars): {response.text[:500]}")
                            continue
                    
                except requests.exceptions.RequestException as req_error:
                    logger.error(f"Request error for download link {i}: {req_error}")
                    continue
                
                if not user_metrics_response:
                    logger.warning(f"No data received from download link {i}")
                    continue
                
                logger.info(f"Download link {i} response type: {type(user_metrics_response)}")
                
                # Handle different response types and format JSON properly
                if isinstance(user_metrics_response, list):
                    # If it's already an array, use it directly
                    user_metrics_data = user_metrics_response
                    logger.info(f"Download link {i} returned array with {len(user_metrics_data)} items")
                elif isinstance(user_metrics_response, dict):
                    # If it's a dict, wrap it in an array
                    user_metrics_data = [user_metrics_response]
                    logger.info(f"Download link {i} returned single object, wrapped in array")
                else:
                    # If it's neither dict nor list, try to format it
                    logger.warning(f"Download link {i} returned unexpected type: {type(user_metrics_response)}")
                    try:
                        # Try to convert to string and parse again
                        response_str = str(user_metrics_response)
                        logger.info(f"Attempting to format response as JSON: {response_str[:200]}...")
                        
                        # If it looks like it might be JSON data, try to format it
                        if response_str.strip().startswith('{') or response_str.strip().startswith('['):
                            formatted_data = json.loads(response_str)
                            if isinstance(formatted_data, list):
                                user_metrics_data = formatted_data
                            elif isinstance(formatted_data, dict):
                                user_metrics_data = [formatted_data]
                            else:
                                logger.error(f"Formatted data is neither dict nor list: {type(formatted_data)}")
                                continue
                        else:
                            logger.error(f"Response does not appear to be JSON format")
                            continue
                    except Exception as format_error:
                        logger.error(f"Failed to format response from download link {i}: {format_error}")
                        continue
                
                # Process each user metrics record
                for user_data in user_metrics_data:
                    if isinstance(user_data, dict):
                        # Calculate top values from nested data
                        top_values = calculate_top_values(user_data)
                        
                        # Add organizational context and metadata
                        # Enrich with team from seat assignments lookup so Grafana
                        # team filters work even though the new metrics API returns
                        # only org-level aggregates (no team breakdown).
                        user_login = user_data.get('user_login', '')
                        resolved_team = (
                            team_lookup.get(user_login, 'no-team')
                            if team_lookup
                            else 'no-team'
                        )
                        enriched_user_data = {
                            **user_data,
                            **top_values,  # Add calculated top values
                            'organization_slug': self.organization_slug,
                            'slug_type': self.slug_type,
                            'last_updated_at': current_time_str,
                            'utc_offset': self.utc_offset,
                            'download_link_index': i,
                            'team_slug': resolved_team,
                        }
                        
                        # Generate unique hash for deduplication (user + day combination)
                        hash_properties = ['organization_slug', 'user_login', 'day']
                        if 'user_login' in enriched_user_data and 'day' in enriched_user_data:
                            enriched_user_data['unique_hash'] = generate_unique_hash(
                                enriched_user_data, hash_properties
                            )
                        else:
                            # Fallback hash if expected fields are missing
                            fallback_properties = ['organization_slug', 'last_updated_at', 'download_link_index']
                            enriched_user_data['unique_hash'] = generate_unique_hash(
                                enriched_user_data, fallback_properties
                            )
                        
                        processed_data.append(enriched_user_data)
                
                logger.info(f"Processed {len(user_metrics_data)} user records from download link {i}")
                
            except Exception as e:
                logger.error(f"Error processing download link {i}: {str(e)}")
                continue
        
        # Save to JSON file for debugging/inspection
        dict_save_to_json_file(
            processed_data,
            f"{self.organization_slug}_copilot_user_metrics",
            save_to_json=save_to_json
        )
        
        logger.info(f"Processed {len(processed_data)} total user metrics records for {self.slug_type}: {self.organization_slug}")
        return processed_data

    def _add_fullpath_slug(self, teams):
        id_to_team = {team["id"]: team for team in teams}

        for team in teams:
            slugs = []
            current_team = team
            while current_team:
                slugs.append(current_team["slug"])
                parent = current_team.get("parent")
                if parent and "id" in parent:
                    current_team = id_to_team.get(parent["id"])
                else:
                    current_team = None
            team["fullpath_slug"] = "/".join(reversed(slugs))

        return teams


class DataSplitter:
    def __init__(self, data, additional_properties={}):
        self.data = data
        self.additional_properties = additional_properties
        self.correction_for_0 = 0

    def get_total_list(self):
        total_list = []
        logger.info("Generating total list from data")
        for entry in self.data:
            total_data = entry.copy()
            total_data.pop("breakdown", None)
            total_data.pop("breakdown_chat", None)
            total_data = total_data | self.additional_properties
            total_data["unique_hash"] = generate_unique_hash(
                total_data, key_properties=["organization_slug", "team_slug", "day"]
            )

            # If the denominator value is 0, it is corrected to a uniform value
            total_data["total_suggestions_count"] = (
                self.correction_for_0
                if total_data["total_suggestions_count"] == 0
                else total_data["total_suggestions_count"]
            )
            total_data["total_lines_suggested"] = (
                self.correction_for_0
                if total_data["total_lines_suggested"] == 0
                else total_data["total_lines_suggested"]
            )
            total_data["total_chat_turns"] = (
                self.correction_for_0
                if total_data["total_chat_turns"] == 0
                else total_data["total_chat_turns"]
            )

            total_list.append(total_data)
        return total_list

    def get_breakdown_list(self):
        breakdown_list = []
        logger.info("Generating breakdown list from data")
        for entry in self.data:
            day = entry.get("day")
            for breakdown_entry in entry.get("breakdown", []):
                breakdown_entry_with_day = breakdown_entry.copy()
                breakdown_entry_with_day["day"] = day
                breakdown_entry_with_day = (
                    breakdown_entry_with_day | self.additional_properties
                )

                # # Normalize editor and language values to lowercase
                # breakdown_entry_with_day['editor'] = breakdown_entry_with_day.get('editor', '').lower()
                # breakdown_entry_with_day['language'] = breakdown_entry_with_day.get('language', '').lower()

                # # Unify `json` and `json with comments` to `json`
                # if breakdown_entry_with_day['language'] == 'json with comments':
                #     breakdown_entry_with_day['language'] = 'json'

                breakdown_entry_with_day["unique_hash"] = generate_unique_hash(
                    breakdown_entry_with_day,
                    key_properties=[
                        "organization_slug",
                        "team_slug",
                        "day",
                        "language",
                        "editor",
                        "model",
                    ],
                )

                # If the denominator value is 0, it is corrected to a uniform value
                breakdown_entry_with_day["suggestions_count"] = (
                    self.correction_for_0
                    if breakdown_entry_with_day["suggestions_count"] == 0
                    else breakdown_entry_with_day["suggestions_count"]
                )
                breakdown_entry_with_day["lines_suggested"] = (
                    self.correction_for_0
                    if breakdown_entry_with_day["lines_suggested"] == 0
                    else breakdown_entry_with_day["lines_suggested"]
                )

                breakdown_list.append(breakdown_entry_with_day)
        return breakdown_list

    def get_breakdown_chat_list(self):
        breakdown_chat_list = []
        logger.info("Generating breakdown chat list from data")
        for entry in self.data:
            day = entry.get("day")
            for breakdown_chat_entry in entry.get("breakdown_chat", []):
                breakdown_chat_entry_with_day = breakdown_chat_entry.copy()
                breakdown_chat_entry_with_day["day"] = day
                breakdown_chat_entry_with_day = (
                    breakdown_chat_entry_with_day | self.additional_properties
                )

                breakdown_chat_entry_with_day["unique_hash"] = generate_unique_hash(
                    breakdown_chat_entry_with_day,
                    key_properties=[
                        "organization_slug",
                        "team_slug",
                        "day",
                        "editor",
                        "model",
                    ],
                )

                # If the denominator value is 0, it is corrected to a uniform value
                breakdown_chat_entry_with_day["chat_turns"] = (
                    self.correction_for_0
                    if breakdown_chat_entry_with_day["chat_turns"] == 0
                    else breakdown_chat_entry_with_day["chat_turns"]
                )

                breakdown_chat_list.append(breakdown_chat_entry_with_day)
        return breakdown_chat_list


class ElasticsearchManager:

    def __init__(self, primary_key=Paras.primary_key):
        self.primary_key = primary_key
        # Retry creating the ES client to handle transient DNS resolution failures
        # that can occur immediately after container startup.
        for attempt in range(1, 13):  # up to ~60 s of DNS-wait
            try:
                if Paras.elasticsearch_user is None or Paras.elasticsearch_pass is None:
                    logger.info("Using Elasticsearch without authentication")
                    self.es = Elasticsearch(
                        hosts=Paras.elasticsearch_url,
                        max_retries=3,
                        retry_on_timeout=True,
                        request_timeout=60,
                    )
                else:
                    logger.info("Using basic authentication for Elasticsearch")
                    self.es = Elasticsearch(
                        hosts=Paras.elasticsearch_url,
                        basic_auth=(Paras.elasticsearch_user, Paras.elasticsearch_pass),
                        max_retries=3,
                        retry_on_timeout=True,
                        request_timeout=60,
                    )
                # Verify we can reach the host before proceeding
                self.es.info()
                break
            except Exception as e:
                logger.warning(
                    f"Elasticsearch not reachable yet (attempt {attempt}/12): {e}. "
                    "Retrying in 5 s..."
                )
                time.sleep(5)
        else:
            logger.error(
                f"Could not connect to Elasticsearch at {Paras.elasticsearch_url} "
                "after 12 attempts. Check that the service is running and accessible."
            )
            raise RuntimeError("Elasticsearch unreachable after repeated DNS/connection failures")

        self.check_and_create_indexes()

    # Check if all indexes in the indexes are present, and if they don't, they are created based on the files in the mapping folder
    def check_and_create_indexes(self):

        # try ping for 1 minute — ES is already confirmed reachable from __init__
        for i in range(30):
            try:
                if self.es.ping():
                    logger.info("Elasticsearch is up and running")
                    break
                else:
                    logger.warning("Elasticsearch is not responding, retrying...")
                    time.sleep(5)
            except Exception as e:
                logger.warning(f"Elasticsearch ping error (retry {i+1}/30): {e}")
                time.sleep(5)

        for index_name in Indexes.__dict__:
            if index_name.startswith("index_"):
                index_name = Indexes.__dict__[index_name]
                if not self.es.indices.exists(index=index_name):
                    mapping_file = f"mapping/{index_name}_mapping.json"
                    with open(mapping_file, "r") as f:
                        mapping = json.load(f)
                    self.es.indices.create(index=index_name, body=mapping)
                    logger.info(f"Created index: {index_name}")
                else:
                    logger.info(f"Index already exists: {index_name}")
                    # Ensure single-node replica settings are correct on
                    # pre-existing indexes.  If indexes were created before the
                    # mapping files gained settings.index.number_of_replicas=0,
                    # they would have the ES default (1 replica).  On a
                    # single-node cluster this puts all shards into YELLOW
                    # state and can make queries return 503 errors.
                    try:
                        settings = self.es.indices.get_settings(index=index_name)
                        current_replicas = int(
                            settings.get(index_name, {})
                            .get("settings", {})
                            .get("index", {})
                            .get("number_of_replicas", 1)
                        )
                        if current_replicas != 0:
                            logger.warning(
                                f"Index {index_name} has number_of_replicas="
                                f"{current_replicas}, updating to 0 for single-node"
                            )
                            self.es.indices.put_settings(
                                index=index_name,
                                settings={"index": {"number_of_replicas": 0}},
                            )
                            logger.info(f"Updated {index_name} to number_of_replicas=0")
                    except Exception as e:
                        logger.warning(f"Could not check/update replica settings for {index_name}: {e}")

    def write_to_es(self, index_name, data, update_condition=None, max_retries=3):
        last_updated_at = current_time()
        data["last_updated_at"] = last_updated_at
        # Add @timestamp for Grafana time-based filtering (ISO 8601 format)
        data["@timestamp"] = datetime.now().isoformat()
        doc_id = data.get(self.primary_key)
        for attempt in range(1, max_retries + 1):
            try:
                # Get existing document
                existing_doc = self.es.get(index=index_name, id=doc_id)

                # Check update condition if provided
                if update_condition:
                    should_preserve_fields = True
                    for field, value in update_condition.items():
                        if (
                            field not in existing_doc["_source"]
                            or existing_doc["_source"][field] != value
                        ):
                            should_preserve_fields = False
                            break

                    if should_preserve_fields:
                        for field in update_condition.keys():
                            if field in existing_doc["_source"]:
                                data[field] = existing_doc["_source"][field]
                        logger.info(
                            f"[partial update] to [{index_name}]: {doc_id} - preserving fields: {list(update_condition.keys())}"
                        )

                # Always update document, possibly with some preserved fields
                self.es.update(index=index_name, id=doc_id, doc=data)
                logger.info(f"[updated] to [{index_name}]: {doc_id}")
                return  # success
            except NotFoundError:
                self.es.index(index=index_name, id=doc_id, document=data)
                logger.info(f"[created] to [{index_name}]: {doc_id}")
                return  # success
            except Exception as e:
                if attempt < max_retries:
                    logger.warning(
                        f"ES write error for {index_name}/{doc_id} "
                        f"(attempt {attempt}/{max_retries}): {e}. Retrying in 5s..."
                    )
                    time.sleep(5)
                else:
                    logger.error(
                        f"Failed to write to {index_name}/{doc_id} after "
                        f"{max_retries} attempts: {e}"
                    )


def create_breakdown_from_user_metrics(user_metrics_data, organization_slug, es_manager):
    """
    Synthesize enterprise-level copilot_usage_breakdown, copilot_usage_breakdown_chat,
    and per-team copilot_usage_total records by aggregating per-user metrics.

    The new GitHub Enterprise report API does not return per-day/per-language/per-editor
    aggregate breakdowns that the dashboard depends on.  This function reconstructs that
    data from the individual user-level records that *are* available
    (via /copilot/metrics/reports/users-28-day/latest), which contain:
      - totals_by_language_model  → language + model per user per day
      - totals_by_ide             → IDE/editor per user per day
      - code_generation_activity_count, code_acceptance_activity_count, etc.

    Populated indices:
      - copilot_usage_breakdown      (Languages / Editors panels)
      - copilot_usage_breakdown_chat (Chat-by-editor panel)
      - copilot_usage_total          (Teams panels — one entry per team per day)
    """
    from collections import defaultdict

    if not user_metrics_data:
        logger.warning("[Enterprise Synthesis] No user metrics data available; skipping breakdown synthesis")
        return

    logger.info(
        f"[Enterprise Synthesis] Aggregating {len(user_metrics_data)} user-day records "
        f"into enterprise breakdown data for org: {organization_slug}"
    )

    # ── Language + Model breakdown ────────────────────────────────────────────
    # Key: (day, language, model)
    lang_model_agg = defaultdict(lambda: {
        "suggestions_count": 0,
        "acceptances_count": 0,
        "lines_suggested": 0,
        "lines_accepted": 0,
        "active_users": 0,
    })

    # ── IDE / Editor breakdown ────────────────────────────────────────────────
    # Key: (day, ide)
    ide_agg = defaultdict(lambda: {
        "chat_turns": 0,
        "chat_copy_events": 0,
        "chat_insertion_events": 0,
        "chat_acceptances": 0,
        "active_users": 0,
    })

    # ── Team usage total ──────────────────────────────────────────────────────
    # Key: (day, team_slug)
    team_agg = defaultdict(lambda: {
        "total_suggestions_count": 0,
        "total_acceptances_count": 0,
        "total_lines_suggested": 0,
        "total_lines_accepted": 0,
        "total_active_users": 0,
        "total_active_chat_users": 0,
        "total_chat_turns": 0,
        "total_chat_acceptances": 0,
        "total_chat_copy_events": 0,
        "total_chat_insertion_events": 0,
    })

    for user in user_metrics_data:
        day = user.get("day")
        if not day:
            continue
        team_slug = user.get("team_slug", "no-team")

        # ── Language + Model aggregation ──────────────────────────────────────
        # Primary source: totals_by_language_model (has model granularity)
        lang_model_entries = user.get("totals_by_language_model", [])
        # Fallback source: totals_by_language_feature (populated even when
        # language_model is empty — enterprise API behaviour)
        lang_feature_entries = user.get("totals_by_language_feature", [])

        if lang_model_entries:
            for lm in lang_model_entries:
                language = lm.get("language", "unknown")
                model = lm.get("model", "unknown")
                key = (day, language, model)
                agg = lang_model_agg[key]
                code_count = lm.get("code_generation_activity_count", 0)
                accept_count = lm.get("code_acceptance_activity_count", 0)
                agg["suggestions_count"] += code_count
                agg["acceptances_count"] += accept_count
                agg["lines_suggested"] += lm.get("loc_suggested_to_add_sum", 0)
                agg["lines_accepted"] += lm.get("loc_added_sum", 0)
                if code_count > 0:
                    agg["active_users"] += 1
        elif lang_feature_entries:
            # Use totals_by_language_feature as fallback — no model field, use
            # feature name as a stand-in so entries remain distinct per language.
            for lf in lang_feature_entries:
                language = lf.get("language", "unknown")
                model = lf.get("feature", "enterprise-aggregate")
                key = (day, language, model)
                agg = lang_model_agg[key]
                code_count = lf.get("code_generation_activity_count", 0)
                accept_count = lf.get("code_acceptance_activity_count", 0)
                agg["suggestions_count"] += code_count
                agg["acceptances_count"] += accept_count
                agg["lines_suggested"] += lf.get("loc_suggested_to_add_sum", 0)
                agg["lines_accepted"] += lf.get("loc_added_sum", 0)
                if code_count > 0:
                    agg["active_users"] += 1

        # ── IDE / Editor aggregation ──────────────────────────────────────────
        for ide_entry in user.get("totals_by_ide", []):
            ide_name = ide_entry.get("ide", "unknown")
            key = (day, ide_name)
            agg = ide_agg[key]
            interaction_count = ide_entry.get("user_initiated_interaction_count", 0)
            agg["chat_turns"] += interaction_count
            agg["chat_acceptances"] += ide_entry.get("code_acceptance_activity_count", 0)
            if interaction_count > 0:
                agg["active_users"] += 1

        # ── Team usage aggregation ────────────────────────────────────────────
        key = (day, team_slug)
        tagg = team_agg[key]
        tagg["total_suggestions_count"] += user.get("code_generation_activity_count", 0)
        tagg["total_acceptances_count"] += user.get("code_acceptance_activity_count", 0)
        tagg["total_lines_suggested"] += user.get("loc_suggested_to_add_sum", 0)
        tagg["total_lines_accepted"] += user.get("loc_added_sum", 0)
        tagg["total_chat_turns"] += user.get("user_initiated_interaction_count", 0)
        if (user.get("code_generation_activity_count", 0) > 0
                or user.get("user_initiated_interaction_count", 0) > 0):
            tagg["total_active_users"] += 1
        if user.get("used_chat", False):
            tagg["total_active_chat_users"] += 1

    logger.info(
        f"[Enterprise Synthesis] Grouped into "
        f"{len(lang_model_agg)} language+model+day, "
        f"{len(ide_agg)} ide+day, "
        f"{len(team_agg)} team+day combinations"
    )

    # ── Write copilot_usage_breakdown (Languages panel) ───────────────────────
    breakdown_written = 0
    for (day, language, model), stats in lang_model_agg.items():
        entry = {
            "day": day,
            "language": language,
            "model": model,
            "editor": "enterprise-aggregate",
            "suggestions_count": stats["suggestions_count"],
            "acceptances_count": stats["acceptances_count"],
            "lines_suggested": stats["lines_suggested"],
            "lines_accepted": stats["lines_accepted"],
            "active_users": stats["active_users"],
            "organization_slug": organization_slug,
            "team_slug": "no-team",
            "position_in_tree": "root_team",
        }
        entry["unique_hash"] = generate_unique_hash(
            entry,
            key_properties=["organization_slug", "team_slug", "day", "language", "editor", "model"],
        )
        es_manager.write_to_es(Indexes.index_name_breakdown, entry)
        breakdown_written += 1
    logger.info(f"[Enterprise Synthesis] Wrote {breakdown_written} language/model breakdown entries")

    # ── Write copilot_usage_breakdown_chat (Editor/Chat panel) ────────────────
    chat_written = 0
    for (day, ide_name), stats in ide_agg.items():
        entry = {
            "day": day,
            "editor": ide_name,
            "model": "enterprise-aggregate",
            "chat_turns": stats["chat_turns"],
            "chat_copy_events": stats["chat_copy_events"],
            "chat_insertion_events": stats["chat_insertion_events"],
            "chat_acceptances": stats["chat_acceptances"],
            "active_users": stats["active_users"],
            "organization_slug": organization_slug,
            "team_slug": "no-team",
            "position_in_tree": "root_team",
        }
        entry["unique_hash"] = generate_unique_hash(
            entry,
            key_properties=["organization_slug", "team_slug", "day", "editor", "model"],
        )
        es_manager.write_to_es(Indexes.index_name_breakdown_chat, entry)
        chat_written += 1
    logger.info(f"[Enterprise Synthesis] Wrote {chat_written} editor/chat breakdown entries")

    # ── Write per-team copilot_usage_total (Teams panels) ────────────────────
    teams_found = {ts for (_, ts) in team_agg.keys()}
    team_written = 0
    for (day, team_slug), stats in team_agg.items():
        entry = {
            "day": day,
            "team_slug": team_slug,
            "position_in_tree": "leaf_team",
            "organization_slug": organization_slug,
            **stats,
        }
        entry["unique_hash"] = generate_unique_hash(
            entry,
            key_properties=["organization_slug", "team_slug", "day"],
        )
        es_manager.write_to_es(Indexes.index_name_total, entry)
        team_written += 1
    logger.info(
        f"[Enterprise Synthesis] Wrote {team_written} team-level usage_total entries "
        f"across {len(teams_found)} team(s): {sorted(teams_found)}"
    )


def main(organization_slug):
    logger.info(
        "=========================================================================================================="
    )

    # organization_slug 2 types:
    # 1. Organization in a GHEC, like "YOUR_ORG_SLUG"
    # 2. Standalone Slug, must be starts with "standalone:", like "standalone:YOUR_STANDALONE_SLUG"

    is_standalone = True if organization_slug.startswith("standalone:") else False
    slug_type = "Standalone" if is_standalone else "Organization"
    organization_slug = organization_slug.replace("standalone:", "")

    logger.info(f"Starting data processing for {slug_type}: {organization_slug}")
    github_org_manager = GitHubOrganizationManager(
        organization_slug, is_standalone=is_standalone
    )
    es_manager = ElasticsearchManager()

    # Process seat info and settings
    logger.info(
        f"Processing Copilot seat info & settings for {slug_type}: {organization_slug}"
    )
    data_seat_info_settings = (
        github_org_manager.get_seat_info_settings()
        if not is_standalone
        else github_org_manager.get_seat_info_settings_standalone()
    )
    if not data_seat_info_settings:
        logger.warning(
            f"No Copilot seat info & settings found for {slug_type}: {organization_slug}"
        )
    else:
        es_manager.write_to_es(Indexes.index_seat_info, data_seat_info_settings)
        logger.info(f"Data processing completed for {slug_type}: {organization_slug}")

    # Process seat assignments
    logger.info(
        f"Processing Copilot seat assignments for {slug_type}: {organization_slug}"
    )
    data_seat_assignments = github_org_manager.get_seat_assignments()
    if not data_seat_assignments:
        logger.warning(
            f"No Copilot seat assignments found for {slug_type}: {organization_slug}"
        )
    else:
        for seat_assignment in data_seat_assignments:
            es_manager.write_to_es(
                Indexes.index_seat_assignments,
                seat_assignment,
                update_condition={"is_active_today": 1},
            )
        logger.info(f"Data processing completed for {slug_type}: {organization_slug}")

    # Build a user_login -> team_slug lookup from seat assignments so user metrics
    # records can be enriched with team info (new metrics API has no team breakdown).
    team_lookup = {}
    if data_seat_assignments:
        for seat in data_seat_assignments:
            login = seat.get("assignee_login")
            team = seat.get("assignee_team_slug", "no-team")
            if login:
                team_lookup[login] = team
        logger.info(
            f"Built team lookup with {len(team_lookup)} users across "
            f"{len(set(team_lookup.values()))} teams for user metrics enrichment"
        )

    # Process user metrics data
    logger.info(
        f"Processing Copilot user metrics for {slug_type}: {organization_slug}"
    )
    user_metrics_data = []  # initialised here so it is visible after the try block
    try:
        logger.info("Calling get_copilot_user_metrics()...")
        user_metrics_data = github_org_manager.get_copilot_user_metrics(
            team_lookup=team_lookup
        )
        logger.info(f"get_copilot_user_metrics() returned: {type(user_metrics_data)} with {len(user_metrics_data) if user_metrics_data else 0} items")
        
        if not user_metrics_data:
            logger.warning(
                f"No Copilot user metrics found for {slug_type}: {organization_slug}"
            )
        else:
            logger.info(f"Writing {len(user_metrics_data)} user metrics to Elasticsearch...")
            for user_metric in user_metrics_data:
                es_manager.write_to_es(Indexes.index_user_metrics, user_metric)
            adoption_entries = build_user_adoption_leaderboard(
                user_metrics_data, organization_slug, slug_type
            )
            if adoption_entries:
                logger.info(
                    f"Writing {len(adoption_entries)} adoption leaderboard entries to Elasticsearch..."
                )
                for adoption_entry in adoption_entries:
                    es_manager.write_to_es(
                        Indexes.index_user_adoption, adoption_entry
                    )
            logger.info(f"Successfully processed {len(user_metrics_data)} user metrics records for {slug_type}: {organization_slug}")
    except Exception as e:
        logger.error(f"Failed to process user metrics for {slug_type} {organization_slug}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")

    # For enterprise/standalone mode: synthesise breakdown, breakdown_chat, and
    # per-team usage_total from per-user metrics so the Languages, Editors, and
    # Teams panels in Grafana are populated (the enterprise report API does not
    # provide these aggregates directly).
    if is_standalone and user_metrics_data:
        try:
            create_breakdown_from_user_metrics(
                user_metrics_data, organization_slug, es_manager
            )
        except Exception as e:
            logger.error(f"Failed to synthesise enterprise breakdown data: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")

    # Create user summaries with aggregated top_model/language/feature
    try:
        logger.info("Creating user summaries with aggregated top values...")
        create_user_summaries()
        logger.info("User summaries created successfully")
    except Exception as e:
        logger.error(f"Failed to create user summaries: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")

    # Create top-by-day docs for drill-down time series panels
    try:
        logger.info("Creating user top-by-day documents for drill-down...")
        create_user_top_by_day(
            source_index=Indexes.index_user_metrics,
            dest_index=os.getenv("INDEX_USER_METRICS_TOP_BY_DAY", "copilot_user_metrics_top_by_day"),
        )
        logger.info("User top-by-day documents created successfully")
    except Exception as e:
        logger.error(f"Failed to create user top-by-day documents: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")

    # Process usage data
    try:
        copilot_usage_datas = github_org_manager.get_copilot_usages(team_slug="all")
    except Exception as e:
        logger.error(f"Failed to fetch Copilot usage data for {slug_type} {organization_slug}: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        copilot_usage_datas = {}
    logger.info(f"Processing Copilot usage data for {slug_type}: {organization_slug}")
    for team_slug, data_with_position in copilot_usage_datas.items():
        logger.info(f"Processing Copilot usage data for team: {team_slug}")

        # Expand data
        data = data_with_position.get("copilot_usage_data")
        position_in_tree = data_with_position.get("position_in_tree")

        # Check if there is data
        if not data:
            logger.warning(f"No Copilot usage data found for team: {team_slug}")
            continue

        data_splitter = DataSplitter(
            data,
            additional_properties={
                "organization_slug": organization_slug,
                "team_slug": team_slug,
                "position_in_tree": position_in_tree,
            },
        )

        # get total_list, breakdown_list, breakdown_chat_list from data_splitter
        # and save to json file
        total_list = data_splitter.get_total_list()
        dict_save_to_json_file(total_list, f"{team_slug}_total_list")

        breakdown_list = data_splitter.get_breakdown_list()
        dict_save_to_json_file(breakdown_list, f"{team_slug}_breakdown_list")

        breakdown_chat_list = data_splitter.get_breakdown_chat_list()
        dict_save_to_json_file(breakdown_chat_list, f"{team_slug}_breakdown_chat_list")

        # Write to ES
        for total_data in total_list:
            es_manager.write_to_es(Indexes.index_name_total, total_data)

        for breakdown_data in breakdown_list:
            es_manager.write_to_es(Indexes.index_name_breakdown, breakdown_data)

        for breakdown_chat_data in breakdown_chat_list:
            es_manager.write_to_es(
                Indexes.index_name_breakdown_chat, breakdown_chat_data
            )

        logger.info(f"Data processing completed for team: {team_slug}")


if __name__ == "__main__":
    import os
    
    # Get execution interval from environment (default: 1 hour)
    execution_interval_hours = int(os.getenv("EXECUTION_INTERVAL_HOURS", "1"))
    execution_interval_seconds = execution_interval_hours * 3600
    
    logger.info(f"Starting Copilot metrics collector with {execution_interval_hours}h interval")
    
    while True:
        try:
            logger.info(
                f"Starting data processing for organizations: {Paras.organization_slugs}"
            )
            # Split Paras.organization_slugs and process each organization, remember to remove spaces after splitting
            organization_slugs = Paras.organization_slugs.split(",")
            for organization_slug in organization_slugs:
                main(organization_slug.strip())
            
            logger.info("-----------------Finished Successfully-----------------")
            logger.info(f"Sleeping for {execution_interval_hours} hour(s) until next run...")
            time.sleep(execution_interval_seconds)
            
        except KeyboardInterrupt:
            logger.info("Received shutdown signal, exiting gracefully...")
            break
        except Exception as e:
            logger.error(f"An error occurred: {e}")
            logger.error(f"Full traceback: {traceback.format_exc()}")
            logger.info(f"Retrying in {execution_interval_hours} hour(s)...")
            time.sleep(execution_interval_seconds)
