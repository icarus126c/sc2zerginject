# SC2 Zerg Inject Evaluator

这是一个用于评估《星际争霸2》虫族喷卵质量的小工具，支持回放解析、喷卵评分、幼虫累计时间线和 Windows GUI。

中文说明请看：

- [README.zh-CN.md](C:/Users/qq563/Documents/codex1/README.zh-CN.md)

Windows GUI 打包版请看：

- [release/sc2_inject_evaluator_gui_windows.zip](C:/Users/qq563/Documents/codex1/release/sc2_inject_evaluator_gui_windows.zip)

Quick summary:

This is a small tool for evaluating StarCraft II Zerg inject quality from JSON input, replay files, or replay folders. It also includes a Windows GUI build for double-click use.

It now supports two input modes:

- Direct JSON input
- Direct `.SC2Replay` parsing through `sc2reader`
- Recursive directory parsing for batches of replays
- Desktop GUI mode for double-click use on Windows

## GUI mode

If you launch the script with no arguments, it opens a desktop window instead of printing a command-line usage error.

You can also launch the GUI explicitly:

```bash
python sc2_inject_evaluator.py --gui
```

Inside the GUI you can:

- Choose a replay or JSON file
- Select the Zerg player when a replay contains multiple Zerg players
- Switch between score output, larva timeline, and parsed replay data
- Show the result as text or JSON

## Scoring model

The evaluator scores inject performance across five dimensions:

- Inject coverage: actual inject count versus theoretical inject opportunities
- Inject uptime: how much of the hatchery's active lifetime is covered by an inject cycle
- Cadence stability: whether inject gaps stay close to 29 seconds
- Hatchery idle time: how long a hatchery sits without an active inject cycle
- Larva pressure: a weak late-game-decayed penalty based on larva stock, game time, and supply

The final score is a weighted score out of 100.

## JSON input

Example:

```json
{
  "match_end": 480,
  "hatcheries": [
    {
      "id": "Main",
      "active_from": 0,
      "inject_times": [32, 64, 96],
      "larva_samples": [
        { "time": 60, "larva": 3 },
        { "time": 120, "larva": 6 }
      ]
    }
  ]
}
```

Run it like this:

```bash
python sc2_inject_evaluator.py sample_match.json
```

## Replay input

Run it directly on a replay:

```bash
python sc2_inject_evaluator.py path/to/game.SC2Replay
```

If the replay contains more than one Zerg player, specify one:

```bash
python sc2_inject_evaluator.py path/to/game.SC2Replay --player-name Rogue
python sc2_inject_evaluator.py path/to/game.SC2Replay --player-id 1
```

If you want to inspect the parsed replay data before scoring:

```bash
python sc2_inject_evaluator.py path/to/game.SC2Replay --dump-parsed
```

To score a whole replay folder recursively:

```bash
python sc2_inject_evaluator.py path/to/replay_folder
```

## Output JSON

For machine-readable results:

```bash
python sc2_inject_evaluator.py sample_match.json --json
```

To inspect how many larva a replay player has cumulatively gained over time:

```bash
python sc2_inject_evaluator.py path/to/game.SC2Replay --larva-timeline
python sc2_inject_evaluator.py path/to/game.SC2Replay --larva-timeline --json
```

## Replay parsing notes

Replay parsing currently does the following:

- Finds the target Zerg player
- Tracks hatchery/lair/hive lifecycle from tracker events
- Detects `SpawnLarva` target-unit commands as inject attempts
- Samples live larva counts at each `PlayerStatsEvent`
- Tracks cumulative larva births over time for the selected player

This version is hatchery-centric rather than queen-centric, so it evaluates how well each base maintains its inject chain. It is still an approximation:

- Command events show issued commands, not guaranteed successful injects in every edge case
- Larva samples are assigned to the nearest active hatchery by position
- Unfinished, cancelled, or pre-completion-destroyed hatcheries are excluded from active hatchery scoring
- After 10 minutes, larva pressure is automatically down-weighted based on time and supply because high-level late-game inject demand is less rigid

## Next ideas

- Distinguish macro queens from creep or defense queens
- Split scoring by game phase
- Add spending/resource context to reduce false positives
- Export parsed replay data for later training analysis
