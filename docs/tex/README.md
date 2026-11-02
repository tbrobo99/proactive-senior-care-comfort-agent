# Documentation A and B, in LaTeX

The course asks for the documentation (A and B) as a LaTeX file inside version
control. These two files are that. They are **generated**, not hand-written:

| File | Source | What it is |
|---|---|---|
| `part-a.tex` | `CSE406_Phase2_A_Proposal_Final.docx` | Deliverable A: proposal and requirement document |
| `part-b.tex` | `PartB_Specification_Draft.md` | Deliverable B: specification, structured system analysis |
| `figures/` | `make_partb_figures.py` plus the Word original | the eight figures in B, and the architecture diagram in A |

If a sentence has to change, change it in the source and regenerate. Editing the
`.tex` by hand means the next build silently discards the edit.

## Build

```
tectonic -X compile -o tex-build part-b.tex
tectonic -X compile -o tex-build part-a.tex
```

Any TeX distribution works; `pdflatex part-b.tex` twice is enough for the
cross-references. Tectonic is only what we used locally.

**Do not drop the `-o`.** Tectonic writes its PDF next to the input by default,
and the out directory must not be called `latex` — that name collides with the
TeX format name and the compile fails with `could not open format file latex`.

## Two things in here are not finished

1. **`part-a.tex` still carries placeholders** from the Word original: the
   `[Name]` cells in Table I and `[Group Number]` on the first page. Fill them
   in the source document and regenerate; do not patch the `.tex`.
2. **A and B describe different systems.** A is the conversational
   appliance-control proposal; B is the risk-graded orchestrator we moved to
   afterwards. The course sheet allows this — "you can change anything in the
   introduction/requirement sections and make changes accordingly" — but a
   reader comparing the two will notice, so B states its own requirements and
   traces them itself. If the group wants A brought in line, that is a rewrite
   of A's Sections I and II, not a formatting change.
