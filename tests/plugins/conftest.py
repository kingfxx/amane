from pathlib import Path

import pytest

from amane.plugin import ImportPlugin, SnapshotImportProvider
from amane.plugins.packaging import load_plugin


@pytest.fixture
def nfo_provider() -> SnapshotImportProvider:
    loaded = load_plugin("local.nfo", Path(__file__).resolve().parents[2] / "extensions/nfo-import")
    assert isinstance(loaded.plugin, ImportPlugin)
    provider = loaded.plugin.build_importer(loaded.plugin.configuration_model()())
    assert isinstance(provider, SnapshotImportProvider)
    return provider
