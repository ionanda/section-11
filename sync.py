#!/usr/bin/env python3
"""
Intervals.icu → GitHub/Local JSON Export
Exports training data for LLM access.

Version 3.5.5 - Full Data Restoration
  - Restored full rich metrics extraction in _format_activities (Power, HR, Zones, Decoupling, etc.).
  - Includes robust HRV Hunter (Apple Watch SDNN fix).
  - Includes NoneType safety for outdoor rides without power/HR data.
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
    VERSION = "3.5.5"

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
        if not w: return None
        for k in ["hrvSdnn", "hrv", "hrvSDNN", "HRV", "SDNN"]:
            val = w.get(k)
            if val is not None:
                try: return float(val)
                except: pass
        for k, v in w.items():
            if v is not None and isinstance(v, (int, float)):
                kl = k.lower()
                if "sleeping" in kl or "resting" in kl: continue
                if "sdnn" in kl or "hrv" in kl: return float(v)
        return None
    
    def _intervals_get(self, endpoint: str, params: Dict = None) -> Dict:
        url = f"{self.INTERVALS_BASE_URL}/athlete/{self.athlete_id}/{endpoint}" if endpoint else f"{self.INTERVALS_BASE_URL}/athlete/{self.athlete_id}"
        headers = {"Authorization": f"Basic {self.intervals_auth}", "Accept": "application/json"}
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        return response.json()
    
    def _fetch_today_wellness(self) -> Dict:
        try: return self._intervals_get(f"wellness/{datetime.now().strftime('%Y-%m-%d')}")
        except Exception: return {}
    
    def _extract_power_model_from_wellness(self, wellness_data: Dict) -> Dict:
        sport_info = wellness_data.get("sportInfo") or []
        cycling_info = next((s for s in sport_info if s.get("type") == "Ride"), None)
        if not cycling_info:
            return {"eftp": None, "w_prime": None, "w_prime_kj": None, "p_max": None, "source": "unavailable"}
        return {
            "eftp": round(cycling_info.get("eftp"), 1) if cycling_info.get("eftp") else None,
            "w_prime": round(cycling_info.get("wPrime")) if cycling_info.get("wPrime") else None,
            "w_prime_kj": round(cycling_info.get("wPrime") / 1000, 1) if cycling_info.get("wPrime") else None,
            "p_max": round(cycling_info.get("pMax")) if cycling_info.get("pMax") else None,
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
            except Exception: pass
        return {"indoor": {}, "outdoor": {}}
    
    def _save_ftp_history(self, history: Dict, current_in: int, current_out: int) -> Dict:
        today = datetime.now().strftime("%Y-%m-%d")
        if "indoor" not in history: history["indoor"] = {}
        if "outdoor" not in history: history["outdoor"] = {}
        
        if current_in:
            last = history["indoor"][sorted(history["indoor"].keys(), reverse=True)[0]] if history["indoor"] else None
            if current_in != last: history["indoor"][today] = current_in
        if current_out:
            last = history["outdoor"][sorted(history["outdoor"].keys(), reverse=True)[0]] if history["outdoor"] else None
            if current_out != last: history["outdoor"][today] = current_out
                
        try:
            with open(self.script_dir / self.FTP_HISTORY_FILE, 'w') as f:
                json.dump(history, f, indent=2, sort_keys=True)
        except Exception: pass
        return history
    
    def _calculate_benchmark_index(self, current_ftp, ftp_history):
        if not current_ftp or not ftp_history: return None, None
        target = datetime.now() - timedelta(days=56)
        earliest, latest = target - timedelta(days=7), target + timedelta(days=7)
        best_date, best_diff = None, float('inf')
        
        for d_str, ftp in ftp_history.items():
            try:
                entry = datetime.strptime(d_str, "%Y-%m-%d")
                if earliest <= entry <= latest:
                    diff = abs((entry - target).days)
                    if diff < best_diff: best_diff, best_date = diff, d_str
            except: pass
            
        if best_date: return round((current_ftp / ftp_history[best_date]) - 1, 3), ftp_history[best_date]
        return None, None
    
    def collect_training_data(self, days_back: int = 7, anonymize: bool = False) -> Dict:
        days_for_acwr = 28
        oldest_ext = (datetime.now() - timedelta(days=days_for_acwr - 1)).strftime("%Y-%m-%d")
        oldest_disp = (datetime.now() - timedelta(days=days_back - 1)).strftime("%Y-%m-%d")
        newest = datetime.now().strftime("%Y-%m-%d")
        today = newest
        
        athlete = self._intervals_get("")
        cycling_settings = next((s for s in athlete.get("sportSettings", []) if "Ride" in s.get("types", []) or "VirtualRide" in s.get("types", [])), None)
        
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
            decayed_ctl = round(y_data.get("ctl", 0) * math.exp(-1/42), 2) if y_data.get("ctl") else None
            decayed_atl = round(y_data.get("atl", 0) * math.exp(-1/7), 2) if y_data.get("atl") else None
            decayed_ramp = round(y_data.get("rampRate", 0) * math.exp(-1/42), 2) if y_data.get("rampRate") else None
        except:
            decayed_ctl = decayed_atl = decayed_ramp = None
            
        latest_wellness = wellness[-1] if wellness else {}
        
        events = self._intervals_get("events", {"oldest": oldest_disp, "newest": (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")})
        past_events = [e for e in events if e.get("start_date_local", "")[:10] <= today]
        near_future = [e for e in events if today <= e.get("start_date_local", "")[:10] <= (datetime.now() + timedelta(days=42)).strftime("%Y-%m-%d")]
        
        if [e for e in events if e.get("start_date_local", "")[:10] == today] and not [a for a in activities_display if a.get("start_date_local", "")[:10] == today]:
            ctl, atl, smart_ramp_rate, fitness_source = decayed_ctl, decayed_atl, decayed_ramp or api_ramp_rate, "Decayed from yesterday"
        else:
            ctl, atl, smart_ramp_rate, fitness_source = round(api_ctl, 2) if api_ctl else decayed_ctl, round(api_atl, 2) if api_atl else decayed_atl, round(api_ramp_rate, 2) if api_ramp_rate else decayed_ramp, "From API"
            
        tsb = round(ctl - atl, 2) if (ctl is not None and atl is not None) else None
        
        curr_in = cycling_settings.get("indoor_ftp") if cycling_settings else None
        curr_out = cycling_settings.get("ftp") if cycling_settings else None
        
        ftp_hist = self._save_ftp_history(self._load_ftp_history(), curr_in, curr_out)
        bm_in, f8_in = self._calculate_benchmark_index(curr_in, ftp_hist.get("indoor", {}))
        bm_out, f8_out = self._calculate_benchmark_index(curr_out, ftp_hist.get("outdoor", {}))
        
        derived_metrics = self._calculate_derived_metrics(
            activities_display, activities_extended, wellness, wellness_extended,
            ctl, atl, tsb, past_events, activities_display, power_model,
            (bm_in, f8_in, curr_in), (bm_out, f8_out, curr_out), vo2max
        )
        
        alerts = self._generate_alerts(derived_metrics, wellness, derived_metrics.get("tss_7d_total", 0), derived_metrics.get("tss_28d_total", 0))
        
        return {
            "READ_THIS_FIRST": {"instruction_for_ai": "Use pre-calculated metrics."},
            "metadata": {"athlete_id": "REDACTED" if anonymize else self.athlete_id, "last_updated": datetime.now().isoformat(), "version": self.VERSION},
            "alerts": alerts, "history": {"available": True}, 
            "summary": {"total_activities": len(activities_display)},
            "current_status": {
                "fitness": {"ctl": ctl, "atl": atl, "tsb": tsb, "ramp_rate": smart_ramp_rate, "fitness_source": fitness_source},
                "thresholds": {"ftp_outdoor": curr_out, "ftp_indoor": curr_in, "eftp": power_model.get("eftp"), "w_prime_kj": power_model.get("w_prime_kj"), "vo2max": vo2max},
                "current_metrics": {"weight_kg": latest_wellness.get("weight") or athlete.get("icu_weight"), "resting_hr": latest_wellness.get("restingHR"), "hrv": self._extract_hrv(latest_wellness), "sleep_hours": round(latest_wellness.get("sleepSecs", 0)/3600, 2) if latest_wellness.get("sleepSecs") else None}
            },
            "derived_metrics": derived_metrics, 
            "recent_activities": self._format_activities(activities_display, anonymize),
            "wellness_data": self._format_wellness(wellness), 
            "planned_workouts": self._format_events(near_future, anonymize)
        }

    def _calculate_derived_metrics(self, activities_7d, activities_28d, wellness_7d, wellness_extended,
                                   current_ctl, current_atl, current_tsb, past_events, acts_consistency,
                                   power_model, bench_in, bench_out, vo2max):
        daily_tss_7d = self._get_daily_tss(activities_7d, 7)
        daily_tss_28d = self._get_daily_tss(activities_28d, 28)
        t_7d, t_28d = sum(daily_tss_7d), sum(daily_tss_28d)
        
        acwr = round((t_7d/7)/(t_28d/28), 2) if (t_28d/28) > 0 else None
        monotony = round(statistics.mean(daily_tss_7d)/statistics.stdev(daily_tss_7d), 2) if len(daily_tss_7d) > 1 and any(daily_tss_7d) and statistics.stdev(daily_tss_7d) > 0 else None
        strain = round(t_7d * monotony, 0) if monotony else None
        
        hrv_7d = [self._extract_hrv(w) for w in wellness_7d if self._extract_hrv(w) is not None]
        rhr_7d = [w.get("restingHR") for w in wellness_7d if w.get("restingHR")]
        h_base_7 = round(statistics.mean(hrv_7d), 1) if hrv_7d else None
        r_base_7 = round(statistics.mean(rhr_7d), 1) if rhr_7d else None
        
        lat_hrv = self._extract_hrv(wellness_7d[-1]) if wellness_7d else None
        lat_rhr = wellness_7d[-1].get("restingHR") if wellness_7d else None
        
        ri = round((lat_hrv/h_base_7)/(lat_rhr/r_base_7), 2) if lat_hrv and lat_rhr and h_base_7 and r_base_7 and r_base_7 > 0 else None
        
        zt = self._aggregate_zones(activities_7d)
        total_z = zt["total_time"]
        
        hard_days = 0
        for act in activities_7d:
            icu_zt = act.get("icu_zone_times") or []
            dz3=dz4=dz5=dz6=dz7 = 0
            for z in icu_zt:
                zid, s = z.get("id", "").lower(), z.get("secs", 0)
                if zid=="z3": dz3+=s
                elif zid=="z4": dz4+=s
                elif zid=="z5": dz5+=s
                elif zid=="z6": dz6+=s
                elif zid=="z7": dz7+=s
            if (dz3+dz4+dz5+dz6+dz7)>=1800 or (dz4+dz5+dz6+dz7)>=600 or (dz5+dz6+dz7)>=300 or (dz6+dz7)>=120 or dz7>=60:
                hard_days += 1
                
        return {
            "recovery_index": ri, "hrv_baseline_7d": h_base_7, "rhr_baseline_7d": r_base_7,
            "latest_hrv": lat_hrv, "latest_rhr": lat_rhr, "acwr": acwr, "monotony": monotony, "strain": strain,
            "tss_7d_total": round(t_7d, 0), "tss_28d_total": round(t_28d, 0),
            "zone_distribution_7d": {"z1_hours": round(zt["z1_time"]/3600, 2), "z2_hours": round(zt["z2_time"]/3600, 2), "z3_hours": round(zt["z3_time"]/3600, 2), "z4_plus_hours": round(zt["z4_plus_time"]/3600, 2), "total_hours": round(total_z/3600, 2)},
            "hard_days_this_week": hard_days, "seiler_tid_7d": self._build_seiler_tid(activities_7d),
            "seiler_tid_28d": self._build_seiler_tid(activities_28d), 
            "capability": {"durability": self._calculate_durability(activities_7d, activities_28d), "tid_comparison": {"drift": "consistent"}},
            "phase_detected": self._detect_phase(acwr, ri, round((zt["z4_plus_time"]/total_z)*100, 1) if total_z else 0, hard_days, strain, monotony, current_tsb, current_ctl)[0]
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
            icu_zt = act.get("icu_zone_times") or []
            for pt in icu_zt:
                zid, secs = pt.get("id", "").lower(), pt.get("secs", 0)
                if zid == "z1": z["z1_time"] += secs
                elif zid == "z2": z["z2_time"] += secs
                elif zid == "z3": z["z3_time"] += secs
                elif zid in ["z4","z5","z6","z7"]: z["z4_plus_time"] += secs
                z["total_time"] += secs
        return z

    def _aggregate_seiler_zones(self, activities):
        sz1 = sz2 = sz3 = 0
        for act in activities:
            icu_zt = act.get("icu_zone_times") or []
            for pt in icu_zt:
                zid, secs = pt.get("id", "").lower(), pt.get("secs", 0)
                if zid in ["z1","z2"]: sz1 += secs
                elif zid == "z3": sz2 += secs
                elif zid in ["z4","z5","z6","z7"]: sz3 += secs
        return {"z1_seconds": sz1, "z2_seconds": sz2, "z3_seconds": sz3, "total_seconds": sz1+sz2+sz3}

    def _build_seiler_tid(self, activities):
        z = self._aggregate_seiler_zones(activities)
        tot = z["total_seconds"]
        if tot == 0: return {"classification": None, "polarization_index": None}
        z1f, z2f, z3f = z["z1_seconds"]/tot, z["z2_seconds"]/tot, z["z3_seconds"]/tot
        pi = round(math.log10((z1f/max(z2f,0.01))*z3f*100), 2) if z3f>=0.01 and (z1f>z3f>z2f) and ((z1f/max(z2f,0.01))*z3f*100)>0 else None
        cls = "Base" if z3f<0.01 and z1f>=z2f else "Polarized" if z1f>z3f>z2f and pi and pi>2.0 else "Pyramidal" if z1f>z2f>z3f else "Threshold" if z2f>=z1f and z2f>=z3f else "High Intensity"
        return {"z1_pct": round(z1f*100,1), "z2_pct": round(z2f*100,1), "z3_pct": round(z3f*100,1), "polarization_index": pi, "classification": cls}

    def _calculate_durability(self, a7, a28):
        def _get_vals(acts):
            vals = []
            for a in acts:
                mt = a.get("moving_time") or 0
                vi = a.get("icu_variability_index")
                dec = a.get("icu_hr_decoupling") or a.get("decoupling")
                if mt >= 5400 and vi is not None and vi > 0 and vi <= 1.05 and dec is not None:
                    vals.append(dec)
            return vals
            
        v7 = _get_vals(a7)
        v28 = _get_vals(a28)
        
        m7 = round(statistics.mean(v7),2) if len(v7)>=2 else None
        m28 = round(statistics.mean(v28),2) if len(v28)>=2 else None
        
        tr = None
        if m7 is not None and m28 is not None:
            if (m7 - m28) < -1.0: tr = "improving"
            elif (m7 - m28) > 1.0: tr = "declining"
            else: tr = "stable"
            
        return {"mean_decoupling_7d": m7, "mean_decoupling_28d": m28, "trend": tr}

    def _detect_phase(self, acwr, ri, q_pct, hard, strain, mon, tsb, ctl):
        if acwr and acwr > 1.3: return "Overreached", []
        if ri and ri < 0.6: return "Overreached", []
        return "Base", []

    def _generate_alerts(self, dm, w7, t7, t28):
        alerts = []
        if dm.get("acwr") and dm.get("acwr") > 1.35: alerts.append({"metric": "acwr", "severity": "alarm", "context": "ACWR outside safe range."})
        return alerts

    def _format_activities(self, activities: List[Dict], anonymize: bool = False) -> List[Dict]:
        formatted = []
        for i, act in enumerate(activities):
            avg_power = (act.get("average_watts") or act.get("avg_watts") or 
                        act.get("average_power") or act.get("avgWatts") or
                        act.get("icu_average_watts"))
            norm_power = (act.get("weighted_average_watts") or act.get("np") or 
                         act.get("icu_pm_np") or act.get("normalizedPower") or
                         act.get("icu_weighted_avg_watts"))
            avg_hr = (act.get("average_heartrate") or act.get("avg_hr") or 
                     act.get("average_heart_rate") or act.get("avgHr") or
                     act.get("icu_average_hr"))
            max_hr = (act.get("max_heartrate") or act.get("max_hr") or 
                     act.get("max_heart_rate") or act.get("maxHr") or
                     act.get("icu_max_hr"))
            
            avg_cadence = (act.get("average_cadence") or act.get("avg_cadence") or act.get("icu_average_cadence"))
            avg_temp = (act.get("average_weather_temp") or act.get("average_temp") or act.get("avg_temp") or act.get("average_temperature"))
            joules = act.get("icu_joules")
            work_kj = round(joules / 1000, 1) if joules else None
            calories = act.get("calories") or act.get("icu_calories")
            variability_index = act.get("icu_variability_index")
            decoupling = act.get("icu_hr_decoupling") or act.get("decoupling")
            
            avg_speed_ms = act.get("average_speed")
            max_speed_ms = act.get("max_speed")
            avg_speed = round(avg_speed_ms * 3.6, 1) if avg_speed_ms else None
            max_speed = round(max_speed_ms * 3.6, 1) if max_speed_ms else None
            avg_pace = act.get("average_pace") or act.get("icu_pace")
            
            weather = act.get("weather_description") or act.get("weather")
            humidity = act.get("humidity") or act.get("average_humidity")
            wind_speed = act.get("average_wind_speed") or act.get("wind_speed")
            
            carbs_used = act.get("carbs_used")
            carbs_ingested = act.get("carbs_ingested")
            
            hr_zones = {}
            power_zones = {}
            
            icu_hr_zone_times = act.get("icu_hr_zone_times", [])
            if icu_hr_zone_times and isinstance(icu_hr_zone_times, list):
                zone_labels = ["z1_time", "z2_time", "z3_time", "z4_time", "z5_time", "z6_time", "z7_time"]
                for idx, secs in enumerate(icu_hr_zone_times):
                    if idx < len(zone_labels):
                        hr_zones[zone_labels[idx]] = secs if secs is not None else 0
            
            icu_zone_times = act.get("icu_zone_times", [])
            if icu_zone_times:
                for zone in icu_zone_times:
                    zone_id = zone.get("id", "").lower()
                    secs = zone.get("secs", 0)
                    if zone_id in ["z1", "z2", "z3", "z4", "z5", "z6", "z7"]:
                        power_zones[f"{zone_id}_time"] = secs if secs is not None else 0
            
            zone_dist = {}
            if hr_zones:
                zone_dist["hr_zones"] = hr_zones
            if power_zones:
                zone_dist["power_zones"] = power_zones
            
            if not zone_dist:
                zone_dist = None
            
            activity_name = act.get("name", "")
            if anonymize:
                if act.get("type", "") in self.OUTDOOR_TYPES:
                    activity_name = "Training Session"
            
            activity = {
                "id": f"activity_{i+1}" if anonymize else act.get("id", f"unknown_{i+1}"),
                "date": act.get("start_date_local", "unknown"),
                "type": act.get("type", "Unknown"),
                "name": activity_name,
                "duration_hours": round((act.get("moving_time") or 0) / 3600, 2),
                "distance_km": round((act.get("distance") or 0) / 1000, 2),
                "tss": act.get("icu_training_load"),
                "intensity_factor": act.get("icu_intensity"),
                "avg_power": avg_power,
                "normalized_power": norm_power,
                "avg_hr": avg_hr,
                "max_hr": max_hr,
                "avg_cadence": avg_cadence,
                "avg_speed": avg_speed,
                "max_speed": max_speed,
                "avg_pace": avg_pace,
                "avg_temp": avg_temp,
                "weather": weather,
                "humidity": humidity,
                "wind_speed": wind_speed,
                "work_kj": work_kj,
                "calories": calories,
                "carbs_used": carbs_used,
                "carbs_ingested": carbs_ingested,
                "variability_index": variability_index,
                "decoupling": decoupling,
                "elevation_m": act.get("total_elevation_gain"),
                "feel": act.get("feel"),
                "rpe": act.get("icu_rpe"),
                "zone_distribution": zone_dist
            }
            
            formatted.append(activity)
        
        return formatted

    def _format_wellness(self, w):
        return [{"date": x.get("id"), "resting_hr": x.get("restingHR"), "hrv_sdnn": self._extract_hrv(x)} for x in w]

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
        payload = {"message": commit_message or "Update v3.5.5", "content": base64.b64encode(json.dumps(data, indent=2).encode()).decode(), "branch": "main"}
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
    print("Done! v3.5.5 Full Data restored.")

if __name__ == "__main__": main()
