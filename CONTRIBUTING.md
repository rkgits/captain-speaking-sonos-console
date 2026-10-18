# Contributing to Captain Speaking

Thanks for helping! This is a small project, so contributions of any size are welcome: bug reports, fixes, ideas, docs, or code.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md). By contributing code you agree that it is released under the project's [MIT License](LICENSE).

## Ways to help

- **Report a bug** or **suggest a feature** using the [issue templates](https://github.com/rkgits/captain-speaking-sonos-console/issues/new/choose).
- **Test on your speakers** and tell us which models work. The compatibility table in the README comes from real reports.
- **Port it to Linux or Windows.** Discovery and the scripts currently use macOS tools. See the README section "Is my setup supported?".
- Pick something from the [project board](https://github.com/users/rkgits/projects/2) or an issue labeled `good first issue` or `help wanted`.

For anything bigger than a small fix, please **open an issue first** so we can agree on the approach before you spend time on it.

## Security problems

Please do not report these in public. See [SECURITY.md](SECURITY.md).

## Setting up

You need macOS and Python 3.9 or newer.

```bash
git clone https://github.com/rkgits/captain-speaking-sonos-console.git
cd captain-speaking-sonos-console
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

### Work on the interface without speakers

```bash
.venv/bin/python tests/mock_server.py      # then open http://localhost:8791
```

This runs the real app against pretend speakers, so you don't need any Sonos hardware.

### Run the tests

```bash
.venv/bin/pytest tests/test_track_change.py tests/test_discovery.py -v      # unit tests, nothing needs to be running
./start.command                                                              # in another terminal, for the API tests
TEST_URL=http://localhost:8787 .venv/bin/pytest tests/test_api.py -v
```

## Making a change

1. **Fork** the repository and create a branch from `main`, for example `fix-volume-readout`.
2. Make your change. Keep it focused: one fix or feature per pull request.
3. **Add or update tests** when you change behavior (`tests/`).
4. Run the tests and try the change in a browser, at desktop and phone widths.
5. Update the README if you changed how something works.
6. Open a pull request and fill in the template.

### Style

- Match the surrounding code: naming, comment density and structure. The app is a single `app.py` plus `static/index.html` on purpose, so please keep it simple.
- No new dependencies unless there is a strong reason. The app needs only `fastapi`, `uvicorn` and `httpx`.
- **Keep the vintage look**: brass, wood and engraved details. Make controls and non-controls look different (buttons are raised, labels are flat).
- **Accessibility matters**: keyboard access, visible focus, text alternatives, and respect for `prefers-reduced-motion`.
- Write commit messages that say what changed and why, for example `fix: Next button left artwork stale`.

### Keep your private details out of commits

- **Use your GitHub no-reply email** so your computer name and real email don't end up in public history. Set it once:
  ```bash
  git config --global user.name "Your Name"
  git config --global user.email "<id>+<username>@users.noreply.github.com"
  ```
  You can find yours under GitHub Settings, Emails.
- Don't paste real IP addresses, speaker room names or MAC addresses into issues, pull requests or tests. Use made-up ones such as `192.168.1.20` and `Kitchen`.

## Review

The maintainer, [@rkgits](https://github.com/rkgits), reviews pull requests when time allows, usually within a week. Small, focused changes with tests are merged fastest.

Thank you for making Captain Speaking better!
