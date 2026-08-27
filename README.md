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

## GUI 默认配置

- GUI 顶部可设置 `Game Root` 与 `Repo Root`
- `Game Root` 会校验是否包含 `omsi.exe`
- 点击 `Save Defaults` 会把默认值写入程序同目录下的 `config.txt`
- Windows 下双击 `omsi_manager.exe`（无参数启动）会直接进入 GUI，并自动读取 `config.txt`

`config.txt` 格式示例：

```txt
game_root=C:\OMSI 2
repo_root=D:\omsi_manager_repo
```

## 打包为 Windows 单文件 EXE（双击直接进入 GUI）

项目包含 `/home/runner/work/omsi_manager/omsi_manager/pyproject.yaml`（打包配置说明）。

1. 安装依赖：

```bash
pip install pyinstaller
```

2. 在仓库根目录执行打包：

```bash
pyinstaller --noconfirm --onefile --windowed --name omsi_manager --distpath .  omsi_manager.py
```

3. 产物位置：

- `../omsi_manager.exe`

4. 使用方式：

- 双击 `omsi_manager.exe` 即可直接进入 GUI 模式
- 首次设置好 `Game Root` / `Repo Root` 后点击 `Save Defaults`，下次双击会自动加载

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