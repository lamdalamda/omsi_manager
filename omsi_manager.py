import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def _file_md5(file_path: Path) -> str:
    digest = hashlib.md5()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4096), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


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


class OmsiAssetManager:
    def __init__(self, game_root: Path, repo_root: Path):
        self.game_root = game_root
        self.repo_root = repo_root
        self.backup_root = repo_root / "backups"
        self.hof_backup = self.backup_root / "hof"
        self.map_backup = self.backup_root / "maps"
        self.vehicle_backup = self.backup_root / "vehicles"
        self.asset_backup = self.backup_root / "assets"
        self.profiles_path = repo_root / "profiles.json"

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

    def backup_maps(self) -> int:
        self.map_backup.mkdir(parents=True, exist_ok=True)
        count = 0
        if not self.maps_path.exists():
            return count
        for map_dir in self.maps_path.iterdir():
            if not map_dir.is_dir():
                continue
            _copytree(map_dir, self.map_backup / map_dir.name)
            count += 1
        return count

    def backup_vehicles(self) -> int:
        self.vehicle_backup.mkdir(parents=True, exist_ok=True)
        count = 0
        if not self.vehicles_path.exists():
            return count
        for vehicle_dir in self.vehicles_path.iterdir():
            if not vehicle_dir.is_dir():
                continue
            _copytree(vehicle_dir, self.vehicle_backup / vehicle_dir.name)
            count += 1
        return count

    def backup_map_referenced_assets(self) -> int:
        self.asset_backup.mkdir(parents=True, exist_ok=True)
        copied = 0
        if not self.maps_path.exists():
            return copied
        for ailist_path in self.maps_path.rglob("ailists.cfg"):
            refs = _parse_ailist_refs(ailist_path.read_text(encoding="utf-8", errors="ignore"))
            for rel_ref in refs:
                source = self._resolve_game_path(rel_ref)
                if not source.exists() or not source.is_file():
                    continue
                target = self.asset_backup / rel_ref
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and _file_md5(target) == _file_md5(source):
                    continue
                shutil.copy2(source, target)
                copied += 1
        return copied

    def backup_all(self) -> Dict[str, int]:
        return {
            "hof": self.backup_hofs(),
            "maps": self.backup_maps(),
            "vehicles": self.backup_vehicles(),
            "map_assets": self.backup_map_referenced_assets(),
        }

    def _load_profiles(self) -> Dict[str, object]:
        if not self.profiles_path.exists():
            return {"active_profile": None, "profiles": {}}
        return json.loads(self.profiles_path.read_text(encoding="utf-8"))

    def _save_profiles(self, data: Dict[str, object]) -> None:
        self.profiles_path.parent.mkdir(parents=True, exist_ok=True)
        self.profiles_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _resolve_game_path(self, rel_ref: Path) -> Path:
        parts = list(rel_ref.parts)
        if not parts:
            return self.game_root
        first = parts[0].lower()
        remaining = Path(*parts[1:]) if len(parts) > 1 else Path()
        if first == "vehicles":
            return self.vehicles_path / remaining
        if first == "maps":
            return self.maps_path / remaining
        if first == "trains":
            return (self.game_root / "trains") / remaining
        return self.game_root / rel_ref

    def save_profile(
        self,
        name: str,
        hofs: Optional[List[Dict[str, object]]] = None,
        maps: Optional[List[str]] = None,
        vehicles: Optional[List[str]] = None,
        auto_include_map_ailist_assets: bool = True,
    ) -> None:
        data = self._load_profiles()
        profile = {
            "hofs": hofs or [],
            "maps": maps or [],
            "vehicles": vehicles or [],
            "auto_include_map_ailist_assets": auto_include_map_ailist_assets,
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

    def _restore_map(self, map_name: str) -> None:
        source = self.map_backup / map_name
        if not source.exists():
            raise FileNotFoundError(f"Map backup not found: {map_name}")
        destination = self.maps_path / map_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copytree(source, destination)

    def _restore_vehicle(self, vehicle_name: str) -> None:
        source = self.vehicle_backup / vehicle_name
        if not source.exists():
            raise FileNotFoundError(f"Vehicle backup not found: {vehicle_name}")
        destination = self.vehicles_path / vehicle_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copytree(source, destination)

    def _restore_map_ailist_assets(self, map_name: str) -> int:
        map_dir = self.maps_path / map_name
        copied = 0
        for ailist_path in map_dir.rglob("ailists.cfg"):
            refs = _parse_ailist_refs(ailist_path.read_text(encoding="utf-8", errors="ignore"))
            for rel_ref in refs:
                source = self.asset_backup / rel_ref
                if not source.exists():
                    continue
                destination = self._resolve_game_path(rel_ref)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied += 1
        return copied

    def _restore_hof_distribution(self, hofs: Iterable[Dict[str, object]]) -> int:
        copied = 0
        for hof in hofs:
            backup_name = str(hof["backup_name"])
            source_hof = self.hof_backup / backup_name
            if not source_hof.exists():
                raise FileNotFoundError(f"HOF backup not found: {backup_name}")
            deploy_name = str(hof.get("deploy_name") or backup_name)
            target_vehicle_dirs = [str(item) for item in hof.get("target_vehicle_dirs", [])]
            for vehicle_dir_name in target_vehicle_dirs:
                destination_dir = self.vehicles_path / vehicle_dir_name
                destination_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_hof, destination_dir / deploy_name)
                copied += 1
        return copied

    def restore_profile(self, name: str) -> Dict[str, int]:
        profile = self.get_profile(name)
        maps = [str(item) for item in profile.get("maps", [])]
        vehicles = [str(item) for item in profile.get("vehicles", [])]
        hofs = profile.get("hofs", [])
        auto_assets = bool(profile.get("auto_include_map_ailist_assets", True))

        for vehicle in vehicles:
            self._restore_vehicle(vehicle)
        for map_name in maps:
            self._restore_map(map_name)

        hof_count = self._restore_hof_distribution(hofs)
        asset_count = 0
        if auto_assets:
            for map_name in maps:
                asset_count += self._restore_map_ailist_assets(map_name)
        return {
            "maps": len(maps),
            "vehicles": len(vehicles),
            "hof_copies": hof_count,
            "map_assets": asset_count,
        }

    def restore_active_profile(self) -> Dict[str, int]:
        data = self._load_profiles()
        active_profile = data.get("active_profile")
        if not active_profile:
            raise RuntimeError("No active profile is set")
        return self.restore_profile(str(active_profile))


def _parse_hof_spec(spec: str) -> Dict[str, object]:
    parts = spec.split(":")
    if len(parts) < 2:
        raise ValueError("--hof format must be backup_name:vehicle1,vehicle2[:deploy_name]")
    backup_name = parts[0]
    target_vehicle_dirs = [item for item in parts[1].split(",") if item]
    deploy_name = parts[2] if len(parts) > 2 and parts[2] else backup_name
    return {
        "backup_name": backup_name,
        "target_vehicle_dirs": target_vehicle_dirs,
        "deploy_name": deploy_name,
    }


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def launch_gui(default_game_root: str = ".", default_repo_root: str = ".") -> int:
    try:
        import tkinter as tk
        from tkinter import scrolledtext
    except Exception as exc:
        raise RuntimeError("Tkinter is required for GUI mode") from exc

    root = tk.Tk()
    root.title("OMSI Manager")
    root.geometry("900x700")

    game_root_var = tk.StringVar(value=default_game_root)
    repo_root_var = tk.StringVar(value=default_repo_root)
    profile_name_var = tk.StringVar()

    tk.Label(root, text="Game Root").grid(row=0, column=0, sticky="w", padx=8, pady=4)
    tk.Entry(root, textvariable=game_root_var, width=90).grid(row=0, column=1, columnspan=5, sticky="we", padx=8, pady=4)
    tk.Label(root, text="Repo Root").grid(row=1, column=0, sticky="w", padx=8, pady=4)
    tk.Entry(root, textvariable=repo_root_var, width=90).grid(row=1, column=1, columnspan=5, sticky="we", padx=8, pady=4)

    tk.Label(root, text="Profile Name").grid(row=2, column=0, sticky="w", padx=8, pady=4)
    tk.Entry(root, textvariable=profile_name_var, width=40).grid(row=2, column=1, sticky="w", padx=8, pady=4)

    tk.Label(root, text="Maps (comma-separated)").grid(row=3, column=0, sticky="w", padx=8, pady=4)
    maps_entry = tk.Entry(root, width=80)
    maps_entry.grid(row=3, column=1, columnspan=5, sticky="we", padx=8, pady=4)

    tk.Label(root, text="Vehicles (comma-separated)").grid(row=4, column=0, sticky="w", padx=8, pady=4)
    vehicles_entry = tk.Entry(root, width=80)
    vehicles_entry.grid(row=4, column=1, columnspan=5, sticky="we", padx=8, pady=4)

    tk.Label(root, text="HOF specs (one per line)").grid(row=5, column=0, sticky="nw", padx=8, pady=4)
    hofs_text = scrolledtext.ScrolledText(root, width=80, height=6)
    hofs_text.grid(row=5, column=1, columnspan=5, sticky="we", padx=8, pady=4)

    output = scrolledtext.ScrolledText(root, width=110, height=20)
    output.grid(row=8, column=0, columnspan=6, sticky="nsew", padx=8, pady=8)

    for column_index in range(6):
        root.grid_columnconfigure(column_index, weight=1 if column_index > 0 else 0)
    root.grid_rowconfigure(8, weight=1)

    def _manager_from_inputs() -> OmsiAssetManager:
        return OmsiAssetManager(Path(game_root_var.get()), Path(repo_root_var.get()))

    def _emit(payload: object) -> None:
        output.insert(tk.END, f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n")
        output.see(tk.END)

    def _run(action) -> None:
        try:
            result = action()
            _emit({"ok": True, "result": result})
        except Exception as exc:
            _emit({"ok": False, "error": str(exc)})

    def _save_profile() -> None:
        name = profile_name_var.get().strip()
        if not name:
            raise ValueError("Profile name is required")
        maps = _split_csv(maps_entry.get())
        vehicles = _split_csv(vehicles_entry.get())
        hofs = []
        for line in hofs_text.get("1.0", tk.END).splitlines():
            spec = line.strip()
            if spec:
                hofs.append(_parse_hof_spec(spec))
        _manager_from_inputs().save_profile(name=name, maps=maps, vehicles=vehicles, hofs=hofs)
        return None

    tk.Button(root, text="Backup All", command=lambda: _run(lambda: _manager_from_inputs().backup_all())).grid(row=6, column=0, padx=8, pady=6, sticky="we")
    tk.Button(root, text="Profile Save", command=lambda: _run(_save_profile)).grid(row=6, column=1, padx=8, pady=6, sticky="we")
    tk.Button(root, text="Profile List", command=lambda: _run(lambda: _manager_from_inputs().list_profiles())).grid(row=6, column=2, padx=8, pady=6, sticky="we")
    tk.Button(root, text="Profile Get", command=lambda: _run(lambda: _manager_from_inputs().get_profile(profile_name_var.get().strip()))).grid(row=6, column=3, padx=8, pady=6, sticky="we")
    tk.Button(root, text="Profile Activate", command=lambda: _run(lambda: _manager_from_inputs().set_active_profile(profile_name_var.get().strip()))).grid(row=6, column=4, padx=8, pady=6, sticky="we")
    tk.Button(root, text="Restore Profile", command=lambda: _run(lambda: _manager_from_inputs().restore_profile(profile_name_var.get().strip()))).grid(row=7, column=0, padx=8, pady=6, sticky="we")
    tk.Button(root, text="Restore Active", command=lambda: _run(lambda: _manager_from_inputs().restore_active_profile())).grid(row=7, column=1, padx=8, pady=6, sticky="we")
    tk.Button(root, text="Clear Output", command=lambda: output.delete("1.0", tk.END)).grid(row=7, column=2, padx=8, pady=6, sticky="we")

    root.mainloop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OMSI asset backup/profile manager")
    parser.add_argument("--game-root", default=".", help="OMSI root path (contains Vehicles/Maps)")
    parser.add_argument("--repo-root", default=".", help="Repository root path for backups/config")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("backup-all")
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
        help="backup_name:vehicle1,vehicle2[:deploy_name]",
    )
    profile_save.add_argument("--no-ailist-assets", action="store_true")

    profile_get = sub.add_parser("profile-get")
    profile_get.add_argument("name")

    sub.add_parser("profile-list")

    profile_activate = sub.add_parser("profile-activate")
    profile_activate.add_argument("name")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "gui":
        return launch_gui(default_game_root=args.game_root, default_repo_root=args.repo_root)

    manager = OmsiAssetManager(Path(args.game_root), Path(args.repo_root))

    if args.command == "backup-all":
        print(json.dumps(manager.backup_all(), ensure_ascii=False))
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
            auto_include_map_ailist_assets=not args.no_ailist_assets,
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
