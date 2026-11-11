# Anticipate: the variants a figure invites, before you are asked for them

Most follow-on asks on a report are an existing figure re-cut along a predictable axis, not a new question. This checklist is the one copy of those axes. It runs twice: over the expected figures while the data contract is drafted (`references/data-contract.md` § 2), where a hit may add a column; and over every new or changed figure after each render (`references/figure-loop.md` § A turn), where a hit becomes a variant in the draft.

The list was fitted to one user's asks over a handful of sessions, so each item is a hypothesis with a few supporting cases, not a law. When an ask arrives that no item predicts, add an item; when an item never fires over several reports, drop it.

## The checklist

For every figure or table, and every headline number:

1. **Paired quantity.** The column has a sibling with the same role (observed/estimated, two sources of one measure, camera A/B) and only one is shown. Plot each, same axes.
2. **Other denominator.** The figure shows counts or one conditional of a two-way relationship. Add the other conditionals: per row, per column, and the transposed one.
3. **Threshold sweep.** The figure depends on one literal cut-point or binarisation. Make it a parameter and show a small sweep over the existing bin edges.
4. **Population filter.** The load or the cell drops rows (a dedupe, "latest per unit", a date filter). Build the unfiltered variant as a parameter and state the population in every caption.
5. **Summary-statistic swap.** A per-unit distribution is reduced to its mean. Add the median, the share above zero, and the obvious bounded transform (a logit, with its zero mapping stated).
6. **Point mass at a boundary.** Many values sit exactly at a bound, or a transform is undefined there, and they are excluded or drawn differently. Give them the same encoding as the rest, in their own band beside the axis.
7. **Other covariate.** Columns in the same table with the same role as the one plotted are unplotted. Repeat the figure against each.
8. **Prior figure.** An earlier report or artifact on the same variables has a figure this one lacks. Reproduce it on this data.
9. **Complement and cost.** A reported fraction invites "what about the rest"; a reported size or time invites "how much more". Put the complement next to the fraction and the extension's size and time next to any change offered.

Representation asks (a colour map, a plot type, tick grouping) are not on the list: set them once in the repository's style primitives and otherwise take them as turns.

## What happens to a hit

Classify each hit as the figure loop classifies a turn: a **cell**, a **parameter**, or a **change to the data contract**. Then:

- a cell-cost hit is built now, as a variant under its primary figure in the draft (`references/variant-picker.md`), never as a question;
- a parameter-cost hit is built now when the render stays under a minute, else offered;
- a hit that changes the data contract is offered, with its size and time, as the first option in the closing question, before `add · change · variant`.

Never ask "would you like more?". Name the figure, the variant and its cost.

The primary figure is always the first in its group and is never a variant. A variant inherits its primary's axes, scale, colour and caption, with one line stating what changed, so the reader compares rather than re-reads.
