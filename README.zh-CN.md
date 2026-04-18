# 星际争霸2虫族喷卵评估器

这是一个用于评估《星际争霸2》虫族玩家喷卵质量的小工具。

它支持三种输入方式：

- 直接读取 `JSON`
- 直接解析 `.SC2Replay` 回放
- 递归分析整个回放目录

此外，项目也提供了 Windows 下可双击运行的 GUI 版本。

## 功能概览

当前工具可以输出两类核心结果：

- 喷卵专项评分
- 幼虫累计时间线

喷卵评分主要从下面几个维度评估：

- 喷卵覆盖率
- 喷卵上线率
- 喷卵节奏稳定性
- 基地空窗时间
- 幼虫堆积压力

幼虫累计时间线则用于回答：

- 这局游戏中，这个玩家一共获得了多少幼虫
- 幼虫总量随时间是怎么增长的

这比单纯只看女王数量或基地数量更接近实际运营能力。

## 命令行使用

进入项目目录：

```powershell
cd C:\Users\qq563\Documents\codex1
```

分析示例 JSON：

```powershell
python sc2_inject_evaluator.py sample_match.json
```

分析单个回放：

```powershell
python sc2_inject_evaluator.py "你的录像.SC2Replay"
```

如果一个回放里有多个虫族玩家，可以指定玩家：

```powershell
python sc2_inject_evaluator.py "你的录像.SC2Replay" --player-name Serral
python sc2_inject_evaluator.py "你的录像.SC2Replay" --player-id 1
```

查看幼虫累计时间线：

```powershell
python sc2_inject_evaluator.py "你的录像.SC2Replay" --larva-timeline
```

输出 JSON：

```powershell
python sc2_inject_evaluator.py "你的录像.SC2Replay" --json
python sc2_inject_evaluator.py "你的录像.SC2Replay" --larva-timeline --json
```

只看解析后的回放结构：

```powershell
python sc2_inject_evaluator.py "你的录像.SC2Replay" --dump-parsed
```

批量分析整个回放目录：

```powershell
python sc2_inject_evaluator.py "回放目录路径"
```

## GUI 使用

如果直接启动脚本且不带参数，会自动打开图形界面：

```powershell
python sc2_inject_evaluator.py
```

也可以显式指定：

```powershell
python sc2_inject_evaluator.py --gui
```

GUI 支持：

- 选择 `replay` 或 `JSON` 文件
- 在多虫族玩家回放里选择目标玩家
- 切换输出模式
- 直接查看文本结果或 JSON 结果

## 打包后的 Windows GUI 版本

仓库中会附带一个可分发的 Windows GUI 压缩包：

- `release/sc2_inject_evaluator_gui_windows.zip`

解压后运行里面的：

- `sc2_inject_evaluator_gui.exe`

注意：

- `.exe` 和 `_internal` 文件夹必须放在同一目录
- 这是 `onedir` 打包，不是单文件版

## JSON 输入格式

示例：

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

## 回放解析逻辑

当前版本会做这些事：

- 找出目标虫族玩家
- 从 `tracker events` 跟踪基地的建造、完成、死亡
- 从 `SpawnLarva` 指令里提取喷卵事件
- 在 `PlayerStatsEvent` 上采样幼虫和人口信息
- 统计该玩家整局累计获得幼虫的总数与时间关系

当前评分是“基地视角”的，而不是“女王视角”的：

- 重点看每个基地的喷卵链有没有维持住
- 未完工就取消、未完工就被打掉、已摧毁的基地不会算进活跃基地
- 10 分钟后幼虫堆积项会自动降权，减少对高水平后期对局的误伤

## 当前局限

这个工具已经能用于训练和复盘，但仍然是专项工具，不是完整宏观评分器。

目前的局限包括：

- 指令事件不等于 100% 成功喷卵
- 幼虫按位置归属到最近基地，属于近似估计
- 还没有结合矿气、人口卡住、战斗压力做复杂度修正

## 仓库文件说明

- `sc2_inject_evaluator.py`：主程序
- `sample_match.json`：示例输入
- `README.md`：英文说明
- `README.zh-CN.md`：中文说明
- `release/sc2_inject_evaluator_gui_windows.zip`：Windows GUI 打包版本
