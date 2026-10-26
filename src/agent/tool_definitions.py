"""The tool schemas the engine offers to models (spec §7.5). Dependency-free so
``roles`` can read the tool names without importing ``tools`` (which imports
``roles``). The literal is moved verbatim: it is part of every model request
(C27), and the golden suite pins it."""

from typing import Any

# Anthropic tool-use schema definitions
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "retrieve_profile",
        "description": (
            "Retrieve the public profile of another lab's agent. "
            "Returns their research focus, techniques, recent publications, "
            "and other publicly available information. "
            "Pass the agent ID or bot name. A lab you cannot currently interact with, "
            "or an unknown ID, returns 'No public profile found'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "The agent ID to look up (e.g., 'wiseman', 'su', 'cravatt')",
                }
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "retrieve_abstract",
        "description": (
            "Fetch a paper's abstract from PubMed. Accepts a PMID (e.g., '12345678') "
            "or DOI (e.g., '10.1234/journal.2024'). Returns title, abstract, journal, year."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pmid_or_doi": {
                    "type": "string",
                    "description": "PubMed ID or DOI of the paper",
                }
            },
            "required": ["pmid_or_doi"],
        },
    },
    {
        "name": "retrieve_full_text",
        "description": (
            "Fetch full text (methods section) from PubMed Central. Use sparingly — "
            "only when the abstract is insufficient and the paper is central to a "
            "potential collaboration. Up to 2 uses per thread."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pmid_or_doi": {
                    "type": "string",
                    "description": "PubMed ID or DOI of the paper",
                }
            },
            "required": ["pmid_or_doi"],
        },
    },
    {
        "name": "search_prior_art",
        "description": (
            "Search issued and published US patent filings (USPTO Open Data Portal) "
            "for prior art. Matches on INVENTION TITLE ONLY. Pass 2-4 highly specific "
            "terms — gene/target symbols, a compound name, a modality — NOT a sentence. "
            "A long descriptive query cannot match any real patent title and will come "
            "back empty no matter how crowded the field is. Good: 'TFEB melanoma'. "
            "Bad: 'TFEB inhibitor nuclear translocation melanoma BRAF resistance'. "
            "Your terms are ANDed for you: do NOT write AND/OR/NOT, which are "
            "query syntax and are dropped rather than searched. "
            "US filings only — absence of a hit is NOT proof of novelty or FTO."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "2-4 specific terms matched against the patent title (e.g. "
                        "'C9orf72 repeat', 'TFEB melanoma'). Not a description."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "consult_specialist",
        "description": (
            "Ask one member of the Blackbird evaluation panel for an opinion in "
            "their domain. Use this DURING an interview, as soon as the PI says "
            "something that falls in a specialist's area — their questions_to_ask "
            "become your next question to the PI, which is worth far more than "
            "asking them after you have already formed a view.\n\n"
            "Domains: 'scientific' (rigor, controls, power, interpretability, "
            "mouse-to-human translatability), 'chemistry' (path to a development "
            "candidate, medchem tractability, tolerability, in-family off-targets), "
            "'clinical' (unmet need vs standard of care, indication, patient "
            "numbers), 'commercial' (competitive landscape, named competing "
            "programs, deal comps), 'legal' (FTO, licensing, research-tool "
            "encumbrance), 'technologic' (platform feasibility, whether the work "
            "would test it), 'talent' (execution probability, conflicts of "
            "interest, over-commitment), 'budget' (scope against Blackbird's grant "
            "bands and 12-24 month durations).\n\n"
            "An advance or conditional verdict is REFUSED if the domains the idea "
            "touches were never consulted, and you cannot consult during the "
            "assessment turn — only here, in the interview. Consult early."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "enum": [
                        "scientific", "chemistry", "clinical", "commercial",
                        "legal", "technologic", "talent", "budget",
                    ],
                    "description": "Which specialist to ask.",
                },
                "question": {
                    "type": "string",
                    "description": (
                        "The specific question, in your own words. Not 'what do you "
                        "think' — name the claim you want tested."
                    ),
                },
                "context": {
                    "type": "string",
                    "description": (
                        "The relevant part of the interview so far: what the PI "
                        "actually said about this. Quote them where you can."
                    ),
                },
            },
            "required": ["domain", "question", "context"],
        },
    },
]

#: The two search tools only BlackbirdBot's project work offers: the worker's research
#: sessions and the hub in a project thread. Kept out of ``TOOL_DEFINITIONS``, which every
#: interview request carries unchanged.
PROJECT_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search_pubmed",
        "description": (
            "Search PubMed by keywords. Returns up to 10 papers, most relevant first, each "
            "with its PMID, year, title and abstract. Pass a few specific terms (a target, "
            "a disease, a method), not a sentence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A few specific search terms, e.g. 'IBAT inhibitor cholestasis'.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_trials",
        "description": (
            "Search ClinicalTrials.gov. Returns up to 10 studies, each with its NCT number, "
            "title, phase, status, conditions and interventions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A few specific search terms: a condition, an intervention or both.",
                },
            },
            "required": ["query"],
        },
    },
]
