"""Grade one of the owner's hand-run NCAAF sheets against ESPN finals (v2 step A2).

    docker compose run --rm dev python /workspace/scripts/grade_sheet.py \
        /workspace/services/tests/fixtures/ncaaf_predictions_20261121.csv 2026-11-21

Each row is found among that date's ESPN games by its two team names. A row that matches no
game, or more than one, stops the script: a guessed game would grade the wrong result. The
names here are the sheet's short forms, so this lookup is for offline grading only and never
used for booking (A3 brings the real matcher). Writes `docs/reports/v2-ncaaf-sheet-{date}.md`.
"""

import csv
import os
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from hoopsedge.common.config_v2 import load_defaults
from hoopsedge.ingest.scores import read_teams
from hoopsedge.l5.data import season_inputs
from hoopsedge.l5.model import fixed_tiers, predict_game, team_input

DATA = Path(os.environ.get("HOOPSEDGE_DATA_DIR", "/workspace/data"))
REPORTS = Path(os.environ.get("HOOPSEDGE_REPORT_DIR", "/workspace/docs/reports"))
SPORT = "NCAAF"


def simple(name: str) -> str:
    name = re.sub(r"[^a-z0-9 &]", "", name.lower().replace("é", "e"))
    return re.sub(r"\bst$", "state", re.sub(r"\s+", " ", name).strip())


@dataclass
class Graded:
    row: dict[str, str]
    game: dict[str, Any]
    final_total: int
    ours: str
    model_total: float
    flags: tuple[str, ...]


def find_game(day_games: list[dict[str, Any]], teams: dict[str, Any], away: str, home: str) -> Any:
    def names(team_id: str) -> set[str]:
        team = teams[team_id]
        return {
            simple(team[key])
            for key in ("location", "short_name", "display_name", "abbreviation")
            if team[key]
        }

    found = [
        g
        for g in day_games
        if simple(away) in names(g["away_id"]) and simple(home) in names(g["home_id"])
    ]
    if len(found) != 1:
        raise SystemExit(f"{away} @ {home}: {len(found)} ESPN games match. Fix the sheet name.")
    return found[0]


def grade(sheet: Path, day: date) -> list[Graded]:
    config = load_defaults()
    sport = config.sports[SPORT]
    season = day.year if day.month >= 7 else day.year - 1
    frame, finals, levels = season_inputs(DATA, SPORT, season)
    teams = {row["team_id"]: row for row in read_teams(DATA, SPORT, season).to_dicts()}
    day_games = frame.filter(pl.col("slate_date_et") == day).to_dicts()
    graded: list[Graded] = []
    for row in csv.DictReader(sheet.open(encoding="utf-8")):
        game = find_game(day_games, teams, row["Away"], row["Home"])
        if not game["completed"]:
            raise SystemExit(f"{row['Away']} @ {row['Home']} has no final yet")
        inputs = {
            side: team_input(
                game[f"{side}_id"],
                finals,
                season=season,
                before=game["kickoff_utc"],
                window=sport.l5_window,
                levels=levels,
            )
            for side in ("home", "away")
        }
        sheet_values = (row["Away L5 PF/PA"], row["Home L5 PF/PA"])
        ours_values = tuple(f"{inputs[s].pf:.1f}/{inputs[s].pa:.1f}" for s in ("away", "home"))
        if ours_values != sheet_values:
            raise SystemExit(f"{row['Away']} @ {row['Home']}: L5 {ours_values} vs {sheet_values}")
        prediction = predict_game(
            inputs["home"],
            inputs["away"],
            config=sport,
            bands=config.outlook,
            tiers=fixed_tiers(sport),
            main_line=float(row["Vegas Total"]),
            levels=levels,
        )
        if prediction.lean != row["Lean"]:
            raise SystemExit(f"{row['Away']} @ {row['Home']}: lean {prediction.lean} vs sheet")
        graded.append(
            Graded(
                row=row,
                game=game,
                final_total=game["home_pts"] + game["away_pts"],
                ours=prediction.lean or "PASS",
                model_total=prediction.model_total,
                flags=prediction.flags,
            )
        )
    return graded


def outcome(side: str, final: int, line: float) -> str:
    if final == line:
        return "push"
    return "won" if (final > line) == (side in ("OVER", "Over")) else "lost"


def report(graded: list[Graded], day: date) -> str:
    legs = [g for g in graded if g.ours in ("OVER", "UNDER")]
    results = [outcome(g.ours, g.final_total, float(g.row["Vegas Total"])) for g in legs]
    won, lost = results.count("won"), results.count("lost")
    side_all = [
        outcome("OVER" if g.model_total > float(g.row["Vegas Total"]) else "UNDER", g.final_total,
                float(g.row["Vegas Total"]))
        for g in graded
        if g.model_total != float(g.row["Vegas Total"])
    ]  # fmt: skip
    model_mae = sum(abs(g.model_total - g.final_total) for g in graded) / len(graded)
    line_mae = sum(abs(float(g.row["Vegas Total"]) - g.final_total) for g in graded) / len(graded)
    sum_mae = sum(abs(float(g.row["Sum of Avgs"]) - g.final_total) for g in graded) / len(graded)
    flagged = [g for g in graded if "cross_level_game" in g.flags]
    flagged_names = ", ".join(f"{g.row['Away']} @ {g.row['Home']}" for g in flagged)
    lines = [
        f"# NCAAF sheet of {day:%Y-%m-%d}, graded",
        "",
        f"Generated by `scripts/grade_sheet.py`. {len(graded)} games from the owner's hand-run "
        "sheet, matched to ESPN finals. For every row, the L5 PF/PA recomputed from ingested "
        "ESPN results (each team's window cut at that game's kickoff) equals the sheet, and the "
        "L5 model's lean equals the sheet's. Lines are the sheet's \"Vegas Total\" (the SportyBet "
        "main line then); odds were not recorded, so there is no ROI.",
        "",
        "## Lean legs (thresholds 4.0 over / 5.0 under)",
        "",
        f"**{won} won, {lost} lost, {results.count('push')} pushed of {len(legs)}** "
        f"({100 * won / max(won + lost, 1):.0f}%). At main-line odds near 1.85 a leg needs about "
        "54% to break even. One slate is far too small to judge the model; this is a first "
        "data point, not evidence.",
        "",
        "| Game | Lean | Line | Model | Final | Result | Flags |",
        "|---|---|---|---|---|---|---|",
    ]
    for g, result in zip(legs, results, strict=True):
        lines.append(
            f"| {g.row['Away']} @ {g.row['Home']} | {g.ours} | {g.row['Vegas Total']} "
            f"| {g.model_total:.1f} | {g.final_total} | {result} | {', '.join(g.flags) or ''} |"
        )
    lines += [
        "",
        "## Every game",
        "",
        f"- Model side of the line, ignoring thresholds: {side_all.count('won')} right, "
        f"{side_all.count('lost')} wrong of {len(side_all)}.",
        f"- Mean absolute error of the total: model {model_mae:.1f}, sum of averages "
        f"{sum_mae:.1f}, bookmaker line {line_mae:.1f} points.",
        f"- Cross-level games (FBS vs FCS): {len(flagged)}"
        + (f" ({flagged_names})" if flagged else "")
        + ".",
        "",
        "| Game | Line | Model | Final | Sheet lean |",
        "|---|---|---|---|---|",
    ]
    for g in sorted(graded, key=lambda g: g.game["kickoff_utc"]):
        lines.append(
            f"| {g.row['Away']} @ {g.row['Home']} | {g.row['Vegas Total']} | {g.model_total:.1f} "
            f"| {g.final_total} | {g.ours} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    sheet, day = Path(sys.argv[1]), date.fromisoformat(sys.argv[2])
    graded = grade(sheet, day)
    text = report(graded, day)
    path = REPORTS / f"v2-ncaaf-sheet-{day:%Y%m%d}.md"
    path.write_text(text, "utf-8", newline="\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
