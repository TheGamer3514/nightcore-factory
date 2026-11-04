# Contributing

Issues and pull requests are welcome.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest            # fast: no network, no ffmpeg
python -m pytest -m slow    # also renders real video (needs ffmpeg)
```

- Keep the default test run offline and ffmpeg-free; mark anything that needs
  either with `@pytest.mark.slow`.
- Never commit anything from `state/` — it holds OAuth credentials and the
  upload ledger. `.gitignore` covers it; double-check `git status` anyway.
- Match the surrounding style: comments explain *why*, not *what*.
