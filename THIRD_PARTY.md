# Third-party material

| Material | Use here | Terms |
|---|---|---|
| PubMed / MEDLINE abstracts | The study corpus. Only PMIDs, query strings and hashes are distributed; `make corpus` re-fetches the records from NCBI E-utilities. Extracted findings include short evidence quotes. | Abstracts remain the copyright of their publishers and authors. Use of NCBI E-utilities follows the NCBI usage policies. |
| PubTator 3 annotations | Entity normalization of exposures and outcomes. Only a manifest is distributed. | NCBI terms of use for PubTator. |
| ManConCorpus (Alamri and Stevenson, J Biomed Semantics 2016) | External benchmark. Only provenance and the question split are included; download the dataset from its source page. | The source describes it as freely available for use by researchers; it carries no formal licence. See `data/derived/mancon_PROVENANCE.md`. |
| MedCPT query and article encoders | Dense retrieval. Weights are not included; `scripts/download_medcpt.py` fetches pinned public revisions. | See the model cards of the MedCPT encoders. |
| GLM-5.3 Flash, GPT-6 Astra, Claude Fable 5.2 | Model outputs produced for this study are included as results. | Each provider's terms of use. |
