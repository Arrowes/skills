"""Install a small native-command hook; never modify callback authentication."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent


def install(bridge: Path, apply: bool = False) -> dict:
    cli = bridge / "dist" / "cli.js"
    source = cli.read_text(encoding="utf-8")
    marker = "// body-fitness-coach native meal adapter v1"
    adapter = (ROOT / "bridge_meals.mjs").as_uri()
    hook = f'''{marker}
  "/bodycoach": async (args, ctx) => {{
    const {{ handleBodyCoach }} = await import({json.dumps(adapter)});
    return handleBodyCoach(args, ctx);
  }},'''
    if marker in source:
        if hook not in source:
            raise RuntimeError("Installed meal hook differs; inspect it before replacing")
        return {"state": "installed", "path": str(cli)}
    anchor = "var handlers = {"
    if source.count(anchor) != 1 or "const formValue = raw?.action?.form_value;" not in source:
        raise RuntimeError("Unsupported bridge build; no files changed")
    updated = source.replace(anchor, anchor + "\n  " + hook, 1)
    result = {"state": "planned", "path": str(cli), "adapter": str(ROOT / "bridge_meals.mjs")}
    if apply:
        backup_dir = ROOT.parent / ".private" / "bridge-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / ("cli-" + hashlib.sha256(source.encode()).hexdigest()[:16] + ".js")
        if not backup.exists():
            backup.write_text(source, encoding="utf-8")
        candidate = cli.with_suffix(".bodycoach-check.mjs")
        try:
            candidate.write_text(updated, encoding="utf-8")
            subprocess.run(["node", "--check", str(candidate)], check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            os.replace(candidate, cli)
        finally:
            candidate.unlink(missing_ok=True)
        result.update(state="installed", backup=str(backup), restart_required=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-dir", type=Path, default=Path(os.environ["APPDATA"]) / "npm/node_modules/lark-channel-bridge")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--owner-id", help="Restrict food submissions to this user; saved only in the private DB")
    args = parser.parse_args()
    result = install(args.bridge_dir, args.apply)
    if args.owner_id:
        if not args.apply or not args.owner_id.startswith("ou_"):
            raise ValueError("--owner-id requires --apply and a user open_id")
        import coach
        config = coach.load_config()
        config["meal_owner_id"] = args.owner_id
        coach.save_config(config)
    print(json.dumps(result, ensure_ascii=False))
