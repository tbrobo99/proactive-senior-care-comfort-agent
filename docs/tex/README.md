# Documentation A and B, in LaTeX

The course asks for the documentation (A and B) as a LaTeX file inside version
control. These two files are that.

| File | What it is |
|---|---|
| `part-a.tex` | Deliverable A: proposal and requirement document. Two pages, IEEE two-column. |
| `part-b.tex` | Deliverable B, sections III and IV only: development environment and requirement specifications. Two pages of body plus a half-page attachment. |
| `figures/` | the eight figures from an earlier draft of B. **No document here includes them any more** — kept only because that is the sole copy of those drawings. |

Both describe the same system, FSD-Home. A fixes the requirements (FR-1 to
FR-5, NFR-1 to NFR-3); B states how each one is built.

**`part-b.tex` is a slice, not the whole of B.** It carries III (development
environment) and IV (specifications), and nothing else. Sections I, II and V are
written elsewhere and are not in this repository. `\setcounter{section}{2}` after
`\maketitle` is what makes the first heading print as III instead of I — remove
it and the numbering silently restarts at I, which contradicts the opening
sentence.

An earlier version of this file ran to fourteen pages of structured system
analysis. It was replaced because it described mechanisms the code does not
have — a nine-table schema where `app/db.py` defines three, a whitelist that
included actions `policy.ACTION_TIERS` does not contain, and a claim that the
model never affects control flow when `proactive._decide()` lets it choose which
candidate actions run. Everything in the current file is checkable against a
named file and line.

`part-b.tex` is **generated** from a markdown source, so editing it by hand
means the next build silently discards the edit. `part-a.tex` is hand-written
and is the source.

## Build

```
tectonic -X compile -o tex-build part-a.tex
tectonic -X compile -o tex-build part-b.tex
```

Any TeX distribution works; `pdflatex part-a.tex` twice is enough for the
cross-references. Tectonic is only what we used locally.

**Do not drop the `-o`.** Tectonic writes its PDF next to the input by default,
and the out directory must not be called `latex` — that name collides with the
TeX format name and the compile fails with `could not open format file latex`.

## One thing to know before editing either file

IEEEtran asks for Times, and under XeTeX that request fails silently: the font
falls back to Latin Modern and **the bold shape is dropped**, so the whole PDF
prints with no bold anywhere. Both files carry three lines near the top that set
the font families explicitly. Do not remove them.

The same trap in a different place: `p{}` column widths in `tabular` do not
include the space LaTeX puts between columns, so a naive width split makes every
table stick out of the margin by 12 pt per gap. The table specs here use
`\dimexpr` to subtract it. If a table looks too wide, that is the line to look at.
