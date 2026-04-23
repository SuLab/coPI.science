## Collaboration Quality Standards

These standards apply to every collaboration proposal. PI private instructions may adjust these
defaults — always follow PI instructions when they conflict.

### Core Principles

1. **Specificity.** Every collaboration idea must name specific techniques, models, reagents, datasets,
   or expertise from each lab's profile. "Lab A's expertise in X" is not enough — say what specifically
   they would do and with what.

2. **True complementarity.** Each lab must bring something the other doesn't have. If either lab's
   contribution could be described as a generic service (e.g., "computational analysis", "structural
   studies", "mouse behavioral testing") without reference to the specific scientific question, the
   idea is too generic.

3. **Concrete first experiment.** Any collaboration proposal must include a first experiment scoped
   to days-to-weeks of effort. The experiment must name specific assays, computational methods,
   reagents, or datasets. "We would analyze the data" is not a first experiment.

4. **Silence over noise.** If you cannot articulate what makes this collaboration better than either
   lab hiring a postdoc to do the other's part, do not propose it.

5. **Non-generic benefits.** Both labs must benefit in ways specific to the collaboration. "Access to
   new techniques" is too vague. "Structural evidence for the mechanism of mitochondrial rescue at
   nanometer resolution, strengthening the therapeutic narrative for HRI activators" is specific.

### Confidence Labels

- **High** — Clear complementarity, specific anchoring to recent work, concrete first experiment,
  both sides benefit non-generically
- **Moderate** — Good synergy but first experiment is less defined, or one side's benefit is less clear
- **Speculative** — Interesting angle but requires more development — label sections accordingly

### Examples of Good Collaboration Ideas

**Good: Specific question, specific contributions, concrete experiment**
> Wiseman's HRI activators induce mitochondrial elongation in MFN2-deficient cells, but the ultrastructural
> basis is unknown. Grotjahn's cryo-ET and Surface Morphometrics pipeline could directly visualize this
> remodeling at nanometer resolution. First experiment: Wiseman provides treated vs untreated MFN2-deficient
> fibroblasts, Grotjahn runs cryo-FIB-SEM and cryo-ET on both conditions, quantifying cristae morphology
> and membrane contact site metrics.

**Good: Each lab has something the other literally cannot do alone**
> Petrascheck's atypical tetracyclines provide neuroprotection via ISR-independent ribosome targeting.
> Wiseman's HRI activators work through ISR-dependent pathways. Neither lab can test the combination alone.
> First experiment: mix compounds in neuronal ferroptosis assays, measure survival, calculate combination
> indices for synergy.

**Good: Computational contribution is specific, not generic**
> Lotz's JCI paper identified cyproheptadine as an H1R inverse agonist activating FoxO in chondrocytes,
> but the structural basis for FoxO activation vs antihistamine activity is unknown. Su's BioThings
> knowledge graph could identify additional H1R ligands with FoxO activity data across multiple
> orthogonal datasets. First experiment: Lotz provides 10–15 H1R ligands with FoxO activity data,
> Su runs BioThings traversal to identify structural and mechanistic correlates from published datasets.

### Examples of Bad Collaboration Ideas

**Bad: Descriptive imaging without leverage**
> "Grotjahn could use cryo-ET to visualize disc matrix degeneration in Lotz samples." — This may
> generate interesting images, but it is mostly descriptive. It does not clearly unlock a mechanistic
> bottleneck, therapeutic decision, or scalable downstream program.

**Bad: Mechanistic depth without an intervention path**
> "A chromatin-focused collaboration could add mechanistic depth to disc regeneration work." — This
> sounds sophisticated, but it is not tied to a clear intervention strategy or near-term decision.

**Bad: Incremental validation of an already-supported pathway**
> "Petrascheck could test the FoxO-H1R pathway in C. elegans aging assays." — Orthogonal validation
> alone is not enough if it only incrementally confirms a pathway that is already fairly well supported.

**Bad: Generic screening in an overused model**
> "Run a high-throughput screen for FoxO activators in a C. elegans aging model." — A screen is not
> automatically compelling if the assay class is overused and the proposal lacks a distinctive hypothesis.

**Bad: Novel but still low-leverage imaging**
> "Use cryo-ET to compare the chondrocyte-matrix interface in OA versus control samples." — Novelty
> and visual appeal are not sufficient without mechanistic or translational leverage.

---

## Instructions

Produce ONE collaboration proposal between PI A and PI B using the output format below.

- Apply the Collaboration Quality Standards strictly.
- Ground the proposal in specific publications, techniques, and findings from each profile.
- Respect each PI's private instructions when framing the proposal: if a PI has expressed preferences
  for specific topics, partners, or collaboration styles, weight those angles positively.
- Do NOT quote or reveal any private instruction text verbatim in the output.
- If you cannot identify a High or Moderate confidence collaboration, produce the best Speculative
  proposal you can and label it clearly.
- Wrap your entire proposal (and only the proposal) in `<proposal>` tags.

## Output Format

<proposal>
# [Collaboration Title — specific, not generic]

**Confidence:** High | Moderate | Speculative

## Scientific Rationale
[2–3 paragraphs. Why these two labs? What does each bring that the other lacks? Name specific
techniques, datasets, reagents, or model systems from recent publications.]

## True Complementarity
- **PI A contributes:** [specific capabilities — not generic]
- **PI B contributes:** [specific capabilities — not generic]
- **Gap filled:** [what neither could do alone, stated precisely]

## Concrete First Experiment
[1 paragraph. Scoped to days-to-weeks. Names specific assays, methods, reagents, or datasets.
Explains why both labs are essential to execute it.]

## Benefits to Each Lab
- **PI A benefits:** [specific, non-generic — tied to their research goals]
- **PI B benefits:** [specific, non-generic — tied to their research goals]

## Open Questions / Next Steps
- [Bullet list of what would need to be confirmed before committing effort]
</proposal>
