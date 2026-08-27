# omsi_manager

用于备份/恢复 OMSI 的 Vehicles、Maps 与 HOF 资产，并通过档案（profile）管理一组可激活内容。

## 主要能力

- 备份 `vehicles` 下所有 `.hof`（按内容去重，重名不同内容自动加前缀）
- 备份 `maps`、`vehicles` 目录
- 解析 `maps/**/ailists.cfg`，备份其中引用的 `.ovh/.bus/.zug` 资产
- 支持 profile 保存/读取/激活/恢复
- profile 支持多选（多个 map、vehicle、hof）

## 命令示例

```bash
python /home/runner/work/omsi_manager/omsi_manager/omsi_manager.py \
  --game-root /path/to/omsi \
  --repo-root /home/runner/work/omsi_manager/omsi_manager \
  backup-all
```

```bash
python /home/runner/work/omsi_manager/omsi_manager/omsi_manager.py \
  --game-root /path/to/omsi \
  --repo-root /home/runner/work/omsi_manager/omsi_manager \
  gui
```

```bash
python /home/runner/work/omsi_manager/omsi_manager/omsi_manager.py \
  --game-root /path/to/omsi \
  --repo-root /home/runner/work/omsi_manager/omsi_manager \
  profile-save yorkshire \
  --map Yorkshire \
  --vehicle YC_Masterdeck \
  --hof YC_Masterdeck_Yorkshire_3.0.hof:YC_Masterdeck:Yorkshire_3.0.hof
```

```bash
python /home/runner/work/omsi_manager/omsi_manager/omsi_manager.py \
  --game-root /path/to/omsi \
  --repo-root /home/runner/work/omsi_manager/omsi_manager \
  profile-activate yorkshire
python /home/runner/work/omsi_manager/omsi_manager/omsi_manager.py \
  --game-root /path/to/omsi \
  --repo-root /home/runner/work/omsi_manager/omsi_manager \
  restore-active
```