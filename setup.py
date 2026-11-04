"""Ship the canonical CAD adapter with wheels as well as the installable skill."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithCadAdapter(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent / ".agents/skills/forma-hardware/scripts/cad.py"
        target = Path(self.build_lib) / "forma_core/_resources/cad.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


setup(cmdclass={"build_py": BuildWithCadAdapter})
