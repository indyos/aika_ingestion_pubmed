# aika_ingestion_pubmed — memoria di progetto

Connettore PubMed/PMC: **primo componente dell'ingestion** della pipeline RAG sull'alimentazione del
paziente in dialisi (ingestion → chunker → indexing su Qdrant con embedding BGE-M3 via servizio gRPC
esistente → agent di interrogazione). L'utente finale è il paziente: il corpus deve contenere **solo
fonti affidabili e approvate da un clinico**. Il connettore cerca su PubMed, scarica solo il full text
dell'**Open Access Subset di PMC**, converte JATS → Markdown + front matter YAML (il "contratto" verso
il chunker) e apre una **PR** su un repository corpus separato (GitHub App a permessi minimi). Scrive
sempre `curation_status: pending`; l'approvazione è del revisore clinico.

Stato: v1 completa e testata (179 test verdi). **Primo run reale eseguito il 2026-09-26** contro NCBI e
GitHub (repo corpus `indyos/aika_doc_ingestion`, clone in `../aika_doc_ingestion`, GitHub App
`aika-doc-ingestion-auth`): 200 risultati esaminati su 271 corrispondenze, 62 documenti nuovi in 4 PR
(#1–#4, branch `ingest/2026-09-26-pubmed-1…4`), 0 errori. Secondo run lo stesso giorno (dopo aver attivato il
filtro `pubmed pmc[sb]`): altri 22 documenti in 2 PR (#5 con 20, #6 con 2, branch `-5` e `-6`); i 62 già in PR
aperte saltati (`in_open_pr`). Esclusi: 104 senza PMCID, 28 licenza non ammessa,
4 senza full text OA, 2 licenza mancante. PR non ancora revisionate né mergiate.

## Comandi

```bash
uv sync                                   # dopo la copia in questa cartella .venv va ricreato
uv run pytest                             # ~70 s (i test git su Windows sono lenti); --block-network attivo
uv run ruff check && uv run ruff format --check && uv run pyright
uv run aika-ingestion-pubmed schema       # rigenera schema/front_matter.schema.json (poi committarlo)
uv run aika-ingestion-pubmed run --dry-run   # cerca+converte, non scrive nulla (usa la rete NCBI)
uv run aika-ingestion-pubmed run
uv run aika-ingestion-pubmed review approve|reject PMID… --branch ingest/… [--notes …] [--all-pending]
uv run pre-commit install                 # hook locali: ruff, format, pyright, schema-in-sync
```

Ambiente: VS Code + uv. `.vscode/settings.json` (interprete `.venv`, pytest, Ruff al salvataggio, LF/UTF-8,
`PYTHONUTF8=1`) e `.vscode/extensions.json` sono già nel progetto: aprire **questa** cartella, non `healthcare-AI`.
`.gitignore` ignora solo `.venv`, quindi `.vscode/` finisce nei commit salvo diversa decisione.

Prima del primo run reale: in `config.yaml` sostituire `ncbi.email` (contatto reale, NCBI lo richiede),
`corpus.repo`, `corpus.local_path` (clone locale del corpus, working tree pulito). Segreti solo da
ambiente (in locale anche da `.env` nella cwd, gitignorato, caricato da `cli._run` con python-dotenv
senza sovrascrivere l'ambiente): `NCBI_API_KEY`, `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`,
`GITHUB_APP_PRIVATE_KEY` o `GITHUB_APP_PRIVATE_KEY_PATH`.

## Architettura (src/aika_ingestion_pubmed/)

`eutils.py` EutilsClient · `search.py` PubMedSearch · `pubmed_parser.py` · `pmc.py` PmcFullTextFetcher
· `jats.py` JatsToMarkdown (+ `inline.py` rendering inline condiviso) · `builder.py` FrontMatterBuilder
(+ `licenses.py`) · `quality.py` QualityGate · `document.py` (serializzazione, hash, parse) ·
`corpus.py` CorpusWriter (git via subprocess) · `review.py` (approve/reject nel branch di una PR) · `github.py` GitHubAppHost · `pipeline.py` (flusso) ·
`cli.py`, `config.py`, `models.py` (Pydantic → JSON Schema), `schema.py`, `xmlutil.py`, `logging_setup.py`.

## Invarianti da non rompere

- **Solo E-utilities** `esearch.fcgi`/`efetch.fcgi` (`db=pubmed|pmc`). Mai `oa.fcgi` né FTP PMC (dismessi
  ad agosto 2026). `EutilsClient` rifiuta ogni altro endpoint.
- **Solo POST**: `api_key` resta nel corpo, mai in URL/log/messaggi d'errore. `tool` ed `email` sempre presenti.
  Rate limit 3/s (10/s con key), retry con backoff su 429/5xx/errori di rete.
- **Determinismo byte per byte**: stesso XML → stesso file. Front matter con ordine chiavi fisso (= ordine dei
  campi di `FrontMatter`), opzionali vuote omesse, PMID sempre stringa, LF, UTF-8, NFC.
- `content_hash` = sha256 del **solo corpo** (così `retrieved_at` non genera diff).
- Nessun articolo senza `<body>` non vuoto nel corpus. Nessun segreto nel repository.
- Se cambia `models.py`: rigenerare lo schema (un test e l'hook pre-commit lo verificano).
- Pyright è **strict su `src/`**; il tipo privato di lxml si nomina solo in `xmlutil.Element`.

## Scelte prese dove la specifica non bastava (modificabili, elencate nel README)

- `xref` bibliografici/nota/affiliazione rimossi; richiami a tabelle/figure/sezioni **mantengono il testo**.
- Celle unite: valore **ripetuto** nelle celle coperte; intestazioni multi-livello unite con ` / `.
- Contenuto senza titolo dopo l'abstract → `## Main text`. Abstract: JATS principale (mai graphical/toc),
  altrimenti PubMed; se nessuno, `## Abstract` resta vuoto.
- `classification_method`: `source` se `doc_type` viene da un publication type, `rule` se cade su `other`.
- «Renal Dialysis» **non mappato** a `modality` (nel MeSH include HD e PD) → `unspecified`, decide il revisore.
- **Flusso di revisione (deciso 2026-09-26):** la PR è il momento della revisione; su `main` solo documenti
  già revisionati. `corpus/` = solo `approved`; i rifiutati stanno in `rejected/` (con `review_notes`) e
  bloccano il PMID; `suspended` si lascia fuori dalla PR. Il connettore scrive `pending`; la CI del corpus è
  rossa finché c'è un `pending` in `corpus/`. `review approve|reject` lavora sul branch della PR (mai su
  `main`). Stesso effetto da GitHub: workflow «Review documenti» (`corpus_ci/review.py` + `review.yml`, menu
  «Use workflow from»; testato byte per byte contro `review_branch` in `tests/test_review_script.py`) oppure
  commento `/approve` | `/reject motivo` sul file in «Files changed» (`review-comment.yml` +
  `review_comment.py`). **Storia (2026-09-26/27):** i primi commenti non avviavano nulla perché i branch delle PR di ingestion erano
  nati/aggiornati prima che `review-comment.yml` esistesse su `main` (il merge-ref della PR restava vecchio e non
  conteneva il file). Fix: PR #11 (trigger anche `pull_request_review`, decisioni idempotenti) **e** merge di
  `origin/main` nei branch aperti. Verificato: un commento `/approve` della GitHub App su PR #4 ha ricevuto in 20 s
  la risposta «Non applicato: solo proprietario…» con reazione 😕 (l'App non è OWNER). **Regola:** dopo ogni modifica
  ai workflow su `main`, aggiornare (merge di main) i branch `ingest/*` ancora aperti. I `/approve` vanno scritti
  come commento **sul file** (non nel riepilogo della review); i commenti in bozza non generano eventi; la GitHub App
  non può leggere Actions (403): per vedere le esecuzioni serve la scheda Actions o il permesso *Actions: read*.
  Negli aggiornamenti di un documento già approvato `review_notes` è conservato e lo stato torna `pending`
  (va riapprovato). Il chunker legge solo `corpus/`.
- Ritrattazioni: pt *Retracted Publication*/*Retraction of Publication* **più** link `RetractionIn`.
- Licenze ammesse: CC0, CC BY, CC BY-SA (NC/ND no). Abstract senza full text esclusi. Filtro OA in ricerca
  predisposto (`search.extra_filter`) ma spento. Client httpx proprio, non Biopython.
- `parse_document` tollera CRLF (file del corpus modificati su Windows).

## Insidie già incontrate

- **Windows + percorso lungo**: in cartelle molto profonde le DLL di `cryptography` non si caricano
  ("estensione troppo lunga"). Fix: `UV_PROJECT_ENVIRONMENT` su un percorso corto. In
  `C:\workspace\healthcare-AI\aika_ingestion_pubmed` non dovrebbe servire.
- Console Windows cp1252: usare `PYTHONUTF8=1` per stampare `≤`, `µ`, ecc.
- lxml: `root.append(el)` **sposta** il nodo; nei test copiare con `copy.deepcopy`.
- Il JSON Schema **non può** verificare `id == pmid-<pmid>` né nome file: la CI del repo corpus deve farlo.
- `git` su Windows con `core.autocrlf=true` produce file CRLF: il codice usa `-c core.autocrlf=false`.
- Negli script di patch generati evitare i doppi backslash (si corrompono): usare Edit o `chr(92)`.

## Test

`tests/support.py`: `FakeNcbi` (httpx.MockTransport su fixture), repository git temporanei (origin bare +
clone), `FakeHost`. Le fixture JATS in `tests/fixtures/jats/` sono **scritte a mano** (casi difficili:
abstract strutturato, celle unite, tabella dentro `<p>`, figure, liste, senza body, più abstract, licenza NC).
`tests/fixtures/jats_real/` + `pubmed_real/` contengono 3 articoli PMC **reali** CC BY 4.0 (scaricati il
2026-09-26 con `efetch`; test in `tests/test_real_fixtures.py`: tabelle, sup/sub, sezioni multilivello,
determinismo). `pytest-recording` è installato ma non serve: le fixture sono file XML statici.
Fatta una prova di mutazione sul convertitore: le regole di esclusione e di filtro abstract ora sono coperte.

## Da fare / non verificato

- Fatto (2026-09-26): smoke test reale, `efetch db=pmc` in POST restituisce JATS convertibile; GitHub App
  creata e funzionante (token, push, PR). Conversione controllata a campione: tabelle, sezioni e front
  matter corretti; `<sup>`/`<sub>` restano nel Markdown per scelta. Le esclusioni per licenza (15/100 a
  campione) sono tutte CC BY-NC/NC-ND reali; un caso «licenza mancante» è solo testo libero senza CC.
- Filtro `search.extra_filter: "pubmed pmc[sb]"` ATTIVO (verificato il 2026-09-26: 271 → 129 risultati,
  gli stessi 129 con PMCID, nessuno perso). Non esiste un filtro «open access» in PubMed
  (`"open access"[filter]`, `"pmc open access"[filter]` → 0 risultati): la licenza resta al quality gate.
  Con il filtro un dry-run dà 84 nuovi (62 già nelle PR #1–#4 + 22 nuovi): un run reale li salta perché
  `in_open_pr` e apre una PR solo per i nuovi.
- CI del repository corpus: #7 e #8 sono mergiate (`corpus/` solo `approved`, `rejected/` solo `rejected` con
  `review_notes`, PMID non duplicato; workflow «Review documenti»). Il merge della #8 è avvenuto prima
  dell'ultimo commit: la **PR #9** (`ci/review-menu`, da mergiare) sostituisce il campo testuale `branch` con il
  menu nativo «Use workflow from». Sorgenti: `corpus_ci/` (`validate_corpus.py`, `validate-corpus.yml`,
  `review.py`, `review.yml`), copie in `scripts/` e `.github/workflows/` di `aika_doc_ingestion`: tenerle
  allineate. Il push di file workflow richiede le credenziali dell'utente (la GitHub App non ha il permesso
  *Workflows*). Repo privato su piano gratuito: le branch protection non sono applicate, il blocco è solo il
  check rosso. Testate in `tests/test_review.py` e `tests/test_review_script.py`; da tenere allineate a
  `models.py`.
- Le PR di ingestion #1–#6 contengono ancora file `pending`: vanno rivisti (workflow o comando `review`) dopo
  «Update branch», prima del merge.
- Query attuale (`config.yaml`): `("Renal Dialysis"[Mesh] OR hemodialysis[TiAb]) AND (Diet/Diet Therapy/
  Nutritional Requirements/Nutritional Status/Nutrition Therapy con `[Majr]`)`. La versione con `[Mesh]` +
  `[TiAb]` su diet/nutrition portava molti articoli fuori tema. `max_results: 200` lascia fuori ~71 delle
  271 corrispondenze: alzarlo se si vuole coprire tutto.
- ~76% dei documenti cade in `doc_type: other` / tier 5 (manca il publication type NLM): estendere
  `mapping.publication_type_doc_type` (es. Cross-Sectional/Cohort Studies) o lasciar classificare al revisore.
- Nomi branch `ingest/<data>-pubmed-<n>`: `n` riparte dal primo numero libero sul remoto (`git ls-remote`), quindi
  più run nello stesso giorno non collidono (test in `tests/test_pipeline.py`).
- Gli originali JATS finiscono in `originals/` (locale, gitignorato); `original_uri` è un `file:///` locale,
  quindi non valido su altre macchine: serve storage condiviso (`originals.base_uri`) + upload, non implementato.
- Il Markdown delimita le tabelle con righe `::: table id=…`: il chunker deve gestirle.
- Validare con il clinico il vocabolario `mapping.topics`, le mappe MeSH→modality e gli `evidence_tier`.
- Non implementati (fuori scope v1): PDF, siti web, chunking, embedding, UI di revisione, pre-triage LLM,
  job di controllo retraction sui documenti già approvati, `include_abstract_only`.
- Il repository git (`.git`) e `.venv` non sono stati copiati con la cartella: `git init`/riallineare e `uv sync`.
