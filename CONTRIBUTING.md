# Contributing

Thanks for your interest. Issues and pull requests are welcome.

## Most useful right now

- **Live test results.** Ran a stage of [`TESTING.md`](TESTING.md) against your own model, PRTG or Slack?
  Open an issue with the stage, model or system version, and what passed or failed.
- **Eval cases** from real, anonymised incidents in [`bya-runtime/evals/cases.json`](bya-runtime/evals/cases.json).
- **Corrections to the guide**: something wrong, outdated or unclear in [`GUIDE.md`](GUIDE.md).

## Before opening a pull request

```sh
python -m unittest discover -s examples
cd bya-runtime
python -m unittest discover -s tests
python evals/run_evals.py
```

All three must pass. CI runs them on Python 3.11–3.13.

## Ground rules

- Standard library only in `examples/` and the base runtime. Optional dependencies go in a separate requirements file.
- Keep changes small and focused; one concern per pull request.
- Never commit credentials, real hostnames or customer data. Use the sample SSOT and runbooks.
- New behaviour in `bya-runtime` needs a test. A new model-facing behaviour needs an eval case.
- Security problems: see [`SECURITY.md`](SECURITY.md), not a public issue.

By contributing, you agree your contribution is licensed under the [Apache License 2.0](LICENSE).
