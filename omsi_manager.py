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
from typing import Callable, Dict, Iterable, List, Optional, Set


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
        vehicles: Optional[List[str]] = None,
    ) -> None:
        data = self._load_profiles()
        profile = {
            "hofs": hofs or [],
            "maps": maps or [],
            "vehicles": vehicles or [],
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

    def set_active_profile(self, name: str) -> None:
        _ = self.get_profile(name)
        data = self._load_profiles()
        data["active_profile"] = name
        self._save_profiles(data)

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
            inactive = any(vehicle_dir.rglob("*.bus.inactivate"))
            result.append({"name": vehicle_dir.name, "active": not inactive})
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

    def _set_vehicle_active(self, vehicle_name: str, active: bool) -> int:
        vehicle_dir = self.vehicles_path / vehicle_name
        if not vehicle_dir.exists():
            return 0
        renamed = 0
        if active:
            for bus_inactive in vehicle_dir.rglob("*.bus.inactivate"):
                bus_inactive.rename(bus_inactive.with_suffix(""))
                renamed += 1
        else:
            for bus_file in vehicle_dir.rglob("*.bus"):
                bus_file.rename(bus_file.with_name(f"{bus_file.name}.inactivate"))
                renamed += 1
        return renamed

    def _restore_hof_distribution(self, backup_names: Iterable[str]) -> int:
        copied = 0
        target_vehicle_dirs = [path for path in self.vehicles_path.iterdir() if path.is_dir()] if self.vehicles_path.exists() else []
        for backup_name in backup_names:
            source_hof = self.hof_backup / backup_name
            if not source_hof.exists():
                raise FileNotFoundError(f"HOF backup not found: {backup_name}")
            for destination_dir in target_vehicle_dirs:
                destination_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_hof, destination_dir / backup_name)
                copied += 1
        return copied

    def restore_profile(self, name: str) -> Dict[str, int]:
        profile = self.get_profile(name)
        maps = [str(item) for item in profile.get("maps", [])]
        vehicles = [str(item) for item in profile.get("vehicles", [])]
        hofs = profile.get("hofs", [])

        selected_maps = {item.lower() for item in maps}
        selected_vehicles = {item.lower() for item in vehicles}
        map_toggle_count = 0
        vehicle_toggle_count = 0
        for item in self.list_maps_with_status():
            map_toggle_count += self._set_map_active(str(item["name"]), str(item["name"]).lower() in selected_maps)
        for item in self.list_vehicles_with_status():
            vehicle_toggle_count += self._set_vehicle_active(str(item["name"]), str(item["name"]).lower() in selected_vehicles)

        backup_names = [
            str(item.get("backup_name", "")).strip()
            for item in hofs
            if isinstance(item, dict) and str(item.get("backup_name", "")).strip()
        ]
        if not backup_names:
            backup_names = [path.name for path in sorted(self.hof_backup.glob("*.hof"), key=lambda item: item.name.lower())]
        hof_count = self._restore_hof_distribution(backup_names)
        return {
            "maps": len(maps),
            "vehicles": len(vehicles),
            "map_toggles": map_toggle_count,
            "vehicle_toggles": vehicle_toggle_count,
            "hof_copies": hof_count,
        }

    def restore_active_profile(self) -> Dict[str, int]:
        data = self._load_profiles()
        active_profile = data.get("active_profile")
        if not active_profile:
            raise RuntimeError("No active profile is set")
        return self.restore_profile(str(active_profile))


def _parse_hof_spec(spec: str) -> Dict[str, object]:
    parts = [item.strip() for item in spec.split(":") if item.strip()]
    if not parts:
        raise ValueError("--hof format must include backup_name")
    return {"backup_name": parts[0]}


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


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
    status_var = tk.StringVar(value="Ready")
    backup_progress_var = tk.DoubleVar(value=0)

    tk.Label(root, text="Game Root").grid(row=0, column=0, sticky="w", padx=8, pady=4)
    tk.Entry(root, textvariable=game_root_var, width=70).grid(row=0, column=1, columnspan=6, sticky="we", padx=8, pady=4)
    tk.Button(
        root,
        text="Browse",
        command=lambda: game_root_var.set(filedialog.askdirectory() or game_root_var.get()),
    ).grid(row=0, column=7, sticky="we", padx=8, pady=4)
    tk.Label(root, text="Repo Root").grid(row=1, column=0, sticky="w", padx=8, pady=4)
    tk.Entry(root, textvariable=repo_root_var, width=70).grid(row=1, column=1, columnspan=6, sticky="we", padx=8, pady=4)
    tk.Button(
        root,
        text="Browse",
        command=lambda: repo_root_var.set(filedialog.askdirectory() or repo_root_var.get()),
    ).grid(row=1, column=7, sticky="we", padx=8, pady=4)

    tk.Label(root, text="Profile Name").grid(row=2, column=0, sticky="w", padx=8, pady=4)
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

    tk.Label(maps_frame, text="Maps", bg="white").pack(anchor="w", padx=8, pady=(8, 4))
    maps_listbox = tk.Listbox(maps_frame, selectmode=tk.EXTENDED, exportselection=False, height=14)
    maps_listbox.pack(fill="both", expand=True, padx=8, pady=4)
    tk.Label(vehicles_frame, text="Vehicles", bg="white").pack(anchor="w", padx=8, pady=(8, 4))
    vehicles_listbox = tk.Listbox(vehicles_frame, selectmode=tk.EXTENDED, exportselection=False, height=14)
    vehicles_listbox.pack(fill="both", expand=True, padx=8, pady=4)
    tk.Label(hofs_frame, text="HOFs", bg="white").pack(anchor="w", padx=8, pady=(8, 4))
    hofs_listbox = tk.Listbox(hofs_frame, selectmode=tk.EXTENDED, exportselection=False, height=14)
    hofs_listbox.pack(fill="both", expand=True, padx=8, pady=4)

    output = scrolledtext.ScrolledText(root, width=110, height=20)
    output.grid(row=8, column=0, columnspan=8, sticky="nsew", padx=8, pady=8)

    for column_index in range(8):
        root.grid_columnconfigure(column_index, weight=1 if column_index > 0 else 0)
    root.grid_rowconfigure(6, weight=1)
    root.grid_rowconfigure(8, weight=2)

    map_vehicle_auto_refs: Dict[str, Set[str]] = {}
    map_display_name_lookup: Dict[str, str] = {}
    vehicle_display_name_lookup: Dict[str, str] = {}
    background_backup_process: Dict[str, object] = {"proc": None, "queue": None, "cancelled": False}
    startup_prompt_shown = {"value": False}

    def _manager_from_inputs(validate_game_root: bool = True) -> OmsiAssetManager:
        game_root = Path(game_root_var.get().strip() or ".").expanduser()
        if validate_game_root:
            _validate_game_root(game_root)
        repo_root = Path(repo_root_var.get()).expanduser()
        return OmsiAssetManager(game_root, repo_root)

    def _emit(payload: object) -> None:
        output.insert(tk.END, f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
        output.see(tk.END)

    def _run(action) -> None:
        try:
            result = action()
            _emit({"ok": True, "result": result})
        except Exception as exc:
            _emit({"ok": False, "error": str(exc)})

    def _selected_values(listbox: "tk.Listbox") -> List[str]:
        return [str(listbox.get(index)) for index in listbox.curselection()]

    def _selected_names(listbox: "tk.Listbox", lookup: Optional[Dict[str, str]] = None) -> List[str]:
        values = _selected_values(listbox)
        if not lookup:
            return values
        return [lookup.get(value, value) for value in values]

    def _select_values(listbox: "tk.Listbox", values: Iterable[str], clear_existing: bool = True) -> None:
        if clear_existing:
            listbox.selection_clear(0, tk.END)
        target = {str(value).lower() for value in values}
        for index in range(listbox.size()):
            item = str(listbox.get(index))
            if item.lower() in target:
                listbox.selection_set(index)

    def _fill_listbox(listbox: "tk.Listbox", values: Iterable[str]) -> None:
        listbox.delete(0, tk.END)
        for value in sorted(values, key=lambda item: item.lower()):
            listbox.insert(tk.END, value)

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
        vehicle_display_name_lookup.clear()
        repo_raw = repo_root_var.get().strip()
        if not repo_raw:
            _fill_listbox(maps_listbox, [])
            _fill_listbox(vehicles_listbox, [])
            _fill_listbox(hofs_listbox, [])
            status_var.set("Repo Root is empty")
            if show_prompt and not startup_prompt_shown["value"]:
                startup_prompt_shown["value"] = True
                messagebox.showinfo("首次复制提示", "尚未设置或检测到空的 Repo Root，请设置 Game Root 并点击 Backup All。")
            return

        repo_root = Path(repo_raw).expanduser()
        manager = OmsiAssetManager(Path(game_root_var.get().strip() or ".").expanduser(), repo_root)
        _set_progress(0, 3, "Loading game assets...")

        maps_with_status = manager.list_maps_with_status()
        maps_display = []
        for item in maps_with_status:
            name = str(item["name"])
            active = bool(item["active"])
            display = f"{name} [{'activate' if active else 'inactivate'}]"
            maps_display.append(display)
            map_display_name_lookup[display] = name
        _set_progress(1, 3, "Loaded maps from game root")

        vehicles_with_status = manager.list_vehicles_with_status()
        vehicles_display = []
        for item in vehicles_with_status:
            name = str(item["name"])
            active = bool(item["active"])
            display = f"{name} [{'activate' if active else 'inactivate'}]"
            vehicles_display.append(display)
            vehicle_display_name_lookup[display] = name
        _set_progress(2, 3, "Loaded vehicles from game root")

        hofs = []
        if manager.hof_backup.exists():
            hofs = [path.name for path in manager.hof_backup.glob("*.hof") if path.is_file()]
        _set_progress(3, 3, "Loaded hof backups")

        for map_name in [str(item["name"]) for item in maps_with_status]:
            refs = _collect_map_vehicle_refs(manager.maps_path / map_name)
            map_vehicle_auto_refs[map_name] = refs

        _fill_listbox(maps_listbox, maps_display)
        _fill_listbox(vehicles_listbox, vehicles_display)
        _fill_listbox(hofs_listbox, hofs)

        if not _has_initial_backup(repo_root):
            status_var.set("No initial backup found. Set Game Root then click Backup All.")
            if show_prompt and not startup_prompt_shown["value"]:
                startup_prompt_shown["value"] = True
                messagebox.showinfo("首次复制提示", "检测到尚未进行初次复制，请设置 Game Root 并点击 Backup All。")
        else:
            status_var.set("Backups loaded")

    def _apply_map_vehicle_auto_select(_event=None) -> None:
        selected_maps = _selected_names(maps_listbox, map_display_name_lookup)
        auto_targets: Set[str] = set()
        for map_name in selected_maps:
            auto_targets.update(map_vehicle_auto_refs.get(map_name, set()))
        if not auto_targets:
            return
        matched_display_values: List[str] = []
        for display_value, vehicle_name in vehicle_display_name_lookup.items():
            vehicle_key = vehicle_name.lower()
            if any(target in vehicle_key or vehicle_key in target for target in auto_targets):
                matched_display_values.append(display_value)
        _select_values(vehicles_listbox, matched_display_values, clear_existing=False)

    def _select_all(listbox: "tk.Listbox") -> None:
        listbox.selection_set(0, tk.END)

    def _invert_selection(listbox: "tk.Listbox") -> None:
        selected = set(listbox.curselection())
        for index in range(listbox.size()):
            if index in selected:
                listbox.selection_clear(index)
            else:
                listbox.selection_set(index)

    def _clear_selection(listbox: "tk.Listbox") -> None:
        listbox.selection_clear(0, tk.END)

    def _add_selection_buttons(container: "tk.Frame", listbox: "tk.Listbox") -> None:
        button_bar = tk.Frame(container, bg="white")
        button_bar.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(button_bar, text="Select All", command=lambda: _select_all(listbox)).pack(side="left", padx=2)
        tk.Button(button_bar, text="Invert", command=lambda: _invert_selection(listbox)).pack(side="left", padx=2)
        tk.Button(button_bar, text="Clear", command=lambda: _clear_selection(listbox)).pack(side="left", padx=2)

    _add_selection_buttons(maps_frame, maps_listbox)
    _add_selection_buttons(vehicles_frame, vehicles_listbox)
    _add_selection_buttons(hofs_frame, hofs_listbox)
    maps_listbox.bind("<<ListboxSelect>>", _apply_map_vehicle_auto_select)

    def _start_backup_subprocess() -> None:
        if background_backup_process["proc"] is not None:
            raise RuntimeError("Backup is already running")
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
        backup_progress.configure(maximum=1)
        backup_progress_var.set(0)
        status_var.set("Backup running...")

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
        _poll_backup_events()

    def _poll_backup_events() -> None:
        events = background_backup_process["queue"]
        process = background_backup_process["proc"]
        if events is None or process is None:
            return
        finished = False
        final_result: Optional[Dict[str, int]] = None
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
                except json.JSONDecodeError:
                    _emit({"raw": raw_payload})
                    continue
                if payload.get("event") == "backup_progress":
                    current = int(payload.get("current", 0))
                    total = int(payload.get("total", 1))
                    step = str(payload.get("step", ""))
                    copied = int(payload.get("copied", 0))
                    _set_progress(current, total, f"Backup {step} done: {copied}")
                elif "hof" in payload:
                    final_result = {"hof": int(payload["hof"])}
                    _emit({"ok": True, "result": final_result})
                else:
                    _emit(payload)
            elif event["type"] == "done":
                finished = True
                return_code = int(event["return_code"])
                stderr_text = str(event.get("stderr", ""))
        if not finished:
            root.after(120, _poll_backup_events)
            return

        background_backup_process["proc"] = None
        background_backup_process["queue"] = None

        if background_backup_process.get("cancelled"):
            status_var.set("Backup cancelled")
            messagebox.showinfo("Backup", "备份已中止。")
            _load_backup_data(show_prompt=False)
            return

        if return_code == 0 and final_result is not None:
            status_var.set("Backup completed")
            summary = f"HOF: {final_result['hof']}"
            messagebox.showinfo("Backup completed", summary)
            _load_backup_data(show_prompt=False)
            return

        status_var.set("Backup failed")
        messagebox.showerror("Backup failed", stderr_text or "Backup process exited with failure.")

    def _cancel_backup() -> None:
        process = background_backup_process["proc"]
        if process is None:
            return
        background_backup_process["cancelled"] = True
        process.terminate()

    def _save_profile() -> None:
        name = profile_name_var.get().strip()
        if not name:
            raise ValueError("Profile name is required")
        maps = _selected_names(maps_listbox, map_display_name_lookup)
        vehicles = _selected_names(vehicles_listbox, vehicle_display_name_lookup)
        hofs = [
            {
                "backup_name": hof_name,
            }
            for hof_name in _selected_values(hofs_listbox)
        ]
        _manager_from_inputs().save_profile(name=name, maps=maps, vehicles=vehicles, hofs=hofs)
        return None

    def _profile_get_and_apply() -> Dict[str, object]:
        profile = _manager_from_inputs().get_profile(profile_name_var.get().strip())
        maps = [str(item) for item in profile.get("maps", [])]
        vehicles = [str(item) for item in profile.get("vehicles", [])]
        hofs = [str(item.get("backup_name", "")) for item in profile.get("hofs", []) if isinstance(item, dict)]
        map_displays = [display for display, name in map_display_name_lookup.items() if name in maps]
        vehicle_displays = [display for display, name in vehicle_display_name_lookup.items() if name in vehicles]
        _select_values(maps_listbox, map_displays)
        _select_values(vehicles_listbox, vehicle_displays)
        _select_values(hofs_listbox, hofs)
        _apply_map_vehicle_auto_select()
        return profile

    def _save_defaults() -> None:
        game_root = Path(game_root_var.get()).expanduser()
        _validate_game_root(game_root)
        repo_root = Path(repo_root_var.get()).expanduser()
        _save_config(config_path, str(game_root), str(repo_root))
        return None

    tk.Button(action_bar, text="Backup All", command=lambda: _run(_start_backup_subprocess)).pack(side="left", padx=4)
    tk.Button(action_bar, text="Stop Backup", command=_cancel_backup).pack(side="left", padx=4)
    tk.Button(action_bar, text="Refresh Backups", command=lambda: _run(lambda: _load_backup_data(show_prompt=False))).pack(side="left", padx=4)
    tk.Button(action_bar, text="Profile Save", command=lambda: _run(_save_profile)).pack(side="left", padx=4)
    tk.Button(action_bar, text="Profile List", command=lambda: _run(lambda: _manager_from_inputs().list_profiles())).pack(side="left", padx=4)
    tk.Button(action_bar, text="Profile Get", command=lambda: _run(_profile_get_and_apply)).pack(side="left", padx=4)
    tk.Button(action_bar, text="Profile Activate", command=lambda: _run(lambda: _manager_from_inputs().set_active_profile(profile_name_var.get().strip()))).pack(side="left", padx=4)
    tk.Button(action_bar, text="Save Defaults", command=lambda: _run(_save_defaults)).pack(side="left", padx=4)
    tk.Button(action_bar, text="Restore Profile", command=lambda: _run(lambda: _manager_from_inputs().restore_profile(profile_name_var.get().strip()))).pack(side="left", padx=4)
    tk.Button(action_bar, text="Restore Active", command=lambda: _run(lambda: _manager_from_inputs().restore_active_profile())).pack(side="left", padx=4)
    tk.Button(action_bar, text="Clear Output", command=lambda: output.delete("1.0", tk.END)).pack(side="left", padx=4)

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
    sub.add_parser("restore-active")
    sub.add_parser("gui")

    restore_profile = sub.add_parser("restore-profile")
    restore_profile.add_argument("name")

    profile_save = sub.add_parser("profile-save")
    profile_save.add_argument("name")
    profile_save.add_argument("--map", action="append", default=[])
    profile_save.add_argument("--vehicle", action="append", default=[])
    profile_save.add_argument(
        "--hof",
        action="append",
        default=[],
        help="backup_name",
    )

    profile_get = sub.add_parser("profile-get")
    profile_get.add_argument("name")

    sub.add_parser("profile-list")

    profile_activate = sub.add_parser("profile-activate")
    profile_activate.add_argument("name")
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
    if args.command == "restore-active":
        print(json.dumps(manager.restore_active_profile(), ensure_ascii=False))
        return 0
    if args.command == "restore-profile":
        print(json.dumps(manager.restore_profile(args.name), ensure_ascii=False))
        return 0
    if args.command == "profile-save":
        hofs = [_parse_hof_spec(spec) for spec in args.hof]
        manager.save_profile(
            name=args.name,
            hofs=hofs,
            maps=args.map,
            vehicles=args.vehicle,
        )
        return 0
    if args.command == "profile-get":
        print(json.dumps(manager.get_profile(args.name), indent=2, ensure_ascii=False))
        return 0
    if args.command == "profile-list":
        print(json.dumps(manager.list_profiles(), ensure_ascii=False))
        return 0
    if args.command == "profile-activate":
        manager.set_active_profile(args.name)
        return 0
    parser.error(f"Unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
