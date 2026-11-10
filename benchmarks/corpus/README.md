# Real-document corpus

Put real documents here, each with a `NAME.truth.json` next to it that says
what a good conversion must contain, then run:

```bash
python benchmarks/run_benchmark.py --corpus benchmarks/corpus
python benchmarks/run_benchmark.py --corpus benchmarks/corpus --update-baseline
python benchmarks/run_benchmark.py --corpus benchmarks/corpus --check
```

`NAME.truth.json` fields (all optional):

| Field | Meaning |
|---|---|
| `words` | words that must appear |
| `headings` | text that must appear as Markdown headings |
| `cells` | values that must appear inside table rows |
| `order` | snippets that must appear in this order |
| `absent` | text that must not appear (page headers, footers, junk) |
| `single_line` | snippets that must not be split across lines |
| `table_count` | exact number of tables |
| `options` | conversion options, e.g. `{"table_format": "auto"}` |

Only commit documents you are allowed to redistribute (public domain, CC0,
your own files). For private test sets, keep the folder outside the repo and
point `--corpus` at it. `autogen-paper.docx` is MarkItDown's own MIT test file.
