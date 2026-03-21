# Su Lab — Public Profile

**PI:** Andrew Su, PhD
**Department:** Integrative Structural and Computational Biology (ISCB), Scripps Research
**Lab:** Su Lab

## Research Areas

The Su lab develops computational infrastructure and AI-driven approaches for biomedical knowledge
integration and drug discovery. We specialize in building and maintaining large-scale biomedical
knowledge graphs that integrate heterogeneous data from genomics, proteomics, pharmacology, and
clinical databases. Our work sits at the intersection of data science, bioinformatics, and
translational medicine.

**Key focus areas:**
- **Knowledge graphs and semantic biomedical data integration** — BioThings ecosystem (MyGene.info, MyVariant.info, MyChem.info, BioThings Explorer)
- **Drug repurposing using network traversal** — identifying non-obvious therapeutic candidates for rare and neglected diseases
- **Agentic AI for biomedical discovery** — LLM-powered agents that can query, reason over, and synthesize biomedical knowledge
- **Biomedical NLP and literature mining** — extracting structured information from scientific literature at scale

## Key Methods and Technologies

- Knowledge graph construction and traversal (property graphs, RDF, SPARQL)
- Large language model (LLM) fine-tuning and evaluation for biomedical tasks
- BioThings API framework — RESTful data services for genes, variants, chemicals, diseases
- Network analysis: node2vec, graph neural networks (GNNs) for link prediction
- Semantic similarity and embedding-based entity resolution
- Python/Elasticsearch/MongoDB stack for scalable data services
- Benchmarking frameworks for biomedical LLMs (BioASQ, BLURB)

## Model Systems and Data Sources

- BioThings knowledge graph (integrates >30 public databases including NCBI, OMIM, ChEMBL, DrugBank, DisGeNET, UniProt, Reactome)
- PubMed/PMC full-text corpus (~35M articles) for NLP tasks
- ClinVar and gnomAD for variant interpretation
- STRING and BioGRID for protein-protein interaction networks
- DrugBank and ChEMBL for drug-target relationships

## Current Active Projects

1. **BioThings Explorer v2** — expanding our knowledge graph traversal tool to support multi-hop reasoning and agentic query formulation
2. **Rare disease drug repurposing** — systematic traversal of drug-gene-disease networks to identify approved compounds with potential for off-label use in ultra-rare diseases (Niemann-Pick C, Batten disease)
3. **LLM benchmarking for biomedical discovery** — creating and maintaining community benchmarks for scientific reasoning and knowledge retrieval in biomedical LLMs
4. **Agentic bioinformatics** — AI agents that can autonomously execute multi-step analytical pipelines (literature search → data retrieval → statistical analysis → interpretation)

## Open Questions / Areas Seeking Collaborators

- **Wet-lab validation of drug repurposing candidates** — we generate candidates computationally and actively seek labs with relevant disease models (rare metabolic disorders, neurological diseases) to test predicted compounds
- **Structural biology integration** — connecting our knowledge graph outputs (drug-target predictions, variant effects) with structural evidence from cryo-EM/cryo-ET
- **Multi-omics data integration** — incorporating single-cell transcriptomics, proteomics, and metabolomics datasets into our knowledge graph infrastructure
- **Clinical data access** — identifying collaborators with EHR or claims data to validate drug repurposing predictions in real-world patient populations

## Available Resources / Unique Capabilities

- **BioThings API infrastructure** — free public APIs querying biomedical knowledge at scale (>1B queries/month)
- **Biomedical knowledge graph** — unified graph integrating 30+ public databases, updated monthly
- **LLM evaluation toolkit** — established benchmarking pipeline for testing biomedical AI systems
- **Computational capacity** — cloud computing infrastructure for large-scale data processing
- **Domain expertise** in rare disease genetics and drug-target interaction networks
