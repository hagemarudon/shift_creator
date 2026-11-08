"""画面で入力した条件を config.local.toml に保存する（次回からの初期値になる。Gitで管理しない）。"""
from __future__ import annotations

from pathlib import Path

from .model import Settings


def _hours(minutes: int) -> str:
    return f"{minutes / 60:g}"


def save_settings(settings: Settings, path: str | Path) -> None:
    lines = [
        "# 画面の「この条件を既定にする」で保存した条件（config.toml より優先）",
        f"slot_minutes = {settings.slot_minutes}",
        "",
        "[shift]",
        f"min_hours = {_hours(settings.min_shift_minutes)}",
        f"normal_max_hours = {_hours(settings.normal_max_minutes)}",
        f"max_hours = {_hours(settings.max_shift_minutes)}",
        "",
    ]
    for r in settings.rules:
        lines += ["[[rules]]", f'days = "{r.days}"', f'start = "{r.start}"', f'end = "{r.end}"',
                  f"people = {r.people}", ""]
    lines += ["[leader]", f"required = {'true' if settings.leader_required else 'false'}", "",
              "[solver]", f"time_limit_seconds = {settings.time_limit_seconds:g}", "", "[weights]"]
    lines += [f"{k} = {v}" for k, v in settings.weights.items()]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
