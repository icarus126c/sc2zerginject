import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Tuple

import sc2reader
from sc2reader.events.game import TargetUnitCommandEvent
from sc2reader.events.tracker import (
    PlayerStatsEvent,
    UnitBornEvent,
    UnitDiedEvent,
    UnitDoneEvent,
    UnitInitEvent,
    UnitTypeChangeEvent,
)


INJECT_DELAY_SECONDS = 29
HATCHERY_TYPES = {"Hatchery", "Lair", "Hive"}
LARVA_TYPE = "Larva"
QUEEN_TYPES = {"Queen", "QueenBurrowed"}
INJECT_ABILITY_NAMES = {"SpawnLarva", "QueenSpawnLarva"}
LARVA_RELEVANCE_DECAY_START = 600


@dataclass
class HatcheryReport:
    hatchery_id: str
    inject_count: int
    expected_inject_count: float
    inject_coverage: float
    inject_uptime: float
    avg_cycle_gap: float
    avg_idle_time: float
    queued_injects_at_end: float
    larva_pressure: float
    score: float


@dataclass
class HatcheryState:
    unit_id: int
    label: str
    started_at: float
    location: Tuple[float, float]
    inject_times: List[float] = field(default_factory=list)
    larva_samples: List[Dict[str, float]] = field(default_factory=list)
    active_from: Optional[float] = None
    active_until: Optional[float] = None
    inject_ready_from: Optional[float] = None
    completed: bool = False


def clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def load_match(path: Path) -> Dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def replay_time_scale(replay) -> float:
    game_length = float(getattr(getattr(replay, "game_length", None), "seconds", 0.0) or 0.0)
    if game_length <= 0:
        return 1.0
    raw_seconds = float(getattr(replay, "frames", 0.0)) / 16.0
    return max(1.0, raw_seconds / game_length)


def event_second(event, time_scale: float = 1.0) -> float:
    second = getattr(event, "second", None)
    if second is not None:
        return float(second) / time_scale
    frame = getattr(event, "frame", getattr(event, "frames", 0))
    return float(frame) / 16.0 / time_scale


def distance_sq(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def normalize_inject_times(inject_times: List[float]) -> List[float]:
    normalized: List[float] = []
    for inject_time in sorted(inject_times):
        if not normalized or inject_time - normalized[-1] > 1.0:
            normalized.append(round(inject_time, 2))
    return normalized


def simulate_inject_queue(inject_times: List[float], active_from: float, active_until: float) -> Dict[str, float]:
    if active_until <= active_from:
        return {
            "coverage_percent": 0.0,
            "uptime_percent": 0.0,
            "completed_cycles": 0.0,
            "avg_cycle_gap": 0.0,
            "avg_idle_time": 0.0,
            "queued_injects_at_end": 0.0,
        }

    inject_times = normalize_inject_times(inject_times)
    potential_window = active_until - active_from
    expected_cycles = safe_div(potential_window, INJECT_DELAY_SECONDS)

    if not inject_times:
        return {
            "coverage_percent": 0.0,
            "uptime_percent": 0.0,
            "completed_cycles": 0.0,
            "avg_cycle_gap": potential_window,
            "avg_idle_time": potential_window,
            "queued_injects_at_end": 0.0,
        }

    queue_end = active_from
    covered_time = 0.0
    completed_cycles = 0.0
    idle_segments: List[float] = []

    for inject_time in inject_times:
        if inject_time > queue_end:
            idle_segments.append(inject_time - queue_end)

        segment_start = max(queue_end, inject_time)
        segment_end = segment_start + INJECT_DELAY_SECONDS

        clipped_start = max(segment_start, active_from)
        clipped_end = min(segment_end, active_until)
        if clipped_end > clipped_start:
            covered_time += clipped_end - clipped_start
        if segment_end <= active_until:
            completed_cycles += 1.0

        queue_end = segment_end

    if queue_end < active_until:
        idle_segments.append(active_until - queue_end)

    cycle_gaps = [current - previous for previous, current in zip(inject_times, inject_times[1:])]
    coverage_percent = clamp(safe_div(completed_cycles, expected_cycles) * 100)
    uptime_percent = clamp(safe_div(covered_time, potential_window) * 100)

    return {
        "coverage_percent": coverage_percent,
        "uptime_percent": uptime_percent,
        "completed_cycles": completed_cycles,
        "avg_cycle_gap": mean(cycle_gaps) if cycle_gaps else potential_window,
        "avg_idle_time": mean(idle_segments) if idle_segments else 0.0,
        "queued_injects_at_end": max(0.0, queue_end - active_until) / INJECT_DELAY_SECONDS,
    }


def compute_larva_pressure(larva_samples: List[Dict[str, float]]) -> float:
    if not larva_samples:
        return 0.0

    weighted_penalty = 0.0
    total_weight = 0.0
    for sample in larva_samples:
        time_sec = float(sample.get("time", 0.0))
        larva = float(sample.get("larva", 0.0))
        food_used = sample.get("food_used")
        food_used_value = float(food_used) if food_used is not None else None

        time_factor = clamp((time_sec - LARVA_RELEVANCE_DECAY_START) / 600.0, 0.0, 1.0)
        supply_factor = 0.0
        if food_used_value is not None:
            supply_factor = clamp((food_used_value - 120.0) / 80.0, 0.0, 1.0)

        threshold = 8.0 + 3.0 * time_factor + 2.0 * supply_factor
        sample_penalty = clamp((larva - threshold) / 6.0, 0.0, 1.0)
        larva_relief = clamp((larva - 8.0) / 10.0, 0.0, 1.0)
        weight = clamp(1.0 - 0.45 * time_factor - 0.25 * supply_factor - 0.15 * larva_relief, 0.15, 1.0)

        weighted_penalty += weight * sample_penalty
        total_weight += weight

    return safe_div(weighted_penalty, total_weight)


def build_larva_total_timeline(
    larva_birth_times: List[float],
    player_stat_samples: List[Dict[str, float]],
) -> List[Dict[str, float]]:
    births = sorted(round(item, 2) for item in larva_birth_times)
    if not births and not player_stat_samples:
        return []

    if not player_stat_samples:
        return [
            {
                "time": birth_time,
                "total_larva_gained": index + 1,
            }
            for index, birth_time in enumerate(births)
        ]

    timeline: List[Dict[str, float]] = []
    birth_index = 0
    total_larva_gained = 0

    for sample in player_stat_samples:
        sample_time = float(sample["time"])
        while birth_index < len(births) and births[birth_index] <= sample_time + 1e-9:
            total_larva_gained += 1
            birth_index += 1

        timeline_sample = {
            "time": round(sample_time, 2),
            "total_larva_gained": total_larva_gained,
            "larva_on_hand": int(sample.get("larva_on_hand", 0)),
        }
        if "food_used" in sample:
            timeline_sample["food_used"] = round(float(sample["food_used"]), 2)
        timeline.append(timeline_sample)

    return timeline


def summarize_larva_total_timeline(timeline: List[Dict[str, float]], match_end: float) -> Dict[str, float]:
    total_larva_gained = int(timeline[-1]["total_larva_gained"]) if timeline else 0
    larva_per_minute = safe_div(total_larva_gained, match_end / 60.0) if match_end > 0 else 0.0
    return {
        "total_larva_gained": total_larva_gained,
        "larva_gained_per_minute": round(larva_per_minute, 2),
    }


def evaluate_hatchery(hatchery: Dict, match_end: float) -> HatcheryReport:
    hatchery_id = hatchery["id"]
    inject_times = normalize_inject_times([float(item) for item in hatchery.get("inject_times", [])])
    larva_samples = hatchery.get("larva_samples", [])

    active_from = max(0.0, float(hatchery.get("inject_ready_from", hatchery.get("active_from", 0.0))))
    active_until = float(hatchery.get("active_until", match_end))
    active_until = max(active_from, min(active_until, match_end))

    expected_window = max(0.0, active_until - active_from)
    expected_inject_count = safe_div(expected_window, INJECT_DELAY_SECONDS)
    queue_metrics = simulate_inject_queue(inject_times, active_from, active_until)
    inject_coverage_percent = queue_metrics["coverage_percent"]
    inject_uptime = queue_metrics["uptime_percent"]
    avg_cycle_gap = queue_metrics["avg_cycle_gap"]
    avg_idle_time = queue_metrics["avg_idle_time"]
    queued_injects_at_end = queue_metrics["queued_injects_at_end"]

    larva_pressure = compute_larva_pressure(larva_samples)

    coverage_score = inject_coverage_percent
    uptime_score = inject_uptime
    cadence_score = clamp(100 - max(0.0, avg_cycle_gap - INJECT_DELAY_SECONDS) * 2.2)
    idle_score = clamp(100 - avg_idle_time * 1.4)
    larva_score = clamp(100 - larva_pressure * 100)

    score = round(
        coverage_score * 0.4
        + uptime_score * 0.25
        + cadence_score * 0.2
        + idle_score * 0.1
        + larva_score * 0.05,
        2,
    )

    return HatcheryReport(
        hatchery_id=hatchery_id,
        inject_count=len(inject_times),
        expected_inject_count=round(expected_inject_count, 2),
        inject_coverage=round(inject_coverage_percent, 2),
        inject_uptime=round(inject_uptime, 2),
        avg_cycle_gap=round(avg_cycle_gap, 2),
        avg_idle_time=round(avg_idle_time, 2),
        queued_injects_at_end=round(queued_injects_at_end, 2),
        larva_pressure=round(larva_pressure * 100, 2),
        score=score,
    )


def describe_score(score: float) -> str:
    if score >= 90:
        return "\u804c\u4e1a\u7ea7"
    if score >= 75:
        return "\u5f88\u5f3a"
    if score >= 60:
        return "\u624e\u5b9e"
    if score >= 45:
        return "\u504f\u5f31"
    return "\u9700\u8981\u52a0\u5f3a"


def build_suggestions(report: HatcheryReport) -> List[str]:
    suggestions: List[str] = []

    if report.inject_coverage < 70:
        suggestions.append("\u55b7\u5375\u8986\u76d6\u7387\u504f\u4f4e\u3002\u628a\u55b7\u5375\u7ed1\u5b9a\u5230\u6bcf\u8f6e\u5b8f\u5faa\u73af\u91cc\uff0c\u80fd\u51cf\u5c11\u6f0f\u55b7\u3002")
    if report.inject_uptime < 70:
        suggestions.append("\u8fd9\u4e2a\u57fa\u5730\u5904\u4e8e\u55b7\u5375\u751f\u6548\u4e2d\u7684\u65f6\u95f4\u504f\u5c11\uff0c\u91cd\u70b9\u662f\u628a\u55b7\u5375\u94fe\u6761\u6301\u7eed\u63a5\u8d77\u6765\u3002")
    if report.avg_cycle_gap > 40:
        suggestions.append("\u55b7\u5375\u8282\u594f\u4e0d\u7a33\u5b9a\u3002\u53ef\u4ee5\u7ec3\u56fa\u5b9a\u955c\u5934\u4f4d\u6216\u56fa\u5b9a\u70ed\u952e\u5faa\u73af\u3002")
    if report.avg_idle_time > 12:
        suggestions.append("\u8fd9\u4e2a\u57fa\u5730\u5728\u4e24\u8f6e\u55b7\u5375\u4e4b\u95f4\u7684\u7a7a\u7a97\u65f6\u95f4\u504f\u957f\u3002")
    if report.queued_injects_at_end > 0.5:
        suggestions.append("\u8fd9\u5c40\u7ed3\u675f\u65f6\u8fd9\u4e2a\u57fa\u5730\u8fd8\u7559\u6709\u9884\u55b7\u5375\u7f13\u5b58\uff0c\u6240\u4ee5\u539f\u59cb\u55b7\u5375\u6b21\u6570\u4f1a\u9ad8\u4e8e\u771f\u5b9e\u6709\u6548\u8986\u76d6\u3002")
    if report.larva_pressure > 25:
        suggestions.append("\u5728\u66f4\u5173\u952e\u7684\u65f6\u95f4\u6bb5\u91cc\u5e7c\u866b\u5806\u79ef\u504f\u9ad8\uff0c\u8bf4\u660e\u55b7\u5375\u8282\u594f\u548c\u82b1\u94b1\u8282\u594f\u53ef\u80fd\u6709\u4e9b\u8131\u8282\u3002")

    if not suggestions:
        suggestions.append("\u8fd9\u4e2a\u57fa\u5730\u7684\u55b7\u5375\u8282\u594f\u6bd4\u8f83\u7a33\u5b9a\uff0c\u4e0b\u4e00\u6b65\u53ef\u4ee5\u7ed3\u5408\u82b1\u94b1\u548c\u51fa\u5175\u53bb\u770b\u6574\u4f53\u8fd0\u8425\u3002")

    return suggestions


def summarize_reports(reports: List[HatcheryReport]) -> Dict[str, float]:
    return {
        "overall_score": round(mean(report.score for report in reports), 2) if reports else 0.0,
        "avg_coverage": round(mean(report.inject_coverage for report in reports), 2) if reports else 0.0,
        "avg_uptime": round(mean(report.inject_uptime for report in reports), 2) if reports else 0.0,
        "avg_cycle_gap": round(mean(report.avg_cycle_gap for report in reports), 2) if reports else 0.0,
        "avg_idle_time": round(mean(report.avg_idle_time for report in reports), 2) if reports else 0.0,
    }


def render_text_report(summary: Dict[str, float], reports: List[HatcheryReport], source_label: str = "") -> str:
    lines = [
        "=== \u661f\u9645\u4e89\u97382\u866b\u65cf\u55b7\u5375\u8bc4\u4f30 ===",
        f"\u6765\u6e90\uff1a{source_label}" if source_label else "\u6765\u6e90\uff1a\u76f4\u63a5\u8f93\u5165",
        f"\u603b\u8bc4\u5206\uff1a{summary['overall_score']}\uff08{describe_score(summary['overall_score'])}\uff09",
        f"\u5e73\u5747\u8986\u76d6\u7387\uff1a{summary['avg_coverage']}%",
        f"\u5e73\u5747\u4e0a\u7ebf\u7387\uff1a{summary['avg_uptime']}%",
        f"\u5e73\u5747\u55b7\u5375\u95f4\u9694\uff1a{summary['avg_cycle_gap']} \u79d2",
        f"\u5e73\u5747\u7a7a\u7a97\u65f6\u95f4\uff1a{summary['avg_idle_time']} \u79d2",
        "",
    ]

    for report in reports:
        lines.extend(
            [
                f"[{report.hatchery_id}]",
                f"\u8bc4\u5206\uff1a{report.score}\uff08{describe_score(report.score)}\uff09",
                f"\u5b9e\u9645/\u7406\u8bba\u55b7\u5375\u8f6e\u6b21\uff1a{report.inject_count}/{report.expected_inject_count}",
                f"\u55b7\u5375\u8986\u76d6\u7387\uff1a{report.inject_coverage}%",
                f"\u55b7\u5375\u4e0a\u7ebf\u7387\uff1a{report.inject_uptime}%",
                f"\u5e73\u5747\u55b7\u5375\u95f4\u9694\uff1a{report.avg_cycle_gap} \u79d2",
                f"\u5e73\u5747\u7a7a\u7a97\u65f6\u95f4\uff1a{report.avg_idle_time} \u79d2",
                f"\u7ed3\u675f\u65f6\u9884\u55b7\u5375\u7f13\u5b58\uff1a{report.queued_injects_at_end}",
                f"\u5e7c\u866b\u5806\u79ef\u538b\u529b\uff1a{report.larva_pressure}%",
                "\u5efa\u8bae\uff1a",
            ]
        )
        lines.extend(f"- {item}" for item in build_suggestions(report))
        lines.append("")

    return "\n".join(lines).strip()


def render_larva_timeline_report(
    larva_summary: Dict[str, float],
    larva_total_timeline: List[Dict[str, float]],
    source_label: str = "",
) -> str:
    lines = [
        "=== \u661f\u9645\u4e89\u97382\u866b\u65cf\u5e7c\u866b\u7d2f\u8ba1\u65f6\u95f4\u7ebf ===",
        f"\u6765\u6e90\uff1a{source_label}" if source_label else "\u6765\u6e90\uff1a\u76f4\u63a5\u8f93\u5165",
        f"\u7d2f\u8ba1\u83b7\u5f97\u5e7c\u866b\u603b\u6570\uff1a{larva_summary.get('total_larva_gained', 0)}",
        f"\u6bcf\u5206\u949f\u83b7\u5f97\u5e7c\u866b\u6570\uff1a{larva_summary.get('larva_gained_per_minute', 0.0)}",
        "",
        "\u65f6\u95f4 | \u7d2f\u8ba1\u83b7\u5f97\u5e7c\u866b | \u5f53\u524d\u5269\u4f59\u5e7c\u866b | \u5f53\u524d\u4eba\u53e3",
    ]

    for sample in larva_total_timeline:
        lines.append(
            f"{sample['time']} | {sample['total_larva_gained']} | "
            f"{sample.get('larva_on_hand', 0)} | {sample.get('food_used', '-')}"
        )

    return "\n".join(lines).strip()


def build_score_output(match_data: Dict, input_path: Path, as_json: bool) -> str:
    summary, reports = evaluate_match(match_data)
    source_label = str(input_path)
    if match_data.get("source") == "replay":
        player = match_data.get("player", {})
        source_label = f"{input_path} | \u9009\u624b={player.get('name', '\u672a\u77e5')} (pid={player.get('pid', '?')})"

    if as_json:
        output = {
            "summary": summary,
            "source": match_data.get("source", "json"),
            "player": match_data.get("player"),
            "hatcheries": [
                {
                    "hatchery_id": report.hatchery_id,
                    "score": report.score,
                    "inject_count": report.inject_count,
                    "expected_inject_count": report.expected_inject_count,
                    "inject_coverage": report.inject_coverage,
                    "inject_uptime": report.inject_uptime,
                    "avg_cycle_gap": report.avg_cycle_gap,
                    "avg_idle_time": report.avg_idle_time,
                    "queued_injects_at_end": report.queued_injects_at_end,
                    "larva_pressure": report.larva_pressure,
                    "suggestions": build_suggestions(report),
                }
                for report in reports
            ],
        }
        return json.dumps(output, ensure_ascii=False, indent=2)

    return render_text_report(summary, reports, source_label=source_label)


def build_larva_timeline_output(match_data: Dict, input_path: Path, as_json: bool) -> str:
    larva_summary = match_data.get("larva_summary", {})
    larva_total_timeline = match_data.get("larva_total_timeline", [])
    if as_json:
        return json.dumps(
            {
                "source": match_data.get("source", "json"),
                "player": match_data.get("player"),
                "larva_summary": larva_summary,
                "larva_total_timeline": larva_total_timeline,
            },
            ensure_ascii=False,
            indent=2,
        )

    source_label = str(input_path)
    if match_data.get("source") == "replay":
        player = match_data.get("player", {})
        source_label = f"{input_path} | \u9009\u624b={player.get('name', '\u672a\u77e5')} (pid={player.get('pid', '?')})"
    return render_larva_timeline_report(larva_summary, larva_total_timeline, source_label=source_label)


def launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title("SC2 \u866b\u65cf\u55b7\u5375\u8bc4\u4f30\u5668")
    root.geometry("980x720")

    input_path_var = tk.StringVar()
    player_name_var = tk.StringVar()
    output_mode_var = tk.StringVar(value="score")
    json_output_var = tk.BooleanVar(value=False)
    status_var = tk.StringVar(value="\u9009\u62e9\u4e00\u4e2a replay \u6216 JSON \u6587\u4ef6\uff0c\u7136\u540e\u70b9\u51fb\u5f00\u59cb\u5206\u6790\u3002")

    root.columnconfigure(1, weight=1)
    root.rowconfigure(4, weight=1)

    ttk.Label(root, text="\u8f93\u5165\u6587\u4ef6").grid(row=0, column=0, padx=10, pady=10, sticky="w")
    input_entry = ttk.Entry(root, textvariable=input_path_var)
    input_entry.grid(row=0, column=1, padx=10, pady=10, sticky="ew")

    def refresh_player_options() -> None:
        raw_path = input_path_var.get().strip()
        player_combo["values"] = []
        player_name_var.set("")
        if not raw_path:
            status_var.set("\u9009\u62e9\u4e00\u4e2a replay \u6216 JSON \u6587\u4ef6\uff0c\u7136\u540e\u70b9\u51fb\u5f00\u59cb\u5206\u6790\u3002")
            return

        path = Path(raw_path)
        if not path.exists():
            status_var.set("\u5f53\u524d\u8def\u5f84\u4e0d\u5b58\u5728\u3002")
            return

        if path.suffix.lower() != ".sc2replay":
            status_var.set("\u5f53\u524d\u8f93\u5165\u4e0d\u662f replay \u6587\u4ef6\uff0c\u4e0d\u9700\u8981\u9009\u62e9\u73a9\u5bb6\u3002")
            return

        try:
            player_names = [str(player.name) for player in replay_zerg_players(path)]
        except Exception as exc:
            status_var.set(f"\u8bfb\u53d6\u73a9\u5bb6\u5217\u8868\u5931\u8d25\uff1a{exc}")
            return

        player_combo["values"] = player_names
        if len(player_names) == 1:
            player_name_var.set(player_names[0])
            status_var.set(f"\u5df2\u81ea\u52a8\u9009\u4e2d\u866b\u65cf\u73a9\u5bb6\uff1a{player_names[0]}")
        elif player_names:
            status_var.set("\u8fd9\u4e2a replay \u91cc\u6709\u591a\u4e2a\u866b\u65cf\u73a9\u5bb6\uff0c\u8bf7\u624b\u52a8\u9009\u62e9\u3002")
        else:
            status_var.set("\u8fd9\u4e2a replay \u91cc\u6ca1\u6709\u68c0\u6d4b\u5230\u866b\u65cf\u73a9\u5bb6\u3002")

    def choose_input_file() -> None:
        file_path = filedialog.askopenfilename(
            title="\u9009\u62e9 replay \u6216 JSON \u6587\u4ef6",
            filetypes=[
                ("Replay and JSON", "*.SC2Replay *.sc2replay *.json"),
                ("SC2 Replay", "*.SC2Replay *.sc2replay"),
                ("JSON", "*.json"),
                ("All files", "*.*"),
            ],
        )
        if file_path:
            input_path_var.set(file_path)
            refresh_player_options()

    ttk.Button(root, text="\u9009\u62e9\u6587\u4ef6", command=choose_input_file).grid(row=0, column=2, padx=10, pady=10)

    ttk.Label(root, text="\u866b\u65cf\u73a9\u5bb6").grid(row=1, column=0, padx=10, pady=5, sticky="w")
    player_combo = ttk.Combobox(root, textvariable=player_name_var, state="readonly")
    player_combo.grid(row=1, column=1, padx=10, pady=5, sticky="ew")
    ttk.Button(root, text="\u5237\u65b0\u73a9\u5bb6", command=refresh_player_options).grid(row=1, column=2, padx=10, pady=5)

    ttk.Label(root, text="\u8f93\u51fa\u7c7b\u578b").grid(row=2, column=0, padx=10, pady=5, sticky="w")
    mode_frame = ttk.Frame(root)
    mode_frame.grid(row=2, column=1, padx=10, pady=5, sticky="w")
    ttk.Radiobutton(mode_frame, text="\u55b7\u5375\u8bc4\u5206", variable=output_mode_var, value="score").grid(row=0, column=0, padx=5)
    ttk.Radiobutton(mode_frame, text="\u5e7c\u866b\u65f6\u95f4\u7ebf", variable=output_mode_var, value="larva").grid(row=0, column=1, padx=5)
    ttk.Radiobutton(mode_frame, text="\u539f\u59cb\u89e3\u6790\u7ed3\u679c", variable=output_mode_var, value="parsed").grid(row=0, column=2, padx=5)
    ttk.Checkbutton(root, text="JSON \u8f93\u51fa", variable=json_output_var).grid(row=2, column=2, padx=10, pady=5, sticky="w")

    output_box = ScrolledText(root, wrap="word")
    output_box.grid(row=4, column=0, columnspan=3, padx=10, pady=10, sticky="nsew")

    ttk.Label(root, textvariable=status_var).grid(row=5, column=0, columnspan=3, padx=10, pady=5, sticky="w")

    def run_analysis() -> None:
        raw_path = input_path_var.get().strip()
        if not raw_path:
            messagebox.showwarning("\u7f3a\u5c11\u8f93\u5165", "\u8bf7\u5148\u9009\u62e9\u4e00\u4e2a replay \u6216 JSON \u6587\u4ef6\u3002")
            return

        path = Path(raw_path)
        if not path.exists():
            messagebox.showerror("\u8def\u5f84\u4e0d\u5b58\u5728", f"\u627e\u4e0d\u5230\u6587\u4ef6\uff1a{path}")
            return

        selected_player = player_name_var.get().strip() or None
        try:
            match_data = load_input_data(path, selected_player, None)
            output_mode = output_mode_var.get()
            as_json = bool(json_output_var.get())

            if output_mode == "parsed":
                output_text = json.dumps(match_data, ensure_ascii=False, indent=2)
            elif output_mode == "larva":
                output_text = build_larva_timeline_output(match_data, path, as_json)
            else:
                output_text = build_score_output(match_data, path, as_json)
        except Exception as exc:
            messagebox.showerror("\u8fd0\u884c\u5931\u8d25", str(exc))
            status_var.set(f"\u8fd0\u884c\u5931\u8d25\uff1a{exc}")
            return

        output_box.delete("1.0", tk.END)
        output_box.insert(tk.END, output_text)
        status_var.set("\u5206\u6790\u5b8c\u6210\u3002")

    ttk.Button(root, text="\u5f00\u59cb\u5206\u6790", command=run_analysis).grid(row=3, column=0, columnspan=3, padx=10, pady=10)

    input_entry.focus_set()
    root.mainloop()


def render_batch_report(results: List[Dict], errors: List[str]) -> str:
    overall_scores = [item["summary"]["overall_score"] for item in results]
    lines = [
        "=== \u661f\u9645\u4e89\u97382\u866b\u65cf\u6279\u91cf\u55b7\u5375\u8bc4\u4f30 ===",
        f"\u6761\u76ee\u6570\uff1a{len(results)}",
        f"\u5e73\u5747\u603b\u8bc4\u5206\uff1a{round(mean(overall_scores), 2) if overall_scores else 0.0}",
        "",
    ]

    for item in results:
        player = item["match_data"].get("player") or {}
        player_label = player.get("name", "\u672a\u77e5")
        lines.extend(
            [
                f"{item['path']}",
                f"\u9009\u624b\uff1a{player_label}",
                f"\u603b\u8bc4\u5206\uff1a{item['summary']['overall_score']}\uff08{describe_score(item['summary']['overall_score'])}\uff09",
                f"\u5e73\u5747\u8986\u76d6\u7387\uff1a{item['summary']['avg_coverage']}%",
                f"\u5e73\u5747\u4e0a\u7ebf\u7387\uff1a{item['summary']['avg_uptime']}%",
                f"\u5e73\u5747\u55b7\u5375\u95f4\u9694\uff1a{item['summary']['avg_cycle_gap']} \u79d2",
                "",
            ]
        )

    if errors:
        lines.extend(["\u9519\u8bef\uff1a", *[f"- {item}" for item in errors]])

    return "\n".join(lines).strip()


def evaluate_match(match_data: Dict) -> Tuple[Dict[str, float], List[HatcheryReport]]:
    match_end = float(match_data["match_end"])
    reports = [evaluate_hatchery(hatchery, match_end) for hatchery in match_data["hatcheries"]]
    return summarize_reports(reports), reports


def choose_target_player(replay, player_name: Optional[str], player_id: Optional[int]):
    zerg_players = [player for player in replay.players if getattr(player, "play_race", "") == "Zerg"]
    if player_id is not None:
        for player in zerg_players:
            if player.pid == player_id:
                return player
        raise ValueError(f"\u8fd9\u4e2a\u5f55\u50cf\u91cc\u6ca1\u6709\u627e\u5230 pid={player_id} \u7684\u866b\u65cf\u73a9\u5bb6\u3002")

    if player_name:
        lowered = player_name.casefold()
        for player in zerg_players:
            if str(player.name).casefold() == lowered:
                return player
        raise ValueError(f"\u8fd9\u4e2a\u5f55\u50cf\u91cc\u6ca1\u6709\u627e\u5230\u540d\u4e3a\u201c{player_name}\u201d\u7684\u866b\u65cf\u73a9\u5bb6\u3002")

    if len(zerg_players) == 1:
        return zerg_players[0]

    available = ", ".join(f"{player.pid}:{player.name}" for player in zerg_players) or "\u65e0"
    raise ValueError(
        "\u8fd9\u4e2a\u5f55\u50cf\u91cc\u5b58\u5728\u591a\u4e2a\u6216\u96f6\u4e2a\u866b\u65cf\u73a9\u5bb6\u3002\u8bf7\u4f7f\u7528 --player-id \u6216 --player-name \u6307\u5b9a\u76ee\u6807\u73a9\u5bb6\u3002"
        f" \u53ef\u9009\u866b\u65cf\u73a9\u5bb6\uff1a{available}"
    )


def ensure_hatchery(
    hatcheries: Dict[int, HatcheryState],
    unit_id: int,
    label: str,
    location: Tuple[float, float],
    started_at: float,
    completed: bool,
) -> HatcheryState:
    state = hatcheries.get(unit_id)
    if state is None:
        state = HatcheryState(
            unit_id=unit_id,
            label=label,
            started_at=started_at,
            location=location,
            active_from=started_at if completed else None,
            completed=completed,
        )
        hatcheries[unit_id] = state
        return state

    state.location = location
    state.started_at = min(state.started_at, started_at)
    if completed:
        state.active_from = started_at if state.active_from is None else min(state.active_from, started_at)
    state.completed = state.completed or completed
    return state


def active_hatcheries(hatcheries: Dict[int, HatcheryState], second: float) -> List[HatcheryState]:
    return [
        hatchery
        for hatchery in hatcheries.values()
        if hatchery.completed
        and hatchery.active_from is not None
        and hatchery.active_from <= second
        and (hatchery.active_until is None or second < hatchery.active_until)
    ]


def nearest_hatchery(
    hatcheries: Dict[int, HatcheryState],
    location: Tuple[float, float],
    second: float,
) -> Optional[HatcheryState]:
    candidates = active_hatcheries(hatcheries, second)
    if not candidates:
        candidates = [hatchery for hatchery in hatcheries.values() if hatchery.completed]
    if not candidates:
        return None
    return min(candidates, key=lambda hatchery: distance_sq(hatchery.location, location))


def append_larva_samples(
    hatcheries: Dict[int, HatcheryState],
    larva_units: Dict[int, Dict[str, Tuple[float, float]]],
    second: float,
    food_used: Optional[float],
) -> None:
    candidates = active_hatcheries(hatcheries, second)
    if not candidates:
        return

    counts = {hatchery.unit_id: 0 for hatchery in candidates}
    for larva in larva_units.values():
        hatchery = min(candidates, key=lambda item: distance_sq(item.location, larva["location"]))
        counts[hatchery.unit_id] += 1

    sample_time = round(second, 2)
    for hatchery in candidates:
        sample = {"time": sample_time, "larva": counts[hatchery.unit_id]}
        if food_used is not None:
            sample["food_used"] = round(food_used, 2)
        if hatchery.larva_samples and hatchery.larva_samples[-1]["time"] == sample_time:
            hatchery.larva_samples[-1] = sample
        else:
            hatchery.larva_samples.append(sample)


def finalize_hatchery_labels(hatcheries: Dict[int, HatcheryState], match_end: float) -> List[HatcheryState]:
    eligible = [
        hatchery
        for hatchery in hatcheries.values()
        if hatchery.completed
        and hatchery.active_from is not None
        and (hatchery.active_until is None or hatchery.active_until > hatchery.active_from)
        and match_end > hatchery.active_from
    ]
    ordered = sorted(eligible, key=lambda item: (item.active_from or 0.0, item.location[0], item.location[1]))
    for index, hatchery in enumerate(ordered, start=1):
        hatchery.label = f"\u57fa\u5730-{index}"
        hatchery.inject_times = normalize_inject_times(hatchery.inject_times)
    return ordered


def parse_replay(path: Path, player_name: Optional[str], player_id: Optional[int]) -> Dict:
    replay = sc2reader.load_replay(str(path), load_level=4, load_map=False)
    target_player = choose_target_player(replay, player_name, player_id)
    time_scale = replay_time_scale(replay)
    event_times = [event_second(event, time_scale) for event in replay.tracker_events] + [event_second(event, time_scale) for event in replay.game_events]
    match_end = max(event_times, default=float(replay.game_length.seconds))

    hatcheries: Dict[int, HatcheryState] = {}
    larva_units: Dict[int, Dict[str, Tuple[float, float]]] = {}
    earliest_queen_ready: Optional[float] = None
    larva_birth_times: List[float] = []
    player_stat_samples: List[Dict[str, float]] = []

    for event in replay.tracker_events:
        second = event_second(event, time_scale)

        if isinstance(event, (UnitInitEvent, UnitBornEvent)):
            owner_pid = getattr(event, "control_pid", None) or getattr(event, "upkeep_pid", None)
            if owner_pid == target_player.pid and event.unit_type_name in HATCHERY_TYPES:
                ensure_hatchery(
                    hatcheries,
                    event.unit_id,
                    event.unit_type_name,
                    (float(event.x), float(event.y)),
                    second,
                    isinstance(event, UnitBornEvent),
                )
            if owner_pid == target_player.pid and event.unit_type_name in QUEEN_TYPES and isinstance(event, UnitBornEvent):
                earliest_queen_ready = second if earliest_queen_ready is None else min(earliest_queen_ready, second)
            if owner_pid == target_player.pid and event.unit_type_name == LARVA_TYPE:
                larva_birth_times.append(second)
                larva_units[event.unit_id] = {"location": (float(event.x), float(event.y))}

        elif isinstance(event, UnitDoneEvent):
            state = hatcheries.get(event.unit_id)
            if state is not None:
                state.completed = True
                state.active_from = second

        elif isinstance(event, UnitTypeChangeEvent):
            state = hatcheries.get(event.unit_id)
            if state is not None and event.unit_type_name in HATCHERY_TYPES:
                state.label = event.unit_type_name
            if event.unit_id in larva_units and event.unit_type_name != LARVA_TYPE:
                del larva_units[event.unit_id]

        elif isinstance(event, UnitDiedEvent):
            state = hatcheries.get(event.unit_id)
            if state is not None:
                state.active_until = second
            larva_units.pop(event.unit_id, None)

        elif isinstance(event, PlayerStatsEvent) and event.pid == target_player.pid:
            append_larva_samples(hatcheries, larva_units, second, getattr(event, "food_used", None))
            player_stat_sample = {
                "time": round(second, 2),
                "larva_on_hand": len(larva_units),
            }
            if getattr(event, "food_used", None) is not None:
                player_stat_sample["food_used"] = round(float(event.food_used), 2)
            player_stat_samples.append(player_stat_sample)

    for event in replay.game_events:
        event_player = getattr(event, "player", None)
        if event_player is None:
            continue
        if str(getattr(event_player, "name", "")) != str(target_player.name):
            continue
        if not isinstance(event, TargetUnitCommandEvent):
            continue
        if getattr(event, "ability_name", "") not in INJECT_ABILITY_NAMES:
            continue

        second = event_second(event, time_scale)
        target_unit_id = getattr(event, "target_unit_id", None)
        hatchery = hatcheries.get(target_unit_id) if target_unit_id else None

        if hatchery is None:
            hatchery = nearest_hatchery(hatcheries, (float(event.x), float(event.y)), second)

        if hatchery is not None:
            hatchery.inject_times.append(second)

    ordered_hatcheries = finalize_hatchery_labels(hatcheries, match_end)
    for hatchery in ordered_hatcheries:
        hatchery.inject_ready_from = max(hatchery.active_from or 0.0, earliest_queen_ready or 0.0)

    larva_total_timeline = build_larva_total_timeline(larva_birth_times, player_stat_samples)
    larva_summary = summarize_larva_total_timeline(larva_total_timeline, match_end)
    return {
        "match_end": match_end,
        "source": "replay",
        "replay_path": str(path),
        "player": {"pid": target_player.pid, "name": str(target_player.name)},
        "larva_summary": larva_summary,
        "larva_total_timeline": larva_total_timeline,
        "hatcheries": [
            {
                "id": hatchery.label,
                "active_from": round(hatchery.active_from or 0.0, 2),
                "inject_ready_from": round(hatchery.inject_ready_from or hatchery.active_from or 0.0, 2),
                "active_until": round(hatchery.active_until or match_end, 2),
                "inject_times": hatchery.inject_times,
                "larva_samples": hatchery.larva_samples,
            }
            for hatchery in ordered_hatcheries
        ],
    }


def load_input_data(path: Path, player_name: Optional[str], player_id: Optional[int]) -> Dict:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return load_match(path)
    if suffix == ".sc2replay":
        return parse_replay(path, player_name, player_id)
    raise ValueError("\u4e0d\u652f\u6301\u7684\u8f93\u5165\u7c7b\u578b\uff0c\u8bf7\u4f7f\u7528 .json \u6216 .SC2Replay \u6587\u4ef6\u3002")


def collect_replay_files(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(path.rglob("*.SC2Replay"))
    raise ValueError(f"\u8f93\u5165\u8def\u5f84\u4e0d\u5b58\u5728\uff1a{path}")


def replay_zerg_players(path: Path) -> List:
    replay = sc2reader.load_replay(str(path), load_level=2, load_map=False)
    return [player for player in replay.players if getattr(player, "play_race", "") == "Zerg"]


def main() -> None:
    if len(sys.argv) == 1:
        launch_gui()
        return

    parser = argparse.ArgumentParser(description="\u4ece JSON\u3001\u5355\u4e2a\u5f55\u50cf\u6216\u5f55\u50cf\u76ee\u5f55\u4e2d\u8bc4\u4f30\u866b\u65cf\u55b7\u5375\u8d28\u91cf\u3002")
    parser.add_argument("input", type=Path, nargs="?", help="JSON \u6587\u4ef6\u3001.SC2Replay \u6587\u4ef6\u6216\u5f55\u50cf\u76ee\u5f55\u8def\u5f84")
    parser.add_argument("--json", action="store_true", help="\u8f93\u51fa\u673a\u5668\u53ef\u8bfb\u7684 JSON \u7ed3\u679c")
    parser.add_argument("--player-name", help="\u6307\u5b9a\u5f55\u50cf\u4e2d\u7684\u866b\u65cf\u73a9\u5bb6\u540d\u79f0")
    parser.add_argument("--player-id", type=int, help="\u6307\u5b9a\u5f55\u50cf\u4e2d\u7684\u866b\u65cf\u73a9\u5bb6 pid")
    parser.add_argument("--gui", action="store_true", help="\u542f\u52a8\u684c\u9762\u56fe\u5f62\u754c\u9762")
    parser.add_argument(
        "--larva-timeline",
        action="store_true",
        help="\u8f93\u51fa\u8be5\u73a9\u5bb6\u672c\u5c40\u7d2f\u8ba1\u83b7\u5f97\u5e7c\u866b\u6570\u91cf\u7684\u65f6\u95f4\u7ebf",
    )
    parser.add_argument(
        "--dump-parsed",
        action="store_true",
        help="\u53ea\u8f93\u51fa\u89e3\u6790\u540e\u7684\u5f55\u50cf\u7ed3\u6784\uff0c\u4e0d\u8fdb\u884c\u8bc4\u5206",
    )
    args = parser.parse_args()

    if args.gui:
        launch_gui()
        return

    if args.input is None:
        parser.error("\u9664\u975e\u4f7f\u7528 --gui\uff0c\u5426\u5219\u5fc5\u987b\u63d0\u4f9b input \u53c2\u6570")

    if args.input.is_dir():
        replay_files = collect_replay_files(args.input)
        if not replay_files:
            raise ValueError(f"\u5728\u8fd9\u4e2a\u76ee\u5f55\u4e0b\u6ca1\u6709\u627e\u5230 .SC2Replay \u6587\u4ef6\uff1a{args.input}")

        results: List[Dict] = []
        errors: List[str] = []
        for replay_path in replay_files:
            try:
                player_names: List[Optional[str]] = [args.player_name]
                if args.player_name is None and args.player_id is None:
                    zerg_players = replay_zerg_players(replay_path)
                    if len(zerg_players) > 1:
                        player_names = [str(player.name) for player in zerg_players]

                for selected_player_name in player_names:
                    match_data = load_input_data(replay_path, selected_player_name, args.player_id)
                    entry_path = str(replay_path)
                    if selected_player_name is not None and len(player_names) > 1:
                        entry_path = f"{replay_path} | \u9009\u624b={selected_player_name}"

                    if args.dump_parsed:
                        results.append({"path": entry_path, "match_data": match_data})
                        continue

                    summary, reports = evaluate_match(match_data)
                    results.append(
                        {
                            "path": entry_path,
                            "match_data": match_data,
                            "summary": summary,
                            "reports": reports,
                        }
                    )
            except Exception as exc:
                errors.append(f"{replay_path}: {exc}")

        if args.dump_parsed:
            print(
                json.dumps(
                    {
                        "results": [{"path": item["path"], "match_data": item["match_data"]} for item in results],
                        "errors": errors,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        if args.json:
            print(
                json.dumps(
                    {
                        "results": [
                            {
                                "path": item["path"],
                                "summary": item["summary"],
                                "player": item["match_data"].get("player"),
                                "source": item["match_data"].get("source", "replay"),
                            }
                            for item in results
                        ],
                        "errors": errors,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        print(render_batch_report(results, errors))
        return

    match_data = load_input_data(args.input, args.player_name, args.player_id)
    if args.dump_parsed:
        print(json.dumps(match_data, ensure_ascii=False, indent=2))
        return

    if args.larva_timeline:
        larva_summary = match_data.get("larva_summary", {})
        larva_total_timeline = match_data.get("larva_total_timeline", [])
        if args.json:
            print(
                json.dumps(
                    {
                        "source": match_data.get("source", "json"),
                        "player": match_data.get("player"),
                        "larva_summary": larva_summary,
                        "larva_total_timeline": larva_total_timeline,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return

        source_label = str(args.input)
        if match_data.get("source") == "replay":
            player = match_data.get("player", {})
            source_label = f"{args.input} | \u9009\u624b={player.get('name', '\u672a\u77e5')} (pid={player.get('pid', '?')})"
        print(render_larva_timeline_report(larva_summary, larva_total_timeline, source_label=source_label))
        return

    summary, reports = evaluate_match(match_data)
    source_label = str(args.input)
    if match_data.get("source") == "replay":
        player = match_data.get("player", {})
        source_label = f"{args.input} | \u9009\u624b={player.get('name', '\u672a\u77e5')} (pid={player.get('pid', '?')})"

    if args.json:
        output = {
            "summary": summary,
            "source": match_data.get("source", "json"),
            "player": match_data.get("player"),
            "hatcheries": [
                {
                    "hatchery_id": report.hatchery_id,
                    "score": report.score,
                    "inject_count": report.inject_count,
                    "expected_inject_count": report.expected_inject_count,
                    "inject_coverage": report.inject_coverage,
                    "inject_uptime": report.inject_uptime,
                    "avg_cycle_gap": report.avg_cycle_gap,
                    "avg_idle_time": report.avg_idle_time,
                    "queued_injects_at_end": report.queued_injects_at_end,
                    "larva_pressure": report.larva_pressure,
                    "suggestions": build_suggestions(report),
                }
                for report in reports
            ],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    print(render_text_report(summary, reports, source_label=source_label))


if __name__ == "__main__":
    main()
