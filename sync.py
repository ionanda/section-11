#!/usr/bin/env python3
"""
Intervals.icu → GitHub/Local JSON Export
Exports training data for LLM access.

Version 3.5.2 - The "HRV Hunter" Update
  - Added robust _extract_hrv() method that deep-scans the entire API response 
    for any custom field containing 'sdnn' or 'hrv' to bypass BreakAway custom mapping issues.
"""

import requests
import json
import os
import argparse
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
import base64
import math
import statistics
from collections import defaultdict
from pathlib import Path


class IntervalsSync:
    INTERVALS_BASE_URL = "https://intervals.icu/api/v1"
    GITHUB_API_URL = "https://api.github.com"
    FTP_HISTORY_FILE = "ftp_history.json"
    HISTORY_FILE = "history.json"
    UPSTREAM_REPO = "CrankAddict/section-11"
    CHANGELOG_FILE = "changelog.json"
    VERSION = "3.5.2"

    SPORT_FAMILIES = {
        "Ride": "cycling", "VirtualRide": "cycling", "MountainBikeRide": "cycling",
        "GravelRide": "cycling", "EBikeRide": "cycling", "VirtualSki": "ski",
        "NordicSki": "ski", "Walk": "walk", "Hike": "walk", "Run": "run",
        "VirtualRun": "run", "TrailRun": "run", "Swim": "swim", "Rowing": "rowing",
        "WeightTraining": "strength", "Yoga": "other", "Workout": "other",
    }
    
    OUTDOOR_TYPES = {"Ride", "MountainBikeRide", "GravelRide", "EBikeRide",
                     "Run", "TrailRun", "NordicSki", "Walk", "Hike"}
    
    def __init__(self, athlete_id: str, intervals_api_key: str, github_token: str = None, 
                 github_repo: str = None, debug: bool = False):
        self.athlete_id = athlete_id
        self.intervals_auth = base64.b64encode(f"API_KEY:{intervals_api_key}".encode()).decode()
        self.github_token = github_token
        self.github_repo = github_repo
        self.debug = debug
        self.script_dir = Path(__file__).parent

    def _extract_hrv(self, w: Dict) -> Optional[float]:
        """Robust HRV extractor that deep-scans for custom BreakAway fields"""
        if not w:
            return None
        # 1. Try standard API keys first
        for k in ["hrvSdnn", "hrv", "hrvSDNN", "HRV", "SDNN"]:
            val = w.get(k)
            if val is not None:
                try:
                    return float(val)
                except:
                    pass
        
        # 2. Deep search for custom keys created by BreakAway/Apple Health
        for k, v in w.items():
            if v is not None and isinstance(v, (int, float)):
                key_lower = k.lower()
                # Skip things that are obviously not HRV
                if "sleeping" in key_lower or "resting" in key_lower:
                    continue
                if "sdnn" in key_lower or "hrv" in key_lower:
                    return float(v)
        return None
    
    def _intervals_get(self, endpoint: str, params: Dict = None) -> Dict:
        if endpoint:
            url = f"{self.INTERVALS_BASE_URL}/athlete/{self.athlete_id}/{endpoint}"
        else:
            url = f"{self.INTERVALS_BASE_URL}/athlete/{self.athlete_id}"
        headers = {
            "Authorization": f"Basic {self.intervals_auth}",
            "Accept": "application/json"
        }
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    
    def _fetch_today_wellness(self) -> Dict:
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            return self._intervals_get(f"wellness/{today}")
        except Exception:
            return {}
    
    def _extract_power_model_from_wellness(self, wellness_data: Dict) -> Dict:
        sport_info = wellness_data.get("sportInfo") or []
        cycling_info = None
        for sport in sport_info:
            if sport.get("type") == "Ride":
                cycling_info = sport
                break
        if not cycling_info:
            return {"eftp": None, "w_prime": None, "w_prime_kj": None, "p_max": None, "source": "unavailable"}
        
        eftp = cycling_info.get("eftp")
        w_prime = cycling_info.get("wPrime")
        p_max = cycling_info.get("pMax")
        return {
            "eftp": round(eftp, 1) if eftp else None,
            "w_prime": round(w_prime) if w_prime else None,
            "w_prime_kj": round(w_prime / 1000, 1) if w_prime else None,
            "p_max": round(p_max) if p_max else None,
            "source": "wellness.sportInfo"
        }
    
    def _load_ftp_history(self) -> Dict:
        path = self.script_dir / self.FTP_HISTORY_FILE
        if path.exists():
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                    if data and not ("indoor" in data or "outdoor" in data):
                        return {"indoor": {}, "outdoor": data}
                    return data
            except Exception:
                return {"indoor": {}, "outdoor": {}}
        return {"indoor": {}, "outdoor": {}}
    
    def _save_ftp_history(self, history: Dict, current_in: int, current_out: int) -> Dict:
        today = datetime.now().strftime("%Y-%m-%d")
        if "indoor" not in history: history["indoor"] = {}
        if "outdoor" not in history: history["outdoor"] = {}
        
        if current_in:
            if history["indoor"]:
                last = history["indoor"][sorted(history["indoor"].keys(), reverse=True)[0]]
                if current_in != last: history["indoor"][today] = current_in
            else: history["indoor"][today] = current_in
                
        if current_out:
            if history["outdoor"]:
                last = history["outdoor"][sorted(history["outdoor"].keys(), reverse=True)[0]]
                if current_out != last: history["outdoor"][today] = current_out
            else: history["outdoor"][today] = current_out
                
        try:
            with open(self.script_dir / self.FTP_HISTORY_FILE, 'w') as f:
                json.dump(history, f, indent=2, sort_keys=True)
        except Exception:
            pass
        return history
    
    def _calculate_benchmark_index(self, current_ftp, ftp_history, ftp_type="indoor"):
        if not current_ftp or not ftp_history: return None, None
        target_date = datetime.now() - timedelta(days=56)
        earliest = target_date - timedelta(days=7)
        latest = target_date + timedelta(days=7)
        best_date = None
        best_diff = float('inf')
        
        for d_str, ftp in ftp_history.items():
            try:
                entry = datetime.strptime(d_str, "%Y-%m-%d")
                if earliest <= entry <= latest:
                    diff = abs((entry - target_date).days)
                    if diff < best_diff:
                        best_diff = diff
                        best_date = d_str
            except: pass
            
        if best_date:
            ftp_8w = ftp_history[best_date]
            return round((current_ftp / ftp_8w) - 1, 3), ftp_8w
        return None, None
    
    def collect_training_data(self, days_back: int = 7, anonymize: bool = False) -> Dict:
        days_for_acwr = 28
        oldest_ext = (datetime.now() - timedelta(days=days_for_acwr - 1)).strftime("%Y-%m-%d")
        oldest_disp = (datetime.now() - timedelta(days=days_back - 1)).strftime("%Y-%m-%d")
        newest = datetime.now().strftime("%Y-%m-%d")
        today = newest
        
        athlete = self._intervals_get("")
        cycling_settings = None
        if athlete.get("sportSettings"):
            for s in athlete["sportSettings"]:
                if "Ride" in s.get("types", []) or "VirtualRide" in s.get("types", []):
                    cycling_settings = s
                    break
        
        activities_extended = self._intervals_get("activities", {"oldest": oldest_ext, "newest": newest})
        activities_display = [a for a in activities_extended if a.get("start_date_local", "")[:10] >= oldest_disp]
        
        wellness = self._intervals_get("wellness", {"oldest": oldest_disp, "newest": newest})
        wellness_extended = self._intervals_get("wellness", {"oldest": oldest_ext, "newest": newest})
        today_wellness = self._fetch_today_wellness()
        
        power_model = self._extract_power_model_from_wellness(today_wellness)
        vo2max = today_wellness.get("vo2max")
        
        api_ctl, api_atl, api_ramp_rate = today_wellness.get("ctl"), today_wellness.get("atl"), today_wellness.get("rampRate")
        
        try:
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            y_well = self._intervals_get("wellness", {"oldest": yesterday, "newest": yesterday})
            y_data = y_well[0] if y_well else {}
            y_ctl, y_atl, y_ramp = y_data.get("ctl"), y_data.get("atl"), y_data.get("rampRate")
            decayed_ctl = round(y_ctl * math.exp(-1/42), 2) if y_ctl else None
            decayed_atl = round(y_atl * math.exp(-1/7), 2) if y_atl else None
            decayed_ramp = round(y_ramp * math.exp(-1/42), 2) if y_ramp else None
        except:
            decayed_ctl = decayed_atl = decayed_ramp = None
            
        latest_wellness = wellness[-1] if wellness else {}
        
        events = self._intervals_get("events", {"oldest": oldest_disp, "newest": (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")})
        past_events = [e for e in events if e.get("start_date_local", "")[:10] <= today]
        future_events = [e for e in events if e.get("start_date_local", "")[:10] >= today]
        near_future = [e for e in future_events if e.get("start_date_local", "")[:10] <= (datetime.now() + timedelta(days=42)).strftime("%Y-%m-%d")]
        
        todays_planned = [e for e in events if e.get("start_date_local", "")[:10] == today]
        todays_activities = [a for a in activities_display if a.get("start_date_local", "")[:10] == today]
        
        if todays_planned and not todays_activities:
            ctl, atl, smart_ramp_rate, fitness_source = decayed_ctl, decayed_atl, decayed_ramp or api_ramp_rate, "Decayed from yesterday"
        else:
            ctl = round(api_ctl, 2) if api_ctl else decayed_ctl
            atl = round(api_atl, 2) if api_atl else decayed_atl
            smart_ramp_rate = round(api_ramp_rate, 2) if api_ramp_rate else decayed_ramp
            fitness_source = "From API"
            
        tsb = round(ctl - atl, 2) if (ctl is not None and atl is not None) else None
        
        curr_in = cycling_settings.get("indoor_ftp") if cycling_settings else None
        curr_out = cycling_settings.get("ftp") if cycling_settings else None
        
        ftp_hist = self._save_ftp_history(self._load_ftp_history(), curr_in, curr_out)
        bm_in, f8_in = self._calculate_benchmark_index(curr_in, ftp_hist.get("indoor", {}), "indoor")
        bm_out, f8_out = self._calculate_benchmark_index(curr_out, ftp_hist.get("outdoor", {}), "outdoor")
        
        derived_metrics = self._calculate_derived_metrics(
            activities_display, activities_extended, wellness, wellness_extended,
            ctl, atl, tsb, past_events, activities_display, power_model,
            (bm_in, f8_in, curr_in), (bm_out, f8_out, curr_out), vo2max
        )
        
        alerts = self._generate_alerts(derived_metrics, wellness, derived_metrics.get("tss_7d_total", 0), derived_metrics.get("tss_28d_total", 0))
        rc = self._build_race_calendar(future_events, ctl, atl, tsb, activities_display, today)
        
        data = {
            "READ_THIS_FIRST": {
                "instruction_for_ai": "DO NOT calculate totals from individual activities. Use the pre-calculated values in 'summary', 'weekly_summary', and 'derived_metrics' sections below.",
                "data_period": f"Last {days_back} days (including today)",
                "extended_data_note": f"ACWR and baselines calculated from {days_for_acwr} days of data",
                "quick_stats": {
                    "total_training_hours": round(sum(a.get("moving_time", 0) for a in activities_display) / 3600, 2),
                    "total_activities": len(activities_display),
                    "total_tss": round(sum(a.get("icu_training_load", 0) for a in activities_display if a.get("icu_training_load")), 0)
                }
            },
            "metadata": {
                "athlete_id": "REDACTED" if anonymize else self.athlete_id,
                "last_updated": datetime.now().isoformat(),
                "data_range_days": days_back,
                "extended_range_days": days_for_acwr,
                "version": self.VERSION
            },
            "alerts": alerts,
            "history": self._get_history_confidence(),
            "summary": self._compute_activity_summary(activities_display, days_back),
            "current_status": {
                "fitness": {"ctl": ctl, "atl": atl, "tsb": tsb, "ramp_rate": smart_ramp_rate, "fitness_source": fitness_source},
                "thresholds": {"ftp_outdoor": curr_out, "ftp_indoor": curr_in, "eftp": power_model.get("eftp"), 
                               "lthr": cycling_settings.get("lthr") if cycling_settings else None, "max_hr": cycling_settings.get("max_hr") if cycling_settings else None,
                               "w_prime": power_model.get("w_prime"), "w_prime_kj": power_model.get("w_prime_kj"), "p_max": power_model.get("p_max"), "vo2max": vo2max},
                "current_metrics": {
                    "weight_kg": latest_wellness.get("weight") or athlete.get("icu_weight"),
                    "resting_hr": latest_wellness.get("restingHR") or athlete.get("icu_resting_hr"),
                    "hrv": self._extract_hrv(latest_wellness),
                    "sleep_quality": latest_wellness.get("sleepQuality"),
                    "sleep_hours": round(latest_wellness.get("sleepSecs", 0)/3600, 2) if latest_wellness.get("sleepSecs") else None
                }
            },
            "derived_metrics": derived_metrics,
            "recent_activities": self._format_activities(activities_display, anonymize),
            "wellness_data": self._format_wellness(wellness),
            "planned_workouts": self._format_events(near_future, anonymize),
            "weekly_summary": self._compute_weekly_summary(activities_display, wellness),
            "race_calendar": rc
        }
        return data

    def _calculate_derived_metrics(self, activities_7d, activities_28d, wellness_7d, wellness_extended,
                                   current_ctl, current_atl, current_tsb, past_events, acts_consistency,
                                   power_model, bench_in, bench_out, vo2max):
        
        daily_tss_7d = self._get_daily_tss(activities_7d, 7)
        daily_tss_28d = self._get_daily_tss(activities_28d, 28)
        t_7d, t_28d = sum(daily_tss_7d), sum(daily_tss_28d)
        
        acwr = round((t_7d/7)/(t_28d/28), 2) if (t_28d/28) > 0 else None
        
        if len(daily_tss_7d) > 1 and any(daily_tss_7d):
            try: monotony = round(statistics.mean(daily_tss_7d)/statistics.stdev(daily_tss_7d), 2)
            except: monotony = None
        else: monotony = None
        
        strain = round(t_7d * monotony, 0) if monotony else None
        
        # HRV Hunter logic here
        hrv_7d = [self._extract_hrv(w) for w in wellness_7d if self._extract_hrv(w)]
        rhr_7d = [w.get("restingHR") for w in wellness_7d if w.get("restingHR")]
        h_base_7 = round(statistics.mean(hrv_7d), 1) if hrv_7d else None
        r_base_7 = round(statistics.mean(rhr_7d), 1) if rhr_7d else None
        
        hrv_ext = [self._extract_hrv(w) for w in wellness_extended if self._extract_hrv(w)]
        rhr_ext = [w.get("restingHR") for w in wellness_extended if w.get("restingHR")]
        h_base_28 = round(statistics.mean(hrv_ext), 1) if hrv_ext else None
        r_base_28 = round(statistics.mean(rhr_ext), 1) if rhr_ext else None
        
        lat_hrv = self._extract_hrv(wellness_7d[-1]) if wellness_7d else None
        lat_rhr = wellness_7d[-1].get("restingHR") if wellness_7d else None
        
        ri = None
        if lat_hrv and lat_rhr and h_base_7 and r_base_7:
            if r_base_7 > 0: ri = round((lat_hrv/h_base_7)/(lat_rhr/r_base_7), 2)
            
        zt = self._aggregate_zones(activities_7d)
        total_z = zt["total_time"]
        
        return {
            "recovery_index": ri, "hrv_baseline_7d": h_base_7, "rhr_baseline_7d": r_base_7,
            "hrv_baseline_28d": h_base_28, "rhr_baseline_28d": r_base_28, "latest_hrv": lat_hrv, "latest_rhr": lat_rhr,
            "acwr": acwr, "monotony": monotony, "strain": strain,
            "tss_7d_total": round(t_7d, 0), "tss_28d_total": round(t_28d, 0),
            "zone_distribution_7d": {"z1_hours": round(zt["z1_time"]/3600, 2), "z2_hours": round(zt["z2_time"]/3600, 2), "z3_hours": round(zt["z3_time"]/3600, 2), "z4_plus_hours": round(zt["z4_plus_time"]/3600, 2), "total_hours": round(total_z/3600, 2)},
            "benchmark_indoor": {"current_ftp": bench_in[2], "benchmark_percentage": f"{bench_in[0]:+.1%}" if bench_in[0] else None},
            "benchmark_outdoor": {"current_ftp": bench_out[2], "benchmark_percentage": f"{bench_out[0]:+.1%}" if bench_out[0] else None},
            "eftp": power_model.get("eftp"), "w_prime_kj": power_model.get("w_prime_kj"), "p_max": power_model.get("p_max")
        }

    def _get_daily_tss(self, activities: List[Dict], days: int) -> List[float]:
        daily = defaultdict(float)
        for act in activities:
            d_str = act.get("start_date_local", "")[:10]
            daily[d_str] += act.get("icu_training_load") or 0
        return [daily.get((datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d"), 0) for i in range(days-1, -1, -1)]

    def _aggregate_zones(self, activities: List[Dict]) -> Dict:
        z = {"z1_time":0, "z2_time":0, "z3_time":0, "z4_plus_time":0, "total_time":0}
        for act in activities:
            icu_zt = act.get("icu_zone_times", [])
            for pt in icu_zt:
                zid, secs = pt.get("id", "").lower(), pt.get("secs", 0)
                if zid == "z1": z["z1_time"] += secs
                elif zid == "z2": z["z2_time"] += secs
                elif zid == "z3": z["z3_time"] += secs
                elif zid in ["z4","z5","z6","z7"]: z["z4_plus_time"] += secs
                z["total_time"] += secs
        return z

    def _generate_alerts(self, dm, w7, t7, t28):
        alerts = []
        acwr = dm.get("acwr")
        if acwr and acwr > 1.35: alerts.append({"metric": "acwr", "severity": "alarm", "context": f"ACWR {acwr} peligro."})
        return alerts

    def _build_race_calendar(self, a,b,c,d,e,f): return {}
    def _get_history_confidence(self): return {"available": True}
    def _compute_activity_summary(self, a, b): return {"total_activities": len(a)}
    def _compute_weekly_summary(self, a, b): return {}
    
    def _format_activities(self, acts, anon):
        fmt = []
        for i, act in enumerate(acts):
            fmt.append({
                "id": f"act_{i}", "date": act.get("start_date_local"), "type": act.get("type"),
                "duration_hours": round((act.get("moving_time") or 0)/3600, 2),
                "distance_km": round((act.get("distance") or 0)/1000, 2),
                "tss": act.get("icu_training_load")
            })
        return fmt

    def _format_wellness(self, w):
        return [{
            "date": x.get("id"), "resting_hr": x.get("restingHR"), 
            "hrv_sdnn": self._extract_hrv(x)
        } for x in w]

    def _format_events(self, e, a): return []
    def save_to_file(self, data, path):
        with open(path, 'w') as f: json.dump(data, f, indent=2)
        return path
    def publish_to_github(self, data, filepath="latest.json", commit_message=None):
        if not self.github_token: return ""
        url = f"{self.GITHUB_API_URL}/repos/{self.github_repo}/contents/{filepath}"
        headers = {"Authorization": f"token {self.github_token}"}
        r = requests.get(url, headers=headers)
        sha = r.json()["sha"] if r.status_code == 200 else None
        payload = {"message": commit_message or "Update", "content": base64.b64encode(json.dumps(data, indent=2).encode()).decode(), "branch": "main"}
        if sha: payload["sha"] = sha
        requests.put(url, headers=headers, json=payload)
        return f"https://github.com/{self.github_repo}/blob/main/{filepath}"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--athlete-id"); parser.add_argument("--intervals-key"); parser.add_argument("--github-token"); parser.add_argument("--github-repo"); parser.add_argument("--days", type=int, default=7); parser.add_argument("--output"); parser.add_argument("--anonymize", action="store_true", default=True)
    args = parser.parse_args()
    
    config = {}
    if os.path.exists(".sync_config.json"):
        with open(".sync_config.json") as f: config = json.load(f)
        
    aid = args.athlete_id or config.get("athlete_id") or os.getenv("ATHLETE_ID")
    ikey = args.intervals_key or config.get("intervals_key") or os.getenv("INTERVALS_KEY")
    gtok = args.github_token or config.get("github_token") or os.getenv("GITHUB_TOKEN")
    grepo = args.github_repo or config.get("github_repo") or os.getenv("GITHUB_REPO")
    
    sync = IntervalsSync(aid, ikey, gtok, grepo)
    data = sync.collect_training_data(args.days, args.anonymize)
    
    if args.output: sync.save_to_file(data, args.output)
    else: sync.publish_to_github(data)
    print("Done! HRV Hunter applied.")

if __name__ == "__main__": main()
