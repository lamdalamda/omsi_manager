import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
from queue import Empty, Queue
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple


DEFAULT_CONFIG_FILE_NAME = "config.txt"


def _file_md5(file_path: Path) -> str:
    digest = hashlib.md5()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4096), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_rel_path(value: str) -> Path:
    normalized = value.strip().replace("\\", "/")
    return Path(normalized)


def _parse_ailist_refs(content: str) -> List[Path]:
    refs: List[Path] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("[") or line.startswith("*"):
            continue
        if not re.search(r"\.(ovh|bus|zug)\b", line, flags=re.IGNORECASE):
            continue
        path_token = re.split(r"\s+", line)[0]
        refs.append(_normalize_rel_path(path_token))
    return refs


def _vehicle_names_from_ailist_content(content: str) -> List[str]:
    names: List[str] = []
    seen: Set[str] = set()
    for rel_ref in _parse_ailist_refs(content):
        parts = rel_ref.parts
        if parts and parts[0].lower() == "vehicles" and len(parts) > 1:
            folder_name = parts[1]
            key = folder_name.lower()
            if key not in seen:
                seen.add(key)
                names.append(folder_name)
        stem = rel_ref.stem
        if stem:
            key = stem.lower()
            if key not in seen:
                seen.add(key)
                names.append(stem)
    return names


class OmsiAssetManager:
    def __init__(self, game_root: Path, repo_root: Path):
        self.game_root = game_root
        self.repo_root = repo_root
        self.backup_root = repo_root / "backups"
        self.hof_backup = self.backup_root / "hof"
        self.profiles_path = repo_root / "profiles.json"
        self.map_toggle_files = ("global.cfg", "ailists.cfg", "laststn.osn", "laststn.osn.owt")

    @property
    def vehicles_path(self) -> Path:
        return self.game_root / "vehicles"

    @property
    def maps_path(self) -> Path:
        return self.game_root / "maps"

    def backup_hofs(self) -> int:
        self.hof_backup.mkdir(parents=True, exist_ok=True)
        backup_count = 0
        for hof_file in self.vehicles_path.rglob("*.hof"):
            vehicle_folder_name = hof_file.parent.name
            current_md5 = _file_md5(hof_file)
            backup_file = self.hof_backup / hof_file.name
            if backup_file.exists():
                existing_md5 = _file_md5(backup_file)
                if existing_md5 == current_md5:
                    continue
                backup_file = self.hof_backup / f"{vehicle_folder_name}_{hof_file.name}"
                if backup_file.exists() and _file_md5(backup_file) == current_md5:
                    continue
            shutil.copy2(hof_file, backup_file)
            backup_count += 1
        return backup_count

    def backup_all(
        self,
        progress_callback: Optional[Callable[[str, int, int, int], None]] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Dict[str, int]:
        steps = [
            ("hof", self.backup_hofs),
        ]
        total = len(steps)
        results: Dict[str, int] = {}
        for index, (name, action) in enumerate(steps, start=1):
            if stop_check and stop_check():
                raise RuntimeError("Backup interrupted")
            step_result = action()
            results[name] = step_result
            if progress_callback:
                progress_callback(name, index, total, step_result)
        return results

    def _load_profiles(self) -> Dict[str, object]:
        if not self.profiles_path.exists():
            return {"active_profile": None, "profiles": {}}
        return json.loads(self.profiles_path.read_text(encoding="utf-8"))

    def _save_profiles(self, data: Dict[str, object]) -> None:
        self.profiles_path.parent.mkdir(parents=True, exist_ok=True)
        self.profiles_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def save_profile(
        self,
        name: str,
        hofs: Optional[List[Dict[str, object]]] = None,
        maps: Optional[List[str]] = None,
        vehicles: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        data = self._load_profiles()
        profile = {
            "hofs": hofs or [],
            "maps": maps or [],
            "vehicles": vehicles or {},
        }
        profiles = data.setdefault("profiles", {})
        profiles[name] = profile
        self._save_profiles(data)

    def get_profile(self, name: str) -> Dict[str, object]:
        data = self._load_profiles()
        profiles = data.get("profiles", {})
        if name not in profiles:
            raise KeyError(f"Profile not found: {name}")
        return profiles[name]

    def list_profiles(self) -> List[str]:
        data = self._load_profiles()
        profiles = data.get("profiles", {})
        return sorted(profiles.keys())

    def list_maps_with_status(self) -> List[Dict[str, object]]:
        result: List[Dict[str, object]] = []
        if not self.maps_path.exists():
            return result
        for map_dir in sorted(self.maps_path.iterdir(), key=lambda item: item.name.lower()):
            if not map_dir.is_dir():
                continue
            inactive = False
            for file_name in self.map_toggle_files:
                if (map_dir / f"{file_name}.inactivate").exists():
                    inactive = True
                    break
            result.append({"name": map_dir.name, "active": not inactive})
        return result

    def list_vehicles_with_status(self) -> List[Dict[str, object]]:
        result: List[Dict[str, object]] = []
        if not self.vehicles_path.exists():
            return result
        for vehicle_dir in sorted(self.vehicles_path.iterdir(), key=lambda item: item.name.lower()):
            if not vehicle_dir.is_dir():
                continue
            
            buses = []
            for bus_path in vehicle_dir.rglob("*.bus"):
                buses.append({"name": bus_path.relative_to(vehicle_dir).as_posix(), "active": True})
            for bus_path in vehicle_dir.rglob("*.bus.inactivate"):
                buses.append({"name": bus_path.relative_to(vehicle_dir).with_suffix("").as_posix(), "active": False})
                
            if buses:
                buses.sort(key=lambda b: b["name"].lower())
                result.append({"folder": vehicle_dir.name, "buses": buses})
        return result

    def _set_map_active(self, map_name: str, active: bool) -> int:
        map_dir = self.maps_path / map_name
        if not map_dir.exists():
            return 0
        renamed = 0
        for file_name in self.map_toggle_files:
            normal = map_dir / file_name
            inactive = map_dir / f"{file_name}.inactivate"
            if active and inactive.exists():
                inactive.rename(normal)
                renamed += 1
            elif not active and normal.exists():
                normal.rename(inactive)
                renamed += 1
        return renamed

    def apply_selections(self, selected_maps: List[str], selected_buses: Dict[str, Set[str]], selected_hofs: List[str], progress_callback: Optional[Callable[[str, int, int], None]] = None) -> Dict[str, int]:
        maps_set = {item.lower() for item in selected_maps}
        map_toggle_count = 0
        vehicle_toggle_count = 0
        hof_count = 0
        
        all_maps = self.list_maps_with_status()
        all_vehicles = self.list_vehicles_with_status()
        target_vehicle_dirs = [path for path in self.vehicles_path.iterdir() if path.is_dir()] if self.vehicles_path.exists() else []
        
        total_steps = len(all_maps) + sum(len(v["buses"]) for v in all_vehicles) + (len(selected_hofs) * len(target_vehicle_dirs))
        current_step = 0

        for item in all_maps:
            map_name = str(item["name"])
            if progress_callback:
                progress_callback(f"正在配置地图: {map_name}", current_step, total_steps)
            map_toggle_count += self._set_map_active(map_name, map_name.lower() in maps_set)
            current_step += 1

        for item in all_vehicles:
            folder = str(item["folder"])
            target_active = selected_buses.get(folder, set())
            vehicle_dir = self.vehicles_path / folder
            
            if vehicle_dir.exists():
                for path in vehicle_dir.rglob("*.bus"):
                    rel_name = path.relative_to(vehicle_dir).as_posix()
                    if rel_name not in target_active:
                        if progress_callback:
                            progress_callback(f"停用车辆: {folder}/{rel_name}", current_step, total_steps)
                        path.rename(path.with_name(path.name + ".inactivate"))
                        vehicle_toggle_count += 1
                    current_step += 1
                        
                for path in vehicle_dir.rglob("*.bus.inactivate"):
                    rel_name = path.relative_to(vehicle_dir).with_suffix("").as_posix()
                    if rel_name in target_active:
                        if progress_callback:
                            progress_callback(f"激活车辆: {folder}/{rel_name}", current_step, total_steps)
                        path.rename(path.with_suffix(""))
                        vehicle_toggle_count += 1
                    current_step += 1

        if selected_hofs:
            selected_hofs_set = set(selected_hofs)
            for backup_name in selected_hofs_set:
                if not (self.hof_backup / backup_name).exists():
                    raise FileNotFoundError(f"HOF 备份未找到: {backup_name}")

            for destination_dir in target_vehicle_dirs:
                if progress_callback:
                    progress_callback(f"正在分发 HOF: {destination_dir.name}", current_step, total_steps)
                for existing_hof in destination_dir.glob("*.hof"):
                    if existing_hof.name not in selected_hofs_set:
                        try:
                            existing_hof.unlink()
                        except FileNotFoundError:
                            pass
                for backup_name in selected_hofs_set:
                    source_hof = self.hof_backup / backup_name
                    target_hof = destination_dir / backup_name
                    shutil.copy2(source_hof, target_hof)
                    hof_count += 1
                current_step += len(selected_hofs_set)

        if progress_callback:
            progress_callback("配置完成！", total_steps, total_steps)

        return {
            "maps": len(selected_maps),
            "vehicles_folders_affected": len(selected_buses),
            "map_toggles": map_toggle_count,
            "vehicle_toggles": vehicle_toggle_count,
            "hof_copies": hof_count,
        }


def _parse_hof_spec(spec: str) -> Dict[str, object]:
    parts = [item.strip() for item in spec.split(":") if item.strip()]
    if not parts:
        raise ValueError("--hof format must include backup_name")
    return {"backup_name": parts[0]}


def _app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _default_config_path() -> Path:
    return _app_base_dir() / DEFAULT_CONFIG_FILE_NAME


def _load_config(config_path: Path) -> Dict[str, str]:
    if not config_path.exists():
        return {}
    values: Dict[str, str] = {}
    for raw_line in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _save_config(config_path: Path, game_root: str, repo_root: str) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"game_root={game_root}\nrepo_root={repo_root}\n",
        encoding="utf-8",
    )


def _validate_game_root(game_root: Path) -> None:
    omsi_exe = game_root / "omsi.exe"
    if not game_root.exists() or not game_root.is_dir():
        raise ValueError(f"Game root does not exist or is not a directory: {game_root}")
    if not omsi_exe.exists():
        raise ValueError(f"Game root must contain omsi.exe: {omsi_exe}")


def _resolved_startup_roots(default_game_root: str, default_repo_root: str, config_path: Path) -> Dict[str, str]:
    config = _load_config(config_path)
    game_root = config.get("game_root", default_game_root)
    repo_root = config.get("repo_root", default_repo_root)
    return {"game_root": game_root, "repo_root": repo_root}


def launch_gui(default_game_root: str = ".", default_repo_root: str = ".") -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, scrolledtext, ttk
    except Exception as exc:
        raise RuntimeError("Tkinter is required for GUI mode") from exc

    config_path = _default_config_path()
    startup = _resolved_startup_roots(default_game_root, default_repo_root, config_path)

    root = tk.Tk()
    root.title("OMSI Manager")
    root.geometry("1100x760")

    game_root_var = tk.StringVar(value=startup["game_root"])
    repo_root_var = tk.StringVar(value=startup["repo_root"])
    profile_name_var = tk.StringVar()
    status_var = tk.StringVar(value="准备就绪")
    backup_progress_var = tk.DoubleVar(value=0)

    tk.Label(root, text="游戏目录 (Game Root)").grid(row=0, column=0, sticky="w", padx=8, pady=4)
    tk.Entry(root, textvariable=game_root_var, width=70).grid(row=0, column=1, columnspan=6, sticky="we", padx=8, pady=4)
    tk.Button(
        root,
        text="浏览",
        command=lambda: game_root_var.set(filedialog.askdirectory() or game_root_var.get()),
    ).grid(row=0, column=7, sticky="we", padx=8, pady=4)

    tk.Label(root, text="备份目录 (Repo Root)").grid(row=1, column=0, sticky="w", padx=8, pady=4)
    tk.Entry(root, textvariable=repo_root_var, width=70).grid(row=1, column=1, columnspan=6, sticky="we", padx=8, pady=4)
    tk.Button(
        root,
        text="浏览",
        command=lambda: repo_root_var.set(filedialog.askdirectory() or repo_root_var.get()),
    ).grid(row=1, column=7, sticky="we", padx=8, pady=4)

    tk.Label(root, text="配置名称 (Profile)").grid(row=2, column=0, sticky="w", padx=8, pady=4)
    tk.Entry(root, textvariable=profile_name_var, width=40).grid(row=2, column=1, sticky="w", padx=8, pady=4)

    action_bar = tk.Frame(root)
    action_bar.grid(row=3, column=0, columnspan=8, sticky="we", padx=8, pady=6)

    backup_progress = ttk.Progressbar(
        root,
        orient="horizontal",
        mode="determinate",
        variable=backup_progress_var,
        maximum=4,
    )
    backup_progress.grid(row=4, column=0, columnspan=8, sticky="we", padx=8, pady=4)
    tk.Label(root, textvariable=status_var, anchor="w").grid(row=5, column=0, columnspan=8, sticky="we", padx=8, pady=2)

    selector_container = tk.Frame(root)
    selector_container.grid(row=6, column=0, columnspan=8, sticky="nsew", padx=8, pady=8)

    maps_frame = tk.Frame(selector_container, bg="white", bd=1, relief="solid")
    vehicles_frame = tk.Frame(selector_container, bg="white", bd=1, relief="solid")
    hofs_frame = tk.Frame(selector_container, bg="white", bd=1, relief="solid")
    maps_frame.grid(row=0, column=0, sticky="nsew", padx=4)
    vehicles_frame.grid(row=0, column=1, sticky="nsew", padx=4)
    hofs_frame.grid(row=0, column=2, sticky="nsew", padx=4)

    for column_index in range(3):
        selector_container.grid_columnconfigure(column_index, weight=1)
    selector_container.grid_rowconfigure(0, weight=1)

    tk.Label(maps_frame, text="地图 (Maps)", bg="white").pack(side=tk.TOP, anchor="w", padx=8, pady=(8, 4))
    maps_listbox = tk.Listbox(maps_frame, selectmode=tk.SINGLE, exportselection=False, height=14)

    tk.Label(vehicles_frame, text="车辆 (Vehicles) - 支持树状展开", bg="white").pack(side=tk.TOP, anchor="w", padx=8, pady=(8, 4))
    vehicles_listbox = tk.Listbox(vehicles_frame, selectmode=tk.SINGLE, exportselection=False, height=14)

    tk.Label(hofs_frame, text="HOF 备份", bg="white").pack(side=tk.TOP, anchor="w", padx=8, pady=(8, 4))
    hofs_listbox = tk.Listbox(hofs_frame, selectmode=tk.SINGLE, exportselection=False, height=14)

    output = scrolledtext.ScrolledText(root, width=110, height=20, state=tk.DISABLED)
    output.grid(row=8, column=0, columnspan=8, sticky="nsew", padx=8, pady=8)

    for column_index in range(8):
        root.grid_columnconfigure(column_index, weight=1 if column_index > 0 else 0)
    root.grid_rowconfigure(6, weight=1)
    root.grid_rowconfigure(8, weight=2)

    # State Variables
    map_vehicle_auto_refs: Dict[str, Set[str]] = {}
    map_display_name_lookup: Dict[str, str] = {}
    background_backup_process: Dict[str, object] = {"proc": None, "queue": None, "cancelled": False}
    startup_prompt_shown = {"value": False}
    last_output_transient = {"value": False}

    # Vehicles Tree State
    vehicle_tree_data: List[Dict[str, object]] = []
    vehicle_expanded: Set[str] = set()
    vehicle_selections: Dict[str, Set[str]] = {}
    vehicle_row_map: List[Tuple[str, str, str]] = []

    def _manager_from_inputs(validate_game_root: bool = True) -> OmsiAssetManager:
        game_root = Path(game_root_var.get().strip() or ".").expanduser()
        if validate_game_root:
            _validate_game_root(game_root)
        repo_root = Path(repo_root_var.get()).expanduser()
        return OmsiAssetManager(game_root, repo_root)

    def _emit(payload: object, transient: bool = False) -> None:
        output.config(state=tk.NORMAL)
        if last_output_transient["value"]:
            output.delete("end-2l", "end-1c")
        text = json.dumps(payload, ensure_ascii=False, indent=2) if not isinstance(payload, str) else payload
        output.insert(tk.END, f"{text}\n")
        output.see(tk.END)
        last_output_transient["value"] = transient
        output.config(state=tk.DISABLED)

    def _run(action) -> None:
        try:
            result = action()
            if result is not None:
                _emit({"ok": True, "result": result})
        except Exception as exc:
            _emit({"ok": False, "error": str(exc)})

    # ================= Listbox Rendering Utilities =================
    def _toggle_simple_listbox(event: "tk.Event", listbox: "tk.Listbox") -> None:
        index = listbox.nearest(event.y)
        if index < 0:
            return
        text = str(listbox.get(index))
        if text.startswith("[X] "):
            listbox.delete(index)
            listbox.insert(index, f"[ ] {text[4:]}")
        elif text.startswith("[ ] "):
            listbox.delete(index)
            listbox.insert(index, f"[X] {text[4:]}")
        listbox.selection_clear(0, tk.END)

    def _render_vehicles() -> None:
        vehicles_listbox.delete(0, tk.END)
        vehicle_row_map.clear()
        
        for item in vehicle_tree_data:
            folder = str(item["folder"])
            buses = item["buses"]
            active_buses = vehicle_selections.get(folder, set())
            
            if len(active_buses) == len(buses) and len(buses) > 0:
                box = "[X]"
            elif len(active_buses) > 0:
                box = "[-]"
            else:
                box = "[ ]"
                
            is_expanded = folder in vehicle_expanded
            arrow = "▼" if is_expanded else "▶"
            
            vehicles_listbox.insert(tk.END, f"{arrow} {box} {folder}")
            vehicle_row_map.append(("folder", folder, ""))
            
            if is_expanded:
                for b in buses:
                    b_name = b["name"]
                    b_box = "[X]" if b_name in active_buses else "[ ]"
                    vehicles_listbox.insert(tk.END, f"    {b_box} {b_name}")
                    vehicle_row_map.append(("bus", folder, b_name))

    def _toggle_vehicles_tree(event: "tk.Event") -> None:
        index = vehicles_listbox.nearest(event.y)
        if index < 0 or index >= len(vehicle_row_map): return
        
        row_type, folder, bus_name = vehicle_row_map[index]
        is_arrow_click = event.x < 25
        
        if row_type == "folder":
            if is_arrow_click:
                if folder in vehicle_expanded:
                    vehicle_expanded.remove(folder)
                else:
                    vehicle_expanded.add(folder)
            else:
                buses = next(item["buses"] for item in vehicle_tree_data if item["folder"] == folder)
                active_buses = vehicle_selections.setdefault(folder, set())
                if len(active_buses) == len(buses):
                    active_buses.clear()
                else:
                    active_buses.update(b["name"] for b in buses)
        else:
            active_buses = vehicle_selections.setdefault(folder, set())
            if bus_name in active_buses:
                active_buses.remove(bus_name)
            else:
                active_buses.add(bus_name)
                
        vehicles_listbox.selection_clear(0, tk.END)
        _render_vehicles()

    maps_listbox.bind("<ButtonRelease-1>", lambda e: [root.after(50, _apply_map_vehicle_auto_select), _toggle_simple_listbox(e, maps_listbox)])
    vehicles_listbox.bind("<ButtonRelease-1>", _toggle_vehicles_tree)
    hofs_listbox.bind("<ButtonRelease-1>", lambda e: _toggle_simple_listbox(e, hofs_listbox))

    def _selected_values(listbox: "tk.Listbox") -> List[str]:
        return [str(listbox.get(i))[4:] for i in range(listbox.size()) if str(listbox.get(i)).startswith("[X] ")]

    def _selected_names(listbox: "tk.Listbox", lookup: Optional[Dict[str, str]] = None) -> List[str]:
        values = _selected_values(listbox)
        if not lookup:
            return values
        return [lookup.get(value, value) for value in values]

    def _select_values(listbox: "tk.Listbox", values: Iterable[str], clear_existing: bool = True) -> None:
        target = {str(value).lower() for value in values}
        for index in range(listbox.size()):
            text = str(listbox.get(index))
            core_text = text[4:]
            if clear_existing:
                prefix = "[X] " if core_text.lower() in target else "[ ] "
            else:
                prefix = "[X] " if text.startswith("[X] ") or core_text.lower() in target else "[ ] "
            if text != f"{prefix}{core_text}":
                listbox.delete(index)
                listbox.insert(index, f"{prefix}{core_text}")

    def _fill_simple_listbox(listbox: "tk.Listbox", values_with_state: Iterable[Tuple[str, bool]]) -> None:
        listbox.delete(0, tk.END)
        for value, is_active in sorted(values_with_state, key=lambda item: item[0].lower()):
            prefix = "[X] " if is_active else "[ ] "
            listbox.insert(tk.END, f"{prefix}{value}")

    def _set_all_state_simple(listbox: "tk.Listbox", state: bool) -> None:
        for index in range(listbox.size()):
            text = str(listbox.get(index))[4:]
            prefix = "[X] " if state else "[ ] "
            listbox.delete(index)
            listbox.insert(index, f"{prefix}{text}")

    def _set_all_state_vehicles(state: bool) -> None:
        for item in vehicle_tree_data:
            folder = item["folder"]
            if state:
                vehicle_selections[folder] = set(b["name"] for b in item["buses"])
            else:
                vehicle_selections[folder] = set()
        _render_vehicles()

    def _add_selection_buttons(container: "tk.Frame", listbox: "tk.Listbox", is_tree: bool = False) -> None:
        button_bar = tk.Frame(container, bg="white")
        button_bar.pack(side=tk.BOTTOM, fill="x", padx=8, pady=8)
        
        if is_tree:
            tk.Button(button_bar, text="全选", command=lambda: _set_all_state_vehicles(True)).pack(side="left", padx=2)
            tk.Button(button_bar, text="清空", command=lambda: _set_all_state_vehicles(False)).pack(side="left", padx=2)
            tk.Button(button_bar, text="全部展开", command=lambda: [vehicle_expanded.update(i["folder"] for i in vehicle_tree_data), _render_vehicles()]).pack(side="left", padx=2)
            tk.Button(button_bar, text="全部折叠", command=lambda: [vehicle_expanded.clear(), _render_vehicles()]).pack(side="left", padx=2)
        else:
            tk.Button(button_bar, text="全选", command=lambda: _set_all_state_simple(listbox, True)).pack(side="left", padx=2)
            tk.Button(button_bar, text="清空", command=lambda: _set_all_state_simple(listbox, False)).pack(side="left", padx=2)

    _add_selection_buttons(maps_frame, maps_listbox)
    _add_selection_buttons(vehicles_frame, vehicles_listbox, is_tree=True)
    _add_selection_buttons(hofs_frame, hofs_listbox)

    maps_listbox.pack(side=tk.TOP, fill="both", expand=True, padx=8, pady=4)
    vehicles_listbox.pack(side=tk.TOP, fill="both", expand=True, padx=8, pady=4)
    hofs_listbox.pack(side=tk.TOP, fill="both", expand=True, padx=8, pady=4)
    def _has_initial_backup(repo_root: Path) -> bool:
        hof_backup = repo_root / "backups" / "hof"
        return hof_backup.exists() and any(hof_backup.glob("*.hof"))

    def _collect_map_vehicle_refs(map_dir: Path) -> Set[str]:
        refs: Set[str] = set()
        for file_name in ("ailists.cfg", "ailist.cfg"):
            for ailist_path in map_dir.rglob(file_name):
                names = _vehicle_names_from_ailist_content(ailist_path.read_text(encoding="utf-8", errors="ignore"))
                refs.update(name.lower() for name in names)
        return refs
    def _set_progress(current: int, total: int, message: str) -> None:
        backup_progress.configure(maximum=max(total, 1))
        backup_progress_var.set(current)
        status_var.set(message)
        root.update_idletasks()

    def _load_backup_data(show_prompt: bool = False) -> None:
        map_vehicle_auto_refs.clear()
        map_display_name_lookup.clear()
        vehicle_tree_data.clear()
        vehicle_selections.clear()
        
        repo_raw = repo_root_var.get().strip()
        if not repo_raw:
            _fill_simple_listbox(maps_listbox, [])
            _render_vehicles()
            _fill_simple_listbox(hofs_listbox, [])
            status_var.set("备份目录为空")
            if show_prompt and not startup_prompt_shown["value"]:
                startup_prompt_shown["value"] = True
                messagebox.showinfo("提示", "尚未设置备份目录，请设置并点击“备份全部”。")
            return

        repo_root = Path(repo_raw).expanduser()
        manager = OmsiAssetManager(Path(game_root_var.get().strip() or ".").expanduser(), repo_root)
        _set_progress(0, 3, "正在加载游戏资产...")

        maps_with_status = manager.list_maps_with_status()
        maps_display = []
        for item in maps_with_status:
            name = str(item["name"])
            active = bool(item["active"])
            maps_display.append((name, active))
            map_display_name_lookup[name] = name
        _set_progress(1, 3, "地图加载完成")

        vehicles_with_status = manager.list_vehicles_with_status()
        for item in vehicles_with_status:
            folder = item["folder"]
            buses = item["buses"]
            vehicle_tree_data.append(item)
            
            active_set = set(b["name"] for b in buses if b["active"])
            if active_set:
                vehicle_selections[folder] = active_set
                
        _set_progress(2, 3, "车辆加载完成")

        hofs = []
        if manager.hof_backup.exists():
            hofs = [(path.name, False) for path in manager.hof_backup.glob("*.hof") if path.is_file()]
        _set_progress(3, 3, "HOF 文件加载完成")

        for map_name in [str(item["name"]) for item in maps_with_status]:
            refs = _collect_map_vehicle_refs(manager.maps_path / map_name)
            map_vehicle_auto_refs[map_name] = refs

        _fill_simple_listbox(maps_listbox, maps_display)
        _render_vehicles()
        _fill_simple_listbox(hofs_listbox, hofs)

        hof_backup = repo_root / "backups" / "hof"
        if not (hof_backup.exists() and any(hof_backup.glob("*.hof"))):
            status_var.set("未找到初次备份记录。请设置游戏目录后点击“备份全部”。")
        else:
            status_var.set("加载完成")

    def _apply_map_vehicle_auto_select(_event=None) -> None:
        selected_maps = _selected_names(maps_listbox, map_display_name_lookup)
        auto_targets: Set[str] = set()
        for map_name in selected_maps:
            auto_targets.update(map_vehicle_auto_refs.get(map_name, set()))
            
        if not auto_targets: return
        
        for item in vehicle_tree_data:
            folder = item["folder"]
            folder_lower = folder.lower()
            if any(target in folder_lower or folder_lower in target for target in auto_targets):
                vehicle_selections[folder] = set(b["name"] for b in item["buses"])
        _render_vehicles()

    def _start_backup_subprocess() -> None:
        if background_backup_process["proc"] is not None:
            raise RuntimeError("后台进程已在运行")
        manager = _manager_from_inputs(validate_game_root=True)
        manager.repo_root.mkdir(parents=True, exist_ok=True)
        _save_config(config_path, str(manager.game_root), str(manager.repo_root))

        if getattr(sys, "frozen", False):
            command = [sys.executable]
        else:
            command = [sys.executable, "-u", str(Path(__file__).resolve())]
        command += ["--game-root", str(manager.game_root), "--repo-root", str(manager.repo_root), "backup-all", "--progress-json-lines"]

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        events: "Queue[Dict[str, object]]" = Queue()
        background_backup_process["proc"] = process
        background_backup_process["queue"] = events
        background_backup_process["cancelled"] = False
        _set_progress(0, 1, "备份中...")

        def _reader() -> None:
            assert process.stdout is not None
            assert process.stderr is not None
            for line in process.stdout:
                payload = line.strip()
                if payload:
                    events.put({"type": "stdout", "payload": payload})
            stderr_text = process.stderr.read()
            return_code = process.wait()
            events.put({"type": "done", "return_code": return_code, "stderr": stderr_text})

        threading.Thread(target=_reader, daemon=True).start()
        _poll_process_events("backup")

    def _apply_ui_selection_to_game() -> None:
        if background_backup_process["proc"] is not None:
            raise RuntimeError("后台进程已在运行")
            
        maps = _selected_names(maps_listbox, map_display_name_lookup)
        buses = {k: set(v) for k, v in vehicle_selections.items() if v}
        hofs = _selected_values(hofs_listbox)
        manager = _manager_from_inputs(validate_game_root=False)
        
        events: "Queue[Dict[str, object]]" = Queue()
        background_backup_process["proc"] = object()
        background_backup_process["queue"] = events
        background_backup_process["cancelled"] = False
        
        def run_apply():
            def cb(msg, current, total):
                events.put({"type": "progress", "msg": msg, "current": current, "total": total})
            try:
                result = manager.apply_selections(maps, buses, hofs, progress_callback=cb)
                events.put({"type": "done", "return_code": 0, "result": result})
            except Exception as e:
                events.put({"type": "done", "return_code": 1, "stderr": str(e)})

        threading.Thread(target=run_apply, daemon=True).start()
        _poll_process_events("apply")

    def _poll_process_events(mode: str) -> None:
        events = background_backup_process["queue"]
        if events is None: return
        
        finished = False
        final_result = None
        stderr_text = ""
        return_code = 1
        
        while True:
            try:
                event = events.get_nowait()
            except Empty:
                break
                
            if event["type"] == "stdout":
                raw_payload = str(event["payload"])
                try:
                    payload = json.loads(raw_payload)
                    if payload.get("event") == "backup_progress":
                        current = int(payload.get("current", 0))
                        total = int(payload.get("total", 1))
                        step = str(payload.get("step", ""))
                        copied = int(payload.get("copied", 0))
                        _set_progress(current, total, f"备份 {step} 完成: {copied}")
                    elif "hof" in payload:
                        final_result = {"hof": int(payload["hof"])}
                        _emit({"ok": True, "result": final_result})
                    else:
                        _emit(payload)
                except json.JSONDecodeError:
                    _emit({"raw": raw_payload})
            elif event["type"] == "progress":
                msg = str(event["msg"])
                current = int(event["current"])
                total = int(event["total"])
                _set_progress(current, total, msg)
                _emit(f"> {msg}", transient=True)
            elif event["type"] == "done":
                finished = True
                return_code = int(event["return_code"])
                stderr_text = str(event.get("stderr", ""))
                if "result" in event:
                    final_result = event["result"]
                    
        if not finished:
            root.after(80, lambda: _poll_process_events(mode))
            return

        background_backup_process["proc"] = None
        background_backup_process["queue"] = None

        if background_backup_process.get("cancelled"):
            status_var.set("操作已中止")
            messagebox.showinfo("提示", "操作已被用户中止。")
            _load_backup_data(show_prompt=False)
            return

        if return_code == 0:
            status_var.set("处理完成")
            if mode == "apply" and final_result:
                _emit({"ok": True, "result": final_result})
                messagebox.showinfo("应用完成", "各项配置已成功应用至游戏！")
            elif mode == "backup" and final_result:
                messagebox.showinfo("备份完成", f"HOF 文件数: {final_result.get('hof', 0)}")
            _load_backup_data(show_prompt=False)
        else:
            status_var.set("操作失败")
            messagebox.showerror("失败", stderr_text or "执行期间出现异常退出。")

    def _cancel_backup() -> None:
        process = background_backup_process["proc"]
        if process is None: return
        background_backup_process["cancelled"] = True
        if hasattr(process, "terminate"):
            process.terminate()

    def _save_profile() -> None:
        name = profile_name_var.get().strip()
        if not name:
            raise ValueError("必须填写配置名称 (Profile Name)")
        maps = _selected_names(maps_listbox, map_display_name_lookup)
        vehicles = {k: list(v) for k, v in vehicle_selections.items() if v}
        hofs = [{"backup_name": hof_name} for hof_name in _selected_values(hofs_listbox)]
        _manager_from_inputs().save_profile(name=name, maps=maps, vehicles=vehicles, hofs=hofs)
        return {"saved": name}

    def _load_profile_to_ui() -> None:
        profile = _manager_from_inputs().get_profile(profile_name_var.get().strip())
        
        maps = [str(item) for item in profile.get("maps", [])]
        _select_values(maps_listbox, maps)
        
        hofs = [str(item.get("backup_name", "")) for item in profile.get("hofs", []) if isinstance(item, dict)]
        _select_values(hofs_listbox, hofs)
        
        saved_vehicles = profile.get("vehicles", {})
        vehicle_selections.clear()
        
        if isinstance(saved_vehicles, dict):
            for k, v in saved_vehicles.items():
                vehicle_selections[k] = set(v)
        elif isinstance(saved_vehicles, list):
            for folder_name in saved_vehicles:
                for item in vehicle_tree_data:
                    if item["folder"] == folder_name:
                        vehicle_selections[folder_name] = set(b["name"] for b in item["buses"])
                        
        _render_vehicles()
        _apply_map_vehicle_auto_select()

    tk.Button(action_bar, text="备份全部", command=lambda: _run(_start_backup_subprocess)).pack(side="left", padx=4)
    tk.Button(action_bar, text="中止备份", command=_cancel_backup).pack(side="left", padx=4)
    tk.Button(action_bar, text="刷新状态", command=lambda: _run(lambda: _load_backup_data(show_prompt=False))).pack(side="left", padx=4)
    tk.Button(action_bar, text="保存方案", command=lambda: _run(_save_profile)).pack(side="left", padx=4)
    tk.Button(action_bar, text="加载方案", command=lambda: _run(_load_profile_to_ui)).pack(side="left", padx=4)
    tk.Button(action_bar, text="方案列表", command=lambda: _run(lambda: _manager_from_inputs().list_profiles())).pack(side="left", padx=4)
    tk.Button(action_bar, text="应用选择到游戏", command=lambda: _run(_apply_ui_selection_to_game)).pack(side="left", padx=4)
    tk.Button(action_bar, text="清空日志", command=lambda: [output.config(state=tk.NORMAL), output.delete("1.0", tk.END), output.config(state=tk.DISABLED)]).pack(side="left", padx=4)

    root.after(150, lambda: _load_backup_data(show_prompt=True))
    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OMSI asset backup/profile manager")
    parser.add_argument("--game-root", default=".", help="OMSI root path (contains Vehicles/Maps)")
    parser.add_argument("--repo-root", default=".", help="Repository root path for backups/config")
    sub = parser.add_subparsers(dest="command", required=True)

    backup_all = sub.add_parser("backup-all")
    backup_all.add_argument("--progress-json-lines", action="store_true")
    sub.add_parser("gui")
    return parser


def main() -> int:
    if len(sys.argv) == 1:
        startup = _resolved_startup_roots(".", str(_app_base_dir()), _default_config_path())
        return launch_gui(default_game_root=startup["game_root"], default_repo_root=startup["repo_root"])

    parser = build_parser()
    args = parser.parse_args()
    if args.command == "gui":
        return launch_gui(default_game_root=args.game_root, default_repo_root=args.repo_root)

    manager = OmsiAssetManager(Path(args.game_root), Path(args.repo_root))

    if args.command == "backup-all":
        if args.progress_json_lines:
            def _progress(step_name: str, current: int, total: int, copied: int) -> None:
                print(
                    json.dumps(
                        {
                            "event": "backup_progress",
                            "step": step_name,
                            "current": current,
                            "total": total,
                            "copied": copied,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            result = manager.backup_all(progress_callback=_progress)
        else:
            result = manager.backup_all()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    parser.error(f"Unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())