# OMSI Asset Manager / OMSI 资产管理器

A configuration management utility designed to isolate and deploy OMSI 2 Maps, Vehicles, and HOF assets through a discrete profile system.
一个配置管理工具，旨在通过独立的配置文件（Profile）系统来隔离和部署 OMSI 2 的地图（Maps）、车辆（Vehicles）以及 HOF 资产。

---

## Core Operational Logic / 核心运行逻辑

### 1. Asset Isolation (Deactivation) / 资产隔离（停用机制）

The program controls asset visibility in OMSI 2 by appending an `.inactivate` extension to unselected files. The game engine ignores files with this extension.
程序通过为未被选中的文件追加 `.inactivate` 扩展名来控制 OMSI 2 中资产的可见性。游戏引擎会自动忽略带有此扩展名的文件。

* **Maps (地图):** Toggles specific configuration files (`global.cfg`, `ailists.cfg`, `laststn.osn`, `laststn.osn.owt`). / 针对特定的配置文件执行切换。
* **Vehicles (车辆):** Scans recursively and toggles individual `.bus` files. Supports fine-grained isolation at the vehicle variant level via a hierarchical tree structure. / 递归扫描并切换独立的 `.bus` 文件。支持通过树状层级结构在车辆变种级别进行精细隔离。

### 2. HOF Backup & Distribution / HOF 备份与分发

* **Backup (备份):** Extracts all `.hof` files from the game directory to a central repository. Uses MD5 hashing for deduplication. Resolves filename collisions by prepending the parent vehicle folder name. / 从游戏目录中提取所有 `.hof` 文件至中央备份库。利用 MD5 哈希值进行去重。通过在文件名前添加父级车辆文件夹名称来解决同名冲突。
* **Deployment (部署):** Enforces exclusive HOF allocation. When applying a profile, the manager deletes all existing `.hof` files in active vehicle directories and copies only the user-selected `.hof` files. / 强制执行独占式 HOF 分配。应用配置方案时，管理器会删除激活车辆目录中现有的所有 `.hof` 文件，并仅复制用户选定的 `.hof` 文件。

### 3. Dependency Resolution / 依赖解析

Parses `ailists.cfg` and `ailist.cfg` within map directories to extract `.ovh`, `.bus`, and `.zug` string references. The GUI utilizes this data to auto-select required vehicle variants when a map is chosen.
解析地图目录内的 `ailists.cfg` 与 `ailist.cfg` 文件，提取 `.ovh`、`.bus` 和 `.zug` 字符串引用。在 GUI 中选择地图时，系统会利用此数据自动勾选所需的车辆变种。

---

## GUI Workflow / GUI 工作流

The GUI provides an interactive interface to construct profiles and manage asset deployment via multi-threading, preventing main window unresponsiveness.
GUI 提供交互式界面以构建配置方案，并通过多线程管理资产部署，避免主窗口失去响应。

1. **Initialization (初始化):**
* Define `Game Root` (must contain `omsi.exe`) and `Repo Root` (target directory for backups and profiles). / 设定 `Game Root`（必须包含 `omsi.exe`）与 `Repo Root`（用于存放备份和配置方案的目录）。
* Click `Save Defaults` to write these paths to a local `config.txt`. / 点击 `Save Defaults` 将路径写入本地的 `config.txt`。
* Execute `Backup All` during the initial run to index game assets and extract HOF files. / 初次运行时执行 `Backup All` 以建立游戏资产索引并提取 HOF 文件。


2. **Profile Configuration (配置方案设置):**
* **Maps (地图):** Select target maps. Associated AI vehicles will highlight automatically. / 选择目标地图。关联的 AI 车辆将被自动勾选。
* **Vehicles (车辆):** Utilize the tree-view interface. Expand (`▶` / `▼`) to select specific `.bus` variants, or check parent folders to toggle all child variants. / 使用树状视图界面。展开 (`▶` / `▼`) 选择特定的 `.bus` 变种，或点击父级文件夹勾选框以切换所有子变种。
* **HOF:** Select required `.hof` files for the current operational scenario. / 为当前运行场景选择所需的 `.hof` 文件。


3. **Execution (执行):**
* Input a profile name and click `Save Profile` to store the state in `profiles.json`. / 输入配置名称并点击 `Save Profile` 将状态保存至 `profiles.json`。
* Click `Apply Selections to Game` to execute the file renaming and HOF distribution logic via background threads. Real-time progress is displayed in the terminal output box. / 点击 `Apply Selections to Game`（应用选择到游戏），通过后台线程执行文件重命名与 HOF 分发逻辑。终端输出框会显示实时进度。



---

## Default Configuration File / 默认配置文件

The GUI automatically reads `config.txt` from the application's base directory upon launch.
GUI 在启动时会自动读取应用程序根目录下的 `config.txt`。

Example `config.txt` / 示例：

```txt
game_root=C:\OMSI 2
repo_root=D:\omsi_manager_repo

```

---

## Command Line Interface (CLI) / 命令行接口

The tool supports CLI execution for integration into automated environments.
该工具支持命令行执行，以便集成至自动化环境中。

**Backup Assets / 备份资产:**

```bash
python omsi_manager.py \
  --game-root /path/to/omsi \
  --repo-root /path/to/repo \
  backup-all

```

**Save a Profile via CLI / 通过命令行保存配置:**

```bash
python omsi_manager.py \
  --game-root /path/to/omsi \
  --repo-root /path/to/repo \
  profile-save yorkshire \
  --map Yorkshire \
  --vehicle YC_Masterdeck \
  --hof YC_Masterdeck_Yorkshire_3.0.hof

```

**Load and Apply an Active Profile / 加载并应用当前配置:**

```bash
python omsi_manager.py \
  --game-root /path/to/omsi \
  --repo-root /path/to/repo \
  profile-activate yorkshire

python omsi_manager.py \
  --game-root /path/to/omsi \
  --repo-root /path/to/repo \
  restore-active

```

---

## Compilation and Continuous Integration / 编译与持续集成

The repository includes a GitHub Actions workflow (`.github/workflows/build.yml`) that automatically compiles the Python script into a standalone Windows executable (`.exe`) using PyInstaller upon pushing to the `main` branch or pushing a tag.
代码库包含 GitHub Actions 工作流 (`.github/workflows/build.yml`)。当推送代码至 `main` 分支或推送标签时，系统将使用 PyInstaller 自动把 Python 脚本编译为独立的 Windows 可执行文件 (`.exe`)。

**Manual Local Compilation / 本地手动编译:**

1. Install PyInstaller / 安装 PyInstaller:

```bash
pip install pyinstaller

```

2. Compile the standalone executable / 编译独立可执行文件:

```bash
pyinstaller --noconfirm --onefile --windowed --name omsi_manager --distpath . omsi_manager.py

```

Output: `omsi_manager.exe` will be generated in the current directory. Double-click the executable to launch the GUI directly.
输出结果：`omsi_manager.exe` 将在当前目录生成。双击该可执行文件即可直接启动 GUI 界面。