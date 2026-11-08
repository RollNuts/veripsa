# Try the offline evaluator on a public repository

See a real report before giving the evaluator your own checkout. This example
uses Flask's public source and Git history. It runs locally with Git and Python
3.12; no GitHub App, account token, database, or package installation is needed.

The project-hosted Veripsa GitHub App remains suspended. This is an offline
example, not an active hosted-service demo.

## Reproduce the example

Run these commands in an empty working directory using a POSIX shell:

```sh
git clone https://github.com/RollNuts/veripsa.git
git -C veripsa fetch origin fcecb9c678e2b5ab75523177547b8ec8c796a66d
git -C veripsa checkout fcecb9c678e2b5ab75523177547b8ec8c796a66d
git clone https://github.com/pallets/flask.git
git -C flask checkout 49943b5a94413a530761350f04e896f76e8d620e
python3.12 -m venv .venv
. .venv/bin/activate
PYTHONHASHSEED=0 python veripsa/evaluate.py flask --out flask-report.md
```

Use a full clone: a shallow clone changes the history being measured. The two
commit IDs pin the evaluator and input. `PYTHONHASHSEED=0` fixes Python's hash
seed because the evaluator samples controls from sets; without it, repeated
processes can choose different random-control pairs.

The clones contact GitHub. After cloning, the evaluation reads local source to
extract structure and uses local Git history; it does not upload source or call
the hosted service. The Markdown report contains the checkout's directory name
and aggregate measurements. Console diagnostics can include its local path, so
review logs before sharing them.

## Observed result

Measured on **2026-11-08** with **Python 3.12.13**, a fresh virtual environment,
no installed third-party packages, and the pinned commits above:

| Measurement | Result |
| --- | ---: |
| In-scope files | 108 |
| Historical commits touching in-scope files | 822 |
| Structurally coupled file pairs with history | 479 |
| Cross-directory coupled pairs | 356 |
| Ever-co-change rate, coupled pairs vs sampled random pairs | 4.3× |
| Ever-co-change rate, cross-directory coupled pairs vs cross-directory controls | 3.6× |

The run exited `0` with an applicable result. A second run in the same clean
environment produced the same report.

These are measurements of one public checkout, not a general accuracy score or
evidence that particular PRs are safe to merge. The engine excludes merge
commits and commits touching more than 30 in-scope files, then compares pairs
that exist at the pinned checkout against sampled controls. The report's
same-directory baseline is context; the pass threshold uses the overall and
cross-directory random controls. See
[`cochange_backtest.py`](../cochange_backtest.py) for the method.

## Try your own repository

```sh
PYTHONHASHSEED=0 python veripsa/evaluate.py /path/to/your/repo --out my-report.md
```

Use a multi-directory Python repository with meaningful Git history for the
standard-library-only path. An N/A result on a small or history-poor checkout is
not a positive finding, even though N/A-only runs exit `0`. Other languages and
some configuration parsing need optional dependencies; see the
[offline-evaluation guide](../README.md#offline-evaluation).

If the evaluator is useful to your workflow, [star Veripsa Core](https://github.com/RollNuts/veripsa)
to find it again. Share ordinary reproducible issues with the input commit,
Python version, and aggregate result; keep private repository names, local
paths, and source out of public reports. Use [private security reporting](../SECURITY.md)
for sensitive findings.
