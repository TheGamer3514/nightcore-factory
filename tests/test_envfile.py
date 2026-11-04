"""The .env loader and boolean flags."""

import envfile


def test_flag_defaults_when_unset_or_blank(monkeypatch):
    monkeypatch.delenv("X_FLAG", raising=False)
    assert envfile.flag("X_FLAG", True) is True
    monkeypatch.setenv("X_FLAG", "  ")
    assert envfile.flag("X_FLAG", True) is True
    assert envfile.flag("X_FLAG", False) is False


def test_flag_off_values(monkeypatch):
    for value in ("0", "false", "No", "OFF"):
        monkeypatch.setenv("X_FLAG", value)
        assert envfile.flag("X_FLAG", True) is False
    monkeypatch.setenv("X_FLAG", "1")
    assert envfile.flag("X_FLAG", False) is True


def test_load_reads_pairs_and_real_env_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        '# comment\nA_KEY=one\nexport B_KEY="two words"\nC_KEY=file\n',
        encoding="utf-8")
    monkeypatch.delenv("A_KEY", raising=False)
    monkeypatch.delenv("B_KEY", raising=False)
    monkeypatch.setenv("C_KEY", "real")
    envfile.load(env)
    import os
    assert os.environ["A_KEY"] == "one"
    assert os.environ["B_KEY"] == "two words"
    assert os.environ["C_KEY"] == "real"
    monkeypatch.delenv("A_KEY")
    monkeypatch.delenv("B_KEY")
