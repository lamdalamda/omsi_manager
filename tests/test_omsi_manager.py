from pathlib import Path
import unittest
from unittest import mock

from omsi_manager import (
    OmsiAssetManager,
    _load_config,
    _save_config,
    _validate_game_root,
    _vehicle_names_from_ailist_content,
    build_parser,
    main,
)


class OmsiManagerTests(unittest.TestCase):
    def test_config_round_trip(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.txt"
            _save_config(config_path, "C:/OMSI", "D:/omsi_manager_repo")
            loaded = _load_config(config_path)
            self.assertEqual(loaded["game_root"], "C:/OMSI")
            self.assertEqual(loaded["repo_root"], "D:/omsi_manager_repo")

    def test_validate_game_root_requires_omsi_exe(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            game_root = Path(temp) / "game"
            game_root.mkdir(parents=True)
            with self.assertRaises(ValueError):
                _validate_game_root(game_root)
            (game_root / "omsi.exe").write_text("", encoding="utf-8")
            _validate_game_root(game_root)

    def test_main_without_args_enters_gui_mode(self):
        with mock.patch("omsi_manager.launch_gui", return_value=0) as launch_mock:
            with mock.patch("sys.argv", ["omsi_manager.py"]):
                exit_code = main()
        self.assertEqual(exit_code, 0)
        launch_mock.assert_called_once()

    def test_parser_supports_gui_command(self):
        parser = build_parser()
        args = parser.parse_args(["gui"])
        self.assertEqual(args.command, "gui")

    def test_parser_supports_backup_all_progress_flag(self):
        parser = build_parser()
        args = parser.parse_args(["backup-all", "--progress-json-lines"])
        self.assertEqual(args.command, "backup-all")
        self.assertTrue(args.progress_json_lines)

    def test_backup_hof_uses_prefix_for_same_name_different_content(self):
        with self.subTest("setup and backup"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temp:
                root = Path(temp) / "game"
                repo = Path(temp) / "repo"
                (root / "vehicles" / "A").mkdir(parents=True)
                (root / "vehicles" / "B").mkdir(parents=True)
                (root / "vehicles" / "A" / "shared.hof").write_text("one", encoding="utf-8")
                (root / "vehicles" / "B" / "shared.hof").write_text("two", encoding="utf-8")

                manager = OmsiAssetManager(root, repo)
                copied = manager.backup_hofs()

                self.assertEqual(copied, 2)
                hof_names = sorted(path.name for path in (repo / "backups" / "hof").glob("*.hof"))
                self.assertIn("shared.hof", hof_names)
                self.assertEqual(len(hof_names), 2)
                self.assertTrue(any(name.endswith("_shared.hof") for name in hof_names))

    def test_restore_profile_toggles_map_vehicle_and_copies_hofs(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            game = Path(temp) / "game"
            repo = Path(temp) / "repo"

            (game / "vehicles" / "YC_Masterdeck").mkdir(parents=True)
            (game / "vehicles" / "MAN_NL202").mkdir(parents=True)
            (game / "maps" / "Yorkshire").mkdir(parents=True)
            (game / "maps" / "Grundorf").mkdir(parents=True)

            (game / "vehicles" / "YC_Masterdeck" / "YCV_Streetdeck.bus").write_text("bus", encoding="utf-8")
            (game / "vehicles" / "MAN_NL202" / "MAN_NL202.bus").write_text("bus", encoding="utf-8")
            (game / "maps" / "Yorkshire" / "global.cfg").write_text("global", encoding="utf-8")
            (game / "maps" / "Yorkshire" / "ailists.cfg").write_text("ailist", encoding="utf-8")
            (game / "maps" / "Grundorf" / "global.cfg").write_text("global", encoding="utf-8")

            manager = OmsiAssetManager(game, repo)
            (repo / "backups" / "hof").mkdir(parents=True, exist_ok=True)
            (repo / "backups" / "hof" / "shared.hof").write_text("hof-content", encoding="utf-8")

            manager.save_profile(
                name="yorkshire",
                maps=["Yorkshire"],
                vehicles=["YC_Masterdeck"],
                hofs=[
                    {
                        "backup_name": "shared.hof",
                    }
                ],
            )

            result = manager.restore_profile("yorkshire")

            self.assertEqual(result["maps"], 1)
            self.assertEqual(result["vehicles"], 1)
            self.assertGreaterEqual(result["map_toggles"], 1)
            self.assertGreaterEqual(result["vehicle_toggles"], 1)
            self.assertEqual(result["hof_copies"], 2)
            self.assertTrue((game / "maps" / "Yorkshire" / "ailists.cfg").exists())
            self.assertTrue((game / "maps" / "Grundorf" / "global.cfg.inactivate").exists())
            self.assertTrue((game / "vehicles" / "YC_Masterdeck" / "YCV_Streetdeck.bus").exists())
            self.assertTrue((game / "vehicles" / "MAN_NL202" / "MAN_NL202.bus.inactivate").exists())
            self.assertTrue((game / "vehicles" / "YC_Masterdeck" / "shared.hof").exists())
            self.assertTrue((game / "vehicles" / "MAN_NL202" / "shared.hof").exists())

    def test_profile_can_store_multiple_maps_vehicles_and_hofs(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            manager = OmsiAssetManager(Path(temp) / "game", Path(temp) / "repo")
            manager.save_profile(
                name="multi",
                maps=["Yorkshire", "Grundorf"],
                vehicles=["YC_Masterdeck", "MAN_NL202"],
                hofs=[
                    {
                        "backup_name": "one.hof",
                    },
                    {
                        "backup_name": "two.hof",
                    },
                ],
            )
            profile = manager.get_profile("multi")
            self.assertEqual(len(profile["maps"]), 2)
            self.assertEqual(len(profile["vehicles"]), 2)
            self.assertEqual(len(profile["hofs"]), 2)

    def test_vehicle_names_from_ailist_content_extracts_vehicle_refs(self):
        content = (
            "[aigroup_2]\n"
            "Vehicles\\YC_AI\\WH_UK_AI\\_Road\\TX4.ovh\t100\n"
            "Vehicles\\MAN_NL202\\model\\nl202.bus 1\n"
            "[end]\n"
        )
        names = _vehicle_names_from_ailist_content(content)
        self.assertIn("YC_AI", names)
        self.assertIn("MAN_NL202", names)
        self.assertIn("TX4", names)
        self.assertIn("nl202", names)

    def test_backup_all_reports_progress(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temp:
            game = Path(temp) / "game"
            repo = Path(temp) / "repo"
            game.mkdir(parents=True)
            (game / "omsi.exe").write_text("", encoding="utf-8")
            (game / "maps").mkdir(parents=True)
            (game / "vehicles").mkdir(parents=True)
            manager = OmsiAssetManager(game, repo)
            progress = []

            result = manager.backup_all(progress_callback=lambda step, current, total, copied: progress.append((step, current, total, copied)))

            self.assertEqual(set(result.keys()), {"hof"})
            self.assertEqual(len(progress), 1)
            self.assertEqual(progress[-1][1], 1)
            self.assertEqual(progress[-1][2], 1)


if __name__ == "__main__":
    unittest.main()
