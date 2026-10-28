import sys

from usageledger.cli import main


def test_missing_warcio_gives_a_clean_message(monkeypatch, tmp_path, capsys):
    for mod in ("warcio", "warcio.statusandheaders", "warcio.warcwriter", "warcio.archiveiterator"):
        monkeypatch.setitem(sys.modules, mod, None)  # makes "import warcio" fail
    assert main(["demo", str(tmp_path / "demo")]) == 1
    assert "pip install 'usageledger[warc]'" in capsys.readouterr().err
