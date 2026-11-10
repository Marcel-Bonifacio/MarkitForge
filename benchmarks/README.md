# Quality benchmark

`run_benchmark.py` builds a fixed set of tricky documents from code (no binary
fixtures to go stale), converts them, and scores the Markdown against known
ground truth. Scores run from 0 to 1.

| Metric | Meaning |
|---|---|
| text | share of expected words present |
| headings | share of expected headings written as `#` headings |
| tables | share of expected cells found inside table rows (Markdown or HTML) |
| order | 1 if key paragraphs appear in reading order |
| clean | 1 if no junk (`NaN`, `Unnamed: 0`, hidden-sheet content) |

```bash
pip install -e 'packages/markitforge[all]' reportlab python-docx openpyxl
python benchmarks/run_benchmark.py                    # table of scores
python benchmarks/run_benchmark.py --json
python benchmarks/run_benchmark.py --check            # CI: fail on regression
python benchmarks/run_benchmark.py --update-baseline  # accept new scores
python benchmarks/run_benchmark.py --engine markitdown  # score upstream MarkItDown
```

`--check` compares each case's overall score with `baseline.json` and fails
when one drops more than 0.02. Raise the baseline (`--update-baseline`) when a
change makes things better, in the same pull request.

## Adding a case

Add a `Case` to `CASES` with a `build(dir) -> path` function and the ground
truth (`words`, `headings`, `cells`, `order`). Keep fixtures small and
deterministic.
