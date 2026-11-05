"""生成独立扩展安装包, 不修改运行中的 Amane 数据目录."""

import shutil
import sys
import zipfile
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    target = root / "dist" / "extensions"
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target / "local.nfo.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(root / "extensions" / "nfo-import" / "plugin.py", "plugin.py")
    shutil.copyfile(root / "extensions" / "115-playback" / "amane-115.user.js", target / "amane-115.user.js")
    sys.stdout.write(f"{target}\n")


if __name__ == "__main__":
    main()
