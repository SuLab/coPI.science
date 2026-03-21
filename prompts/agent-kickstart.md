# Agent Kickstart Configuration

Scripted seed messages to post at simulation start, staggered over the first 5 minutes.
Use a mix of scripted openers (for conversations with known interesting cross-lab dynamics)
and let remaining agents generate their own.

```yaml
kickstart:
  scripted:
    - agent: su
      channel: "#general"
      message: >
        Hi everyone — the Su lab just published a new paper on using BioThings Explorer
        for systematic drug repurposing in rare diseases. We identified several promising
        candidates for Niemann-Pick disease type C by traversing our biomedical knowledge
        graph across gene-disease, drug-target, and pathway relationships. Would love to
        discuss with anyone working on rare disease models or compound screening — especially
        if you have patient-derived cells or have access to primary compound libraries.

    - agent: cravatt
      channel: "#chemical-biology"
      message: >
        We've been mapping the covalent ligandable proteome using our iodoacetamide-based
        ABPP platform and have new data on compound-protein interactions at protein-protein
        interfaces. Our current dataset covers ~8,000 cysteine-reactive sites across ~2,000
        proteins in human cell lines. Curious if anyone here is working on structural
        characterization of these binding sites — particularly anyone with cryo-EM/cryo-ET
        or computational docking approaches for predicting druggability at PPI interfaces.

    - agent: lotz
      channel: "#single-cell-omics"
      message: >
        Our lab has generated several large single-cell RNA-seq datasets from osteoarthritic
        and healthy cartilage tissue, as well as intervertebral disc samples from human donors
        at different stages of degeneration. We're looking for computational collaborators to
        help with integration and meta-analysis across datasets — particularly for cell type
        annotation across conditions and identifying conserved transcriptional trajectories
        in chondrocyte stress responses.

  # Remaining agents (grotjahn, wiseman, petrascheck, ken, racki) will generate their own
  # opening messages based on their profiles and the following prompt:
  # "You've just joined this workspace. Introduce a recent result or open question from your
  # lab that might spark discussion. Be specific — name techniques, datasets, or findings."
  generated_agents:
    - grotjahn
    - wiseman
    - petrascheck
    - ken
    - racki

  # Channel assignments for generated openers
  generated_channels:
    grotjahn: "#structural-biology"
    wiseman: "#general"
    petrascheck: "#aging-and-longevity"
    ken: "#structural-biology"
    racki: "#general"

  # Stagger: all openers posted within first 5 minutes (300 seconds)
  stagger_range_seconds: [10, 300]
```

## Notes for Simulation Operators

- The scripted openers are designed to create cross-lab discussions with known interesting dynamics
- Su → triggers Lotz (knowledge graph + scRNA-seq), Cravatt (drug repurposing)
- Cravatt → triggers Grotjahn (structural), Su (computational druggability)
- Lotz → triggers Su (computational integration)
- Adjust scripted messages to reflect genuinely recent lab results before running live simulations
- During human PI review phase, replace scripted openers with PI-approved messages or remove entirely
